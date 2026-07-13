r"""Legacy-install migration for the Tauri desktop shell (M-7/S-3 spec §S-3 item 2).

The first Tauri release lands on machines that may still carry the *legacy* shell-bundle
install (`installer/windows-native/shell/install.ps1` on Windows;
`installer/macos/shell/install.sh` on macOS). Those installs left a differently-shaped runtime
behind — a `repo\`/`.venv\`/`uv\`/`bin\` tree, a Start-Menu shortcut, a scheduled task, an
HKCU Run key, a user-PATH entry (Windows); a LaunchAgent plist and a `~/.local/bin/msa`
launcher (macOS). The Tauri era owns none of that, so it must be **removed** without ever
touching the ADR-009 DataDir (`%USERPROFILE%\MediaSearchAgent` / `~/Library/Application
Support/MediaSearchAgent` — config.yaml + index), which survives byte-identical (M-7 exit
criterion #4).

Two entry points run the same removal set, kept in **allowlist parity**:
  1. the NSIS ``NSIS_HOOK_PREINSTALL`` in ``packaging/windows/msa-installer-hooks.nsh``
     (the primary path — runs before file extraction on every Windows install);
  2. ``sweep_legacy_install`` here — a belt-and-braces first-run pass the shim runs (covers a
     direct-download install that skipped the NSIS hook, and the macOS legacy artifacts NSIS
     can't reach).

**This module's core is a PURE enumerator** (:func:`plan_migration`): it takes injectable
platform roots and RETURNS the plan — it deletes nothing. Every filesystem target it lists is
proven, at enumeration time, to sit strictly **inside a fixed allowed root** and **outside the
DataDir** (symlink- and ``..``-escapes are refused, not followed — realpath containment). The
executor (:func:`execute_migration`) re-checks the same invariants before every delete
(defense in depth). The DoD deletion itself is exercised only in the human-only Hyper-V pass;
the offline suite unit-tests the enumerator (and the executor over tmp trees).

Hard invariants (encoded + tested in ``tests/test_desktop_migration.py``):
  * operate ONLY under the fixed allowed roots (Windows: ``%LOCALAPPDATA%\MediaSearchAgent`` +
    the Start-Menu Programs dir; macOS: ``~/Library/LaunchAgents`` + ``~/.local/bin``);
  * the delete ALLOWLIST is exactly ``repo\`` ``.venv\`` ``uv\`` ``bin\`` ``Cache\models\``
    ``logs\`` ``start.ps1`` ``stop.ps1`` ``version.txt`` + Start-Menu shortcut + scheduled
    Task "MediaSearchAgent" + HKCU Run value + the PATH entry (macOS: plist + msa launcher);
  * NEVER a wholesale removal of ``%LOCALAPPDATA%\MediaSearchAgent`` — only its named children;
  * NEVER touch the DataDir (``%USERPROFILE%\MediaSearchAgent`` /
    ``~/Library/Application Support/MediaSearchAgent``) — if the AppDir root ever resolves to
    (or inside) the DataDir, the whole AppDir enumeration is refused;
  * idempotent — missing paths are no-ops.

The old macOS ``MediaSearchAgent.app`` is deliberately NOT enumerated for deletion: from inside
the shim we cannot tell the *legacy Swift* ``.app`` apart from the *new Tauri* ``.app`` (both
live at ``~/Applications/MediaSearchAgent.app``), and the installer/updater already replaces the
bundle in place. Deleting a ``.app`` from a first-run sweep risks removing the running app, so
we leave it to the installer (drag-install / Tauri updater overwrite).
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

# ── allowlist constants (parity with msa-installer-hooks.nsh) ─────────────────
# Legacy Windows AppDir (name-keyed, %LOCALAPPDATA%\MediaSearchAgent) — the ONLY root the
# AppDir file/dir removals may touch. Distinct from the Tauri app-private dir
# %LOCALAPPDATA%\ai.openara.mediasearchagent (identifier-keyed), so the new install is never hit.
_APPDIR_NAME = "MediaSearchAgent"
# The ADR-009 DataDir shares the *basename* but lives under a different root — %USERPROFILE% on
# Windows, ~/Library/Application Support on macOS. NEVER touched.
_DATADIR_NAME = "MediaSearchAgent"

# Directories under the legacy AppDir that the migration removes (spec §S-3 item 2; dev decision
# (b): the legacy Cache\models\ ~1.5 GB and logs\ ARE deleted). ``Cache\models`` is the nested
# model cache — NOT the whole ``Cache`` tree.
_APPDIR_DIRS = ("repo", ".venv", "uv", "bin", "Cache/models", "logs")
# Files at the legacy AppDir root that the migration removes.
_APPDIR_FILES = ("start.ps1", "stop.ps1", "version.txt")

# COLLISION GUARD: Tauri's NSIS currentUser install lands at %LOCALAPPDATA%\MediaSearchAgent
# (productName) — the SAME root as the legacy AppDir — and stages these children as LIVE
# resources of the running app. The NSIS PREINSTALL removes legacy `bin\` safely (it runs BEFORE
# extraction). But the Python belt-and-braces sweep runs on first launch FROM INSIDE that live
# install, so when the legacy root IS the running app's install dir it must NOT delete these — that
# would wipe the new app's staged uv/exiftool/mediainfo (bin), the shim itself (backend), the LIVE
# ADR-009 log dir (logs — msa-desktop.log is being written right now; in the field only an open
# handle [WinError 32] saved it), or the LIVE model cache (Cache\models — the setup flow's
# downloads). Legacy content inside those dirs is the NSIS PREINSTALL hook's job (it runs before
# the new install exists); the in-app sweep only refuses children that are the running install.
# NB: "backend" has no delete path today (it is not in _APPDIR_DIRS — no legacy dir shares the
# name); it is listed defensively so a future allowlist addition can never sweep the shim itself.
_TAURI_LIVE_CHILDREN = ("bin", "backend", "logs", "Cache/models")

# Start-Menu shortcut folder (under %APPDATA%\Microsoft\Windows\Start Menu\Programs).
_START_MENU_REL = ("Microsoft", "Windows", "Start Menu", "Programs", "Media Search Agent")

# Scheduled task + HKCU Run value name (both "MediaSearchAgent"); the legacy user-PATH entry.
_SCHEDULED_TASK = "MediaSearchAgent"
_RUN_SUBKEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
_RUN_VALUE_NAME = "MediaSearchAgent"
_PATH_ENTRY_REL = "bin"  # %LOCALAPPDATA%\MediaSearchAgent\bin was added to the user PATH

# Legacy tray process (stopped, not deleted — its file lives under bin\ which IS deleted).
_STOP_PROCESSES = ("MediaSearchAgentTray.exe",)

# macOS legacy artifacts.
_MACOS_LAUNCH_AGENT = "ai.openara.mediasearchagent.plist"  # ~/Library/LaunchAgents/<this>
_MACOS_LAUNCHER_REL = (".local", "bin", "msa")             # ~/.local/bin/msa


class MigrationError(RuntimeError):
    """A migration invariant was violated (e.g. a target resolved outside its allowed root)."""


@dataclass(frozen=True)
class SkippedTarget:
    """A candidate the enumerator REFUSED for safety (symlink/``..`` escape, or DataDir overlap)."""

    path: Path
    reason: str


@dataclass(frozen=True)
class MigrationPlan:
    """The removal plan — pure data, no side effects. ``directories``/``files``/``launch_agents``
    are proven safe (strictly within an ``allowed_root`` and outside every ``protected_root``);
    unsafe candidates land in ``skipped``. The executor re-checks the same invariants."""

    platform: str
    allowed_roots: tuple[Path, ...] = ()
    protected_roots: tuple[Path, ...] = ()   # DataDir(s) — must never be touched
    directories: tuple[Path, ...] = ()       # rmtree targets
    files: tuple[Path, ...] = ()             # unlink targets
    launch_agents: tuple[Path, ...] = ()     # macOS plists (launchctl-unloaded, then unlinked)
    scheduled_task: str | None = None
    run_value: tuple[str, str] | None = None  # (subkey, value_name)
    path_entry: Path | None = None            # dir to strip from the user PATH
    stop_processes: tuple[str, ...] = ()
    skipped: tuple[SkippedTarget, ...] = ()

    def fs_targets(self) -> tuple[Path, ...]:
        """Every filesystem path the plan would delete (dirs + files + plists)."""
        return self.directories + self.files + self.launch_agents

    def is_empty(self) -> bool:
        return not (
            self.fs_targets()
            or self.scheduled_task
            or self.run_value
            or self.path_entry
            or self.stop_processes
        )


# ── containment helpers (realpath — refuses symlink/.. escapes) ───────────────


_EXT_PREFIX = "\\\\?\\"           # \\?\C:\...        (Windows extended-length path prefix)
_EXT_UNC_PREFIX = "\\\\?\\UNC\\"  # \\?\UNC\srv\share (extended-length UNC form)


def _strip_extended_prefix(s: str) -> str:
    r"""Drop the Windows extended-length prefix so ``\\?\C:\x`` compares equal to ``C:\x``.

    The Rust supervisor canonicalizes paths with ``std::fs::canonicalize``, which RETURNS
    extended-length paths on Windows — so every ``__file__``-derived path in the spawned shim
    carries ``\\?\``, while env-derived paths (%LOCALAPPDATA%) do not. Python's ``realpath``
    PRESERVES the prefix when the input has it, so comparing the two unstripped silently
    DISARMED the self-install collision guard: the first-launch sweep then deleted the live
    install's ``bin\`` (bundled exiftool/mediainfo) as a "legacy leftover" — hit in the field
    and by the user-flow BVT on 2026-07-11."""
    if s.startswith(_EXT_UNC_PREFIX):
        return "\\\\" + s[len(_EXT_UNC_PREFIX):]
    if s.startswith(_EXT_PREFIX):
        return s[len(_EXT_PREFIX):]
    return s


def _real(path: Path) -> Path:
    """Fully-resolved absolute path (follows symlinks on existing components, normalizes ``..``).
    Unlike provision's ``abspath`` (which deliberately does NOT follow symlinks), migration
    uses realpath so a legacy dir that is actually a SYMLINK pointing outside its root is detected
    and refused rather than deleted-through. The extended-length prefix is stripped FIRST (see
    ``_strip_extended_prefix``) so supervisor-spawned ``\\?\``-prefixed paths compare equal to
    their plain forms."""
    return Path(os.path.realpath(_strip_extended_prefix(os.fspath(path))))


def _strictly_within(child: Path, parent: Path) -> bool:
    """True iff ``child`` resolves to a path strictly BELOW ``parent`` (never equal — that would be
    a wholesale removal of the root)."""
    rc, rp = _real(child), _real(parent)
    if rc == rp:
        return False
    try:
        rc.relative_to(rp)
        return True
    except ValueError:
        return False


def _overlaps_protected(path: Path, protected: tuple[Path, ...]) -> bool:
    """True iff ``path`` resolves to, or inside, any protected (DataDir) root — the DataDir guard."""
    rpath = _real(path)
    for prot in protected:
        rprot = _real(prot)
        if rpath == rprot:
            return True
        try:
            rpath.relative_to(rprot)
            return True
        except ValueError:
            continue
    return False


# ── pure enumerator ───────────────────────────────────────────────────────────


def plan_migration(
    *,
    platform: str | None = None,
    localappdata: Path | str | None = None,
    appdata: Path | str | None = None,
    userprofile: Path | str | None = None,
    home: Path | str | None = None,
    self_install_dir: Path | str | None = None,
) -> MigrationPlan:
    """Enumerate the legacy artifacts to remove — PURE (no side effects). Injectable roots let the
    offline suite drive every platform + adversarial case (DataDir-lookalike, symlink escape,
    partial legacy) without a real machine.

    ``self_install_dir`` is the running Tauri app's install dir (``<Resources>`` == $INSTDIR on
    Windows). When the legacy AppDir root coincides with it, the ``bin``/``backend`` children are
    the LIVE new install and are refused (see :data:`_TAURI_LIVE_CHILDREN`). ``None`` (the NSIS
    PREINSTALL case, pre-extraction) keeps the full legacy allowlist."""
    plat = platform if platform is not None else sys.platform
    if plat.startswith("win") or plat == "nt":
        return _plan_windows(
            localappdata=_coerce(localappdata),
            appdata=_coerce(appdata),
            userprofile=_coerce(userprofile),
            self_install_dir=_coerce(self_install_dir),
        )
    if plat == "darwin":
        return _plan_macos(home=_coerce(home))
    # Linux/other: the legacy shell path is retained (architecture §9) — nothing to migrate.
    return MigrationPlan(platform=plat)


def _self_install_dir() -> Path:
    """The running Tauri app's install dir inferred from this module's own location
    (``<Resources>/backend/app/migration.py`` → ``<Resources>`` == $INSTDIR on Windows)."""
    return Path(__file__).resolve().parents[2]


def _coerce(value: Path | str | None) -> Path | None:
    return Path(value) if value is not None else None


def _classify_fs(
    candidates: list[Path],
    *,
    allowed_root: Path,
    protected: tuple[Path, ...],
    is_dir_target: bool,
    skipped: list[SkippedTarget],
    dirs: list[Path],
    files: list[Path],
) -> None:
    """Route each candidate to the safe bucket or ``skipped``, enforcing: strictly-within the
    allowed root AND outside every protected root. Missing paths still route to the safe bucket
    (the executor no-ops them) — enumeration is path-shape logic, not a filesystem probe."""
    for cand in candidates:
        if not _strictly_within(cand, allowed_root):
            skipped.append(SkippedTarget(cand, f"resolves outside allowed root {allowed_root}"))
            continue
        if _overlaps_protected(cand, protected):
            skipped.append(SkippedTarget(cand, "resolves to or inside the DataDir (protected)"))
            continue
        (dirs if is_dir_target else files).append(cand)


def _plan_windows(
    *,
    localappdata: Path | None,
    appdata: Path | None,
    userprofile: Path | None,
    self_install_dir: Path | None = None,
) -> MigrationPlan:
    skipped: list[SkippedTarget] = []
    dirs: list[Path] = []
    files: list[Path] = []
    allowed_roots: list[Path] = []

    protected: tuple[Path, ...] = ()
    if userprofile is not None:
        protected = (userprofile / _DATADIR_NAME,)  # %USERPROFILE%\MediaSearchAgent — NEVER touch

    appdir_root = (localappdata / _APPDIR_NAME) if localappdata is not None else None
    start_menu: Path | None = None
    path_entry: Path | None = None

    # Collision guard: when the legacy root IS the running Tauri install dir, drop the LIVE children.
    is_self_install = (
        self_install_dir is not None
        and appdir_root is not None
        and _real(appdir_root) == _real(self_install_dir)
    )
    appdir_dirs = _APPDIR_DIRS
    if is_self_install:
        appdir_dirs = tuple(d for d in _APPDIR_DIRS if d not in _TAURI_LIVE_CHILDREN)
        for live in _TAURI_LIVE_CHILDREN:
            if live in _APPDIR_DIRS:
                skipped.append(SkippedTarget(
                    appdir_root / live, "live Tauri install resource (running app dir) — not a legacy leftover"
                ))

    if appdir_root is not None:
        # DataDir guard, strongest form: if the AppDir root is (or is inside) the DataDir — a
        # mis-wired root — refuse the ENTIRE AppDir enumeration rather than enumerate DataDir children.
        if _overlaps_protected(appdir_root, protected):
            skipped.append(SkippedTarget(appdir_root, "AppDir root overlaps the DataDir — refusing all AppDir removals"))
        else:
            allowed_roots.append(appdir_root)
            _classify_fs(
                [appdir_root / rel for rel in appdir_dirs],
                allowed_root=appdir_root, protected=protected, is_dir_target=True,
                skipped=skipped, dirs=dirs, files=files,
            )
            _classify_fs(
                [appdir_root / rel for rel in _APPDIR_FILES],
                allowed_root=appdir_root, protected=protected, is_dir_target=False,
                skipped=skipped, dirs=dirs, files=files,
            )
            # The PATH entry (…\bin) is only stripped when bin\ is a legacy leftover, not the live install.
            if not is_self_install:
                path_entry = appdir_root / _PATH_ENTRY_REL

    if appdata is not None:
        programs_root = appdata.joinpath(*_START_MENU_REL[:-1])  # ...\Start Menu\Programs
        shortcut = appdata.joinpath(*_START_MENU_REL)
        if _strictly_within(shortcut, programs_root) and not _overlaps_protected(shortcut, protected):
            allowed_roots.append(programs_root)
            dirs.append(shortcut)
            start_menu = shortcut
        else:  # pragma: no cover — fixed relative path, always safe
            skipped.append(SkippedTarget(shortcut, "start-menu shortcut failed containment check"))

    return MigrationPlan(
        platform="win32",
        allowed_roots=tuple(allowed_roots),
        protected_roots=protected,
        directories=tuple(dirs),
        files=tuple(files),
        scheduled_task=_SCHEDULED_TASK,
        run_value=(_RUN_SUBKEY, _RUN_VALUE_NAME),
        path_entry=path_entry,
        stop_processes=_STOP_PROCESSES,
        skipped=tuple(skipped),
    )


def _plan_macos(*, home: Path | None) -> MigrationPlan:
    if home is None:
        return MigrationPlan(platform="darwin")
    skipped: list[SkippedTarget] = []
    launch_agents: list[Path] = []
    files: list[Path] = []
    allowed_roots: list[Path] = []

    # DataDir on macOS: ~/Library/Application Support/MediaSearchAgent — NEVER touched.
    protected = (home / "Library" / "Application Support" / _DATADIR_NAME,)

    la_root = home / "Library" / "LaunchAgents"
    plist = la_root / _MACOS_LAUNCH_AGENT
    if _strictly_within(plist, la_root) and not _overlaps_protected(plist, protected):
        allowed_roots.append(la_root)
        launch_agents.append(plist)
    else:  # pragma: no cover
        skipped.append(SkippedTarget(plist, "LaunchAgent plist failed containment check"))

    bin_root = home / ".local" / "bin"
    launcher = home.joinpath(*_MACOS_LAUNCHER_REL)
    if _strictly_within(launcher, bin_root) and not _overlaps_protected(launcher, protected):
        allowed_roots.append(bin_root)
        files.append(launcher)
    else:  # pragma: no cover
        skipped.append(SkippedTarget(launcher, "msa launcher failed containment check"))

    return MigrationPlan(
        platform="darwin",
        allowed_roots=tuple(allowed_roots),
        protected_roots=protected,
        files=tuple(files),
        launch_agents=tuple(launch_agents),
        skipped=tuple(skipped),
    )


# ── executor (re-checks invariants; best-effort; idempotent) ──────────────────


def _assert_safe(path: Path, plan: MigrationPlan) -> None:
    """Re-verify — at delete time — that ``path`` sits strictly within one of the plan's allowed
    roots and outside every protected root. A plan only ever contains safe targets; this is the
    belt to that suspenders, so a future enumerator bug can never delete outside the allowlist."""
    if _overlaps_protected(path, plan.protected_roots):
        raise MigrationError(f"refusing to remove {path}: resolves to or inside the DataDir")
    if not any(_strictly_within(path, root) for root in plan.allowed_roots):
        raise MigrationError(f"refusing to remove {path}: outside every allowed root {plan.allowed_roots}")


def _remove_path(path: Path) -> bool:
    """Remove a file/dir/symlink idempotently. A symlink is unlinked as a LINK (never
    rmtree'd-through — that could reach outside the root). Returns True iff something was removed.

    Deletion errors are allowed to RAISE (``rmtree`` runs WITHOUT ``ignore_errors``; ``unlink``
    raises natively): a genuine perms/lock failure must surface so :func:`execute_migration` reports
    ``could not remove`` rather than a false ``removed``. The target has already passed
    :func:`_assert_safe` (allowlist + realpath-containment + DataDir-never); this changes only
    honesty-of-reporting on an already-vetted path, never what is eligible for deletion."""
    if path.is_symlink():
        path.unlink()
        return True
    if not path.exists():
        return False
    if path.is_dir():
        shutil.rmtree(path)  # no ignore_errors: a failed delete must raise, not report success
        return True
    path.unlink()
    return True


def execute_migration(
    plan: MigrationPlan,
    *,
    run=subprocess.run,
    remover=_remove_path,
    on_event=None,
    strip_path_entry=None,
    delete_run_value=None,
) -> list[str]:
    """Execute ``plan`` — best-effort, idempotent. Every filesystem delete re-passes
    :func:`_assert_safe`. Injectable ``run`` / ``remover`` / registry+PATH hooks keep it fully
    offline-testable. Returns a list of human-readable event strings (also fed to ``on_event``)."""
    events: list[str] = []

    def _emit(msg: str) -> None:
        events.append(msg)
        if on_event is not None:
            try:
                on_event(msg)
            except Exception:
                pass

    # 1) Stop legacy processes first so their files unlock (WIN-005: a running tray exe is locked).
    for name in plan.stop_processes:
        try:
            if os.name == "nt":
                run(["taskkill", "/IM", name, "/F"], capture_output=True, check=False)
                _emit(f"stopped legacy process {name}")
        except Exception as exc:  # pragma: no cover — best-effort
            _emit(f"could not stop {name}: {exc}")

    # 2) Filesystem removals (dirs, files, plists) — each re-checked for safety.
    for plist in plan.launch_agents:
        try:
            if sys.platform == "darwin":
                run(["launchctl", "unload", str(plist)], capture_output=True, check=False)
        except Exception:  # pragma: no cover
            pass
    for path in plan.fs_targets():
        try:
            _assert_safe(path, plan)
        except MigrationError as exc:
            _emit(f"SKIP (unsafe) {path}: {exc}")
            continue
        try:
            if remover(path):
                _emit(f"removed {path}")
        except OSError as exc:  # locked / permission — best-effort, don't abort the sweep
            _emit(f"could not remove {path}: {exc}")

    # 3) Scheduled task.
    if plan.scheduled_task:
        try:
            if os.name == "nt":
                run(["schtasks", "/Delete", "/TN", plan.scheduled_task, "/F"], capture_output=True, check=False)
                _emit(f"removed scheduled task {plan.scheduled_task}")
        except Exception as exc:  # pragma: no cover
            _emit(f"could not remove scheduled task: {exc}")

    # 4) HKCU Run value.
    if plan.run_value is not None:
        subkey, value_name = plan.run_value
        try:
            if delete_run_value is not None:
                delete_run_value(subkey, value_name)
                _emit(f"removed HKCU Run value {value_name}")
            elif os.name == "nt":
                _delete_hkcu_run_value(subkey, value_name)
                _emit(f"removed HKCU Run value {value_name}")
        except Exception as exc:  # pragma: no cover
            _emit(f"could not remove Run value {value_name}: {exc}")

    # 5) User-PATH entry.
    if plan.path_entry is not None:
        try:
            if strip_path_entry is not None:
                strip_path_entry(plan.path_entry)
                _emit(f"removed PATH entry {plan.path_entry}")
            elif os.name == "nt":
                _strip_user_path_entry(plan.path_entry)
                _emit(f"removed PATH entry {plan.path_entry}")
        except Exception as exc:  # pragma: no cover
            _emit(f"could not strip PATH entry: {exc}")

    return events


def _delete_hkcu_run_value(subkey: str, value_name: str) -> None:  # pragma: no cover — Windows-only
    """Delete an HKCU Run value via the stdlib ``winreg`` (no-op if already absent)."""
    import winreg  # type: ignore

    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, subkey, 0, winreg.KEY_SET_VALUE) as key:
            winreg.DeleteValue(key, value_name)
    except FileNotFoundError:
        pass


def _strip_user_path_entry(entry: Path) -> None:  # pragma: no cover — Windows-only
    """Remove ``entry`` from the persistent HKCU\\Environment ``Path`` (idempotent)."""
    import winreg  # type: ignore

    target = os.path.normcase(str(entry).rstrip("\\"))
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment", 0, winreg.KEY_READ | winreg.KEY_SET_VALUE) as key:
        try:
            current, kind = winreg.QueryValueEx(key, "Path")
        except FileNotFoundError:
            return
        parts = [p for p in str(current).split(";") if os.path.normcase(p.rstrip("\\")) != target]
        winreg.SetValueEx(key, "Path", 0, kind, ";".join(parts))


# ── first-run belt-and-braces entry (called by the shim) ──────────────────────


def sweep_legacy_install(env: dict[str, str] | None = None, *, log=None, run=subprocess.run) -> MigrationPlan:
    """Belt-and-braces first-run migration: build the plan from real platform roots and execute it
    best-effort. Idempotent, so re-running on a subsequent launch is a cheap no-op. Never raises —
    a migration failure must never block the app launch (the NSIS PREINSTALL hook is the primary
    path on Windows; this covers direct-download installs + the macOS artifacts NSIS can't reach).
    Returns the plan (for logging/tests)."""
    environ = os.environ if env is None else env
    plan = plan_migration(
        localappdata=environ.get("LOCALAPPDATA"),
        appdata=environ.get("APPDATA"),
        userprofile=environ.get("USERPROFILE"),
        home=Path.home(),
        self_install_dir=_self_install_dir(),  # guard: never delete the live Tauri install's bin\/backend\
    )
    if plan.is_empty():
        return plan

    def _log(msg: str) -> None:
        if log is not None:
            try:
                log(msg)
            except Exception:
                pass

    _log(f"legacy migration: {len(plan.fs_targets())} fs target(s), "
         f"{len(plan.skipped)} skipped-for-safety")
    execute_migration(plan, run=run, on_event=_log)
    return plan


def run_first_run_sweep(
    env: dict[str, str] | None = None, *, log=None, run=subprocess.run
) -> MigrationPlan | None:
    """The guarded first-run legacy sweep shared by BOTH provisioning entries — the GUI shim
    (``app.__main__.main``) and the headless entry (``app.provision.headless_main``) — so the two
    first-run paths run the IDENTICAL sweep (allowlist / realpath-containment / DataDir-never /
    self-install collision guards all intact, via :func:`sweep_legacy_install`). ``log`` is a plain
    ``str -> None`` message sink. Never raises — a migration hiccup must NEVER block the app launch.
    Returns the plan, or ``None`` if the sweep itself failed unexpectedly."""
    try:
        return sweep_legacy_install(env, log=log, run=run)
    except Exception:  # pragma: no cover — defensive; the sweep is best-effort internally
        if log is not None:
            try:
                log("legacy migration sweep failed (non-fatal)")
            except Exception:
                pass
        return None
