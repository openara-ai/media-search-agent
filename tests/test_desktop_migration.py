"""Offline unit tests for the legacy-install migration (src-tauri/backend/app/migration.py, M-7/S-3).

The migration removes the legacy shell-bundle runtime (repo/.venv/uv/bin/Cache-models/logs +
Start-Menu shortcut + scheduled task + HKCU Run value + PATH entry on Windows; LaunchAgent plist +
~/.local/bin/msa on macOS) WITHOUT ever touching the ADR-009 DataDir. The core is a PURE
enumerator; these tests drive every platform + adversarial case (DataDir-lookalike, symlink
escape, partial legacy, idempotency) offline via injectable roots, and also exercise the executor
over tmp trees to prove the delete allowlist + DataDir-never-touched invariants hold end-to-end.
"""

import os
import sys
from pathlib import Path

import pytest

_SHIM_ROOT = Path(__file__).resolve().parents[1] / "src-tauri" / "backend"
if str(_SHIM_ROOT) not in sys.path:
    sys.path.insert(0, str(_SHIM_ROOT))

from app import migration  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parents[1]
NSH = REPO_ROOT / "packaging" / "windows" / "msa-installer-hooks.nsh"


# ── Windows enumeration: the exact allowlist ──────────────────────────────────


def _win_plan(tmp_path: Path) -> migration.MigrationPlan:
    (tmp_path / "LOCALAPPDATA").mkdir()
    (tmp_path / "APPDATA").mkdir()
    (tmp_path / "USERPROFILE").mkdir()
    return migration.plan_migration(
        platform="win32",
        localappdata=tmp_path / "LOCALAPPDATA",
        appdata=tmp_path / "APPDATA",
        userprofile=tmp_path / "USERPROFILE",
    )


def test_windows_plan_enumerates_exactly_the_allowlist(tmp_path):
    plan = _win_plan(tmp_path)
    root = tmp_path / "LOCALAPPDATA" / "MediaSearchAgent"

    expected_dirs = {root / d for d in ("repo", ".venv", "uv", "bin", "Cache/models", "logs")}
    expected_files = {root / f for f in ("start.ps1", "stop.ps1", "version.txt")}
    shortcut = tmp_path / "APPDATA" / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "Media Search Agent"

    assert set(plan.directories) == expected_dirs | {shortcut}
    assert set(plan.files) == expected_files
    assert plan.scheduled_task == "MediaSearchAgent"
    assert plan.run_value == (r"Software\Microsoft\Windows\CurrentVersion\Run", "MediaSearchAgent")
    assert plan.path_entry == root / "bin"
    assert plan.stop_processes == ("MediaSearchAgentTray.exe",)
    assert plan.skipped == ()


def test_windows_plan_never_targets_the_appdir_root_itself(tmp_path):
    """No wholesale RMDir of %LOCALAPPDATA%\\MediaSearchAgent — only named children."""
    plan = _win_plan(tmp_path)
    root = tmp_path / "LOCALAPPDATA" / "MediaSearchAgent"
    for target in plan.fs_targets():
        assert migration._real(target) != migration._real(root), (
            f"{target} must not be the AppDir root itself"
        )
        # Every AppDir child target is strictly below the root.
        if str(root) in str(target):
            assert migration._strictly_within(target, root)


def test_windows_model_cache_target_is_nested_not_whole_cache(tmp_path):
    """Dev decision (b): delete Cache\\models\\, NOT the whole Cache\\ tree."""
    plan = _win_plan(tmp_path)
    root = tmp_path / "LOCALAPPDATA" / "MediaSearchAgent"
    assert root / "Cache" / "models" in set(plan.directories)
    assert root / "Cache" not in set(plan.directories)


# ── self-install collision guard (Tauri $INSTDIR == legacy AppDir root) ───────


def test_windows_self_install_excludes_live_children(tmp_path):
    """Tauri NSIS installs to %LOCALAPPDATA%\\MediaSearchAgent (== legacy root) and stages bin\\ +
    backend\\ there. When the legacy root IS the running install dir, those live children are
    refused so the belt-and-braces sweep never deletes the running app's uv/exiftool/shim."""
    localappdata = tmp_path / "LOCALAPPDATA"
    root = localappdata / "MediaSearchAgent"
    root.mkdir(parents=True)
    plan = migration.plan_migration(
        platform="win32",
        localappdata=localappdata,
        appdata=tmp_path / "APPDATA",
        userprofile=tmp_path / "USERPROFILE",
        self_install_dir=root,  # legacy root == running Tauri install dir
    )
    assert (root / "bin") not in set(plan.directories), "live bin\\ must not be a delete target"
    # logs\ and Cache\models\ are ALSO live in the Tauri layout — the ADR-009 log dir being
    # written right now, and the setup flow's model cache — never delete targets in self-install.
    assert (root / "logs") not in set(plan.directories), "live logs\\ must not be a delete target"
    assert (root / "Cache" / "models") not in set(plan.directories), "live model cache must not be a delete target"
    # The other legacy children are still removed (legacy-only names; the new runtime lives
    # under the identifier-keyed app-private dir).
    assert (root / "repo") in set(plan.directories)
    assert (root / ".venv") in set(plan.directories)
    assert (root / "uv") in set(plan.directories)
    # The PATH entry (…\bin) is not stripped when bin is the live install.
    assert plan.path_entry is None
    assert any("live Tauri install resource" in s.reason for s in plan.skipped)


def test_windows_self_install_guard_survives_extended_length_prefix(tmp_path):
    r"""The Rust supervisor spawns the shim with \\?\-prefixed (extended-length) paths — the
    output of std::fs::canonicalize — so the shim's __file__-derived self_install_dir carries
    the prefix while the env-derived legacy root does not. The guard must still recognize them
    as the SAME directory. Regression for the 2026-07-11 field + BVT failure: the disarmed
    guard deleted the live install's bin\ (bundled exiftool/mediainfo) on real first launches."""
    localappdata = tmp_path / "LOCALAPPDATA"
    root = localappdata / "MediaSearchAgent"
    root.mkdir(parents=True)
    plan = migration.plan_migration(
        platform="win32",
        localappdata=localappdata,
        appdata=tmp_path / "APPDATA",
        userprofile=tmp_path / "USERPROFILE",
        self_install_dir=Path("\\\\?\\" + str(root)),  # what the supervisor-spawned shim sees
    )
    assert (root / "bin") not in set(plan.directories), \
        "extended-length self_install_dir must not disarm the collision guard"
    assert any("live Tauri install resource" in s.reason for s in plan.skipped)


def test_strip_extended_prefix_forms():
    assert migration._strip_extended_prefix("\\\\?\\C:\\Users\\x") == "C:\\Users\\x"
    assert migration._strip_extended_prefix("\\\\?\\UNC\\srv\\share\\x") == "\\\\srv\\share\\x"
    assert migration._strip_extended_prefix("C:\\Users\\x") == "C:\\Users\\x"  # plain: untouched
    assert migration._strip_extended_prefix("/posix/path") == "/posix/path"


def test_windows_non_self_install_still_removes_bin(tmp_path):
    """When the new install lives elsewhere, legacy bin\\ + its PATH entry ARE removed (NSIS-parity
    full allowlist)."""
    localappdata = tmp_path / "LOCALAPPDATA"
    root = localappdata / "MediaSearchAgent"
    plan = migration.plan_migration(
        platform="win32", localappdata=localappdata,
        appdata=tmp_path / "APPDATA", userprofile=tmp_path / "USERPROFILE",
        self_install_dir=tmp_path / "elsewhere",
    )
    assert (root / "bin") in set(plan.directories)
    assert plan.path_entry == root / "bin"


def test_sweep_never_deletes_the_running_app_bin(tmp_path):
    """End-to-end safety: seed a live bin\\ at the shim's own install root; the sweep (which passes
    self_install_dir=_self_install_dir()) must leave it intact."""
    real_root = migration._self_install_dir()
    # Only meaningful if the module resolves to a MediaSearchAgent-named root; simulate via override.
    localappdata = tmp_path / "LOCALAPPDATA"
    root = localappdata / "MediaSearchAgent"
    (root / "bin").mkdir(parents=True)
    (root / "bin" / "uv.exe").write_text("LIVE")
    (root / "repo").mkdir()
    plan = migration.plan_migration(
        platform="win32", localappdata=localappdata,
        appdata=tmp_path / "APPDATA", userprofile=tmp_path / "USERPROFILE",
        self_install_dir=root,
    )
    migration.execute_migration(plan, run=lambda *a, **k: None,
                                delete_run_value=lambda *a: None, strip_path_entry=lambda *a: None)
    assert (root / "bin" / "uv.exe").read_text() == "LIVE", "live install bin must survive"
    assert not (root / "repo").exists(), "legacy repo still removed"
    assert real_root is not None  # sanity: the helper resolves


# ── the DataDir is NEVER in the plan ──────────────────────────────────────────


def test_windows_datadir_is_never_in_the_plan(tmp_path):
    plan = _win_plan(tmp_path)
    datadir = tmp_path / "USERPROFILE" / "MediaSearchAgent"
    assert datadir in plan.protected_roots
    for target in plan.fs_targets():
        assert not migration._overlaps_protected(target, plan.protected_roots), (
            f"{target} overlaps the protected DataDir"
        )
    assert plan.path_entry is None or not migration._overlaps_protected(
        plan.path_entry, plan.protected_roots
    )


def test_datadir_lookalike_appdir_pointing_at_datadir_is_refused(tmp_path):
    """Adversarial: if LOCALAPPDATA is mis-wired so the AppDir root resolves INTO the DataDir,
    the whole AppDir enumeration is refused rather than enumerating DataDir children."""
    shared = tmp_path / "shared"
    shared.mkdir()
    # userprofile == localappdata → AppDir root (localappdata/MediaSearchAgent) == DataDir.
    plan = migration.plan_migration(
        platform="win32",
        localappdata=shared,
        appdata=tmp_path / "APPDATA",
        userprofile=shared,
    )
    root = shared / "MediaSearchAgent"
    # No AppDir file/dir target survived.
    for target in plan.directories + plan.files:
        assert root not in target.parents and target != root
    assert any("overlaps the DataDir" in s.reason for s in plan.skipped)


# ── symlink / .. escape refused ───────────────────────────────────────────────


@pytest.mark.skipif(os.name == "nt", reason="POSIX symlink semantics")
def test_symlink_escape_is_refused_not_followed(tmp_path):
    """A legacy 'logs' that is actually a symlink pointing OUTSIDE the AppDir root must be refused,
    never deleted-through (realpath containment)."""
    localappdata = tmp_path / "LOCALAPPDATA"
    root = localappdata / "MediaSearchAgent"
    root.mkdir(parents=True)
    outside = tmp_path / "precious_outside"
    outside.mkdir()
    (root / "logs").symlink_to(outside, target_is_directory=True)

    plan = migration.plan_migration(
        platform="win32",
        localappdata=localappdata,
        appdata=tmp_path / "APPDATA",
        userprofile=tmp_path / "USERPROFILE",
    )
    assert (root / "logs") not in set(plan.directories), "escaping symlink must not be a delete target"
    assert any("outside allowed root" in s.reason for s in plan.skipped)


@pytest.mark.skipif(os.name == "nt", reason="POSIX symlink semantics")
def test_symlink_escape_target_survives_execution(tmp_path):
    localappdata = tmp_path / "LOCALAPPDATA"
    root = localappdata / "MediaSearchAgent"
    root.mkdir(parents=True)
    outside = tmp_path / "precious_outside"
    outside.mkdir()
    (outside / "keep.txt").write_text("do not delete")
    (root / "logs").symlink_to(outside, target_is_directory=True)

    plan = migration.plan_migration(
        platform="win32",
        localappdata=localappdata,
        appdata=tmp_path / "APPDATA",
        userprofile=tmp_path / "USERPROFILE",
    )
    migration.execute_migration(plan, run=lambda *a, **k: None,
                                delete_run_value=lambda *a: None, strip_path_entry=lambda *a: None)
    assert (outside / "keep.txt").exists(), "content outside the root must survive"


# ── executor: deletes only the allowlist, idempotent ──────────────────────────


def _seed_windows_tree(tmp_path: Path) -> tuple[Path, Path]:
    localappdata = tmp_path / "LOCALAPPDATA"
    root = localappdata / "MediaSearchAgent"
    for d in ("repo", ".venv", "uv", "bin", "Cache/models", "logs"):
        (root / d).mkdir(parents=True)
        (root / d / "f.txt").write_text("x")
    (root / "Cache" / "other").mkdir(parents=True)  # NOT in allowlist — must survive
    (root / "Cache" / "other" / "keep.txt").write_text("keep")
    for f in ("start.ps1", "stop.ps1", "version.txt"):
        (root / f).write_text("x")
    # DataDir with real user data — must survive byte-identical.
    datadir = tmp_path / "USERPROFILE" / "MediaSearchAgent"
    (datadir / "index").mkdir(parents=True)
    (datadir / "index" / "media.sqlite").write_text("USER DATA")
    (datadir / "config.yaml").write_text("media_sources: []")
    return root, datadir


def test_executor_removes_allowlist_and_preserves_datadir_and_unlisted(tmp_path):
    (tmp_path / "APPDATA").mkdir()
    root, datadir = _seed_windows_tree(tmp_path)
    plan = migration.plan_migration(
        platform="win32",
        localappdata=tmp_path / "LOCALAPPDATA",
        appdata=tmp_path / "APPDATA",
        userprofile=tmp_path / "USERPROFILE",
    )
    events = migration.execute_migration(
        plan, run=lambda *a, **k: None,
        delete_run_value=lambda *a: None, strip_path_entry=lambda *a: None,
    )

    # Allowlist gone.
    for d in ("repo", ".venv", "uv", "bin", "Cache/models", "logs"):
        assert not (root / d).exists(), f"{d} should be removed"
    for f in ("start.ps1", "stop.ps1", "version.txt"):
        assert not (root / f).exists(), f"{f} should be removed"
    # Unlisted Cache sibling survives (proves we delete Cache/models, not Cache).
    assert (root / "Cache" / "other" / "keep.txt").read_text() == "keep"
    # DataDir untouched, byte-identical.
    assert (datadir / "index" / "media.sqlite").read_text() == "USER DATA"
    assert (datadir / "config.yaml").read_text() == "media_sources: []"
    assert any("removed" in e for e in events)


def test_executor_is_idempotent(tmp_path):
    (tmp_path / "APPDATA").mkdir()
    _seed_windows_tree(tmp_path)
    kwargs = dict(run=lambda *a, **k: None, delete_run_value=lambda *a: None, strip_path_entry=lambda *a: None)
    plan = migration.plan_migration(
        platform="win32", localappdata=tmp_path / "LOCALAPPDATA",
        appdata=tmp_path / "APPDATA", userprofile=tmp_path / "USERPROFILE",
    )
    migration.execute_migration(plan, **kwargs)
    # Second run over the now-clean tree: no FILESYSTEM removals, no error. (The scheduled-task /
    # Run-value / PATH-entry ops are idempotent OS calls that "attempt" every run — the invariant
    # is that no on-disk target is deleted twice.)
    events2 = migration.execute_migration(plan, **kwargs)
    fs_removed = {f"removed {t}" for t in plan.fs_targets()}
    assert not (fs_removed & set(events2)), "second run must not delete any filesystem target again"


def test_partial_legacy_only_removes_present_paths(tmp_path):
    """Only some legacy artifacts present — the executor no-ops missing ones without error."""
    (tmp_path / "APPDATA").mkdir()
    localappdata = tmp_path / "LOCALAPPDATA"
    root = localappdata / "MediaSearchAgent"
    (root / ".venv").mkdir(parents=True)   # only the venv exists
    (root / "version.txt").write_text("v0.3.2")
    plan = migration.plan_migration(
        platform="win32", localappdata=localappdata,
        appdata=tmp_path / "APPDATA", userprofile=tmp_path / "USERPROFILE",
    )
    migration.execute_migration(
        plan, run=lambda *a, **k: None,
        delete_run_value=lambda *a: None, strip_path_entry=lambda *a: None,
    )
    assert not (root / ".venv").exists()
    assert not (root / "version.txt").exists()


# ── executor safety net: a smuggled unsafe target is refused ──────────────────


def test_executor_refuses_unsafe_target_even_if_smuggled_into_plan(tmp_path):
    """Defense in depth: even if a target somehow bypassed the enumerator, the executor's
    _assert_safe re-check refuses anything outside the allowed roots."""
    root = tmp_path / "LOCALAPPDATA" / "MediaSearchAgent"
    root.mkdir(parents=True)
    outside = tmp_path / "outside" / "precious"
    outside.mkdir(parents=True)
    (outside / "keep.txt").write_text("keep")
    smuggled = migration.MigrationPlan(
        platform="win32",
        allowed_roots=(root,),
        protected_roots=(),
        directories=(outside,),  # NOT within root — must be refused at execute time
    )
    events = migration.execute_migration(
        smuggled, run=lambda *a, **k: None,
        delete_run_value=lambda *a: None, strip_path_entry=lambda *a: None,
    )
    assert (outside / "keep.txt").exists(), "unsafe smuggled target must not be deleted"
    assert any("SKIP (unsafe)" in e for e in events)


def test_failed_dir_delete_is_reported_as_failed_not_removed(tmp_path, monkeypatch):
    """Honest failure reporting (Copilot #3): a directory whose delete genuinely fails (perms/locks)
    must be reported ``could not remove <path>``, NEVER a false ``removed <path>``. Regression guard
    for the old ``shutil.rmtree(..., ignore_errors=True)`` that swallowed the error and then returned
    True, so ``execute_migration`` logged a delete that never happened."""
    (tmp_path / "APPDATA").mkdir()
    localappdata = tmp_path / "LOCALAPPDATA"
    root = localappdata / "MediaSearchAgent"
    venv = root / ".venv"
    venv.mkdir(parents=True)  # the only present legacy target
    (venv / "marker.txt").write_text("still here")

    def _locked_rmtree(_path, *a, **k):
        raise OSError("directory is locked by another process")

    monkeypatch.setattr(migration.shutil, "rmtree", _locked_rmtree)

    plan = migration.plan_migration(
        platform="win32", localappdata=localappdata,
        appdata=tmp_path / "APPDATA", userprofile=tmp_path / "USERPROFILE",
    )
    events = migration.execute_migration(
        plan, run=lambda *a, **k: None,
        delete_run_value=lambda *a: None, strip_path_entry=lambda *a: None,
    )

    assert any(f"could not remove {venv}" in e for e in events), \
        "a failed delete must be reported as 'could not remove', not silently succeed"
    assert not any(e == f"removed {venv}" for e in events), \
        "a failed delete must NEVER be reported as 'removed'"
    assert venv.exists(), "the delete genuinely failed, so the dir is still on disk"


# ── macOS enumeration ─────────────────────────────────────────────────────────


def test_macos_plan_enumerates_plist_and_launcher_only(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    plan = migration.plan_migration(platform="darwin", home=home)
    plist = home / "Library" / "LaunchAgents" / "ai.openara.mediasearchagent.plist"
    launcher = home / ".local" / "bin" / "msa"
    assert set(plan.launch_agents) == {plist}
    assert set(plan.files) == {launcher}
    # DataDir (Application Support) is protected and never targeted.
    datadir = home / "Library" / "Application Support" / "MediaSearchAgent"
    assert datadir in plan.protected_roots
    for target in plan.fs_targets():
        assert not migration._overlaps_protected(target, plan.protected_roots)


def test_macos_plan_does_not_target_the_app_bundle(tmp_path):
    """The .app is deliberately left to the installer/updater — never enumerated for deletion."""
    home = tmp_path / "home"
    home.mkdir()
    plan = migration.plan_migration(platform="darwin", home=home)
    for target in plan.fs_targets():
        assert not str(target).endswith("MediaSearchAgent.app")
        assert "Applications" not in target.parts


def test_macos_datadir_survives_execution(tmp_path):
    home = tmp_path / "home"
    (home / "Library" / "Application Support" / "MediaSearchAgent").mkdir(parents=True)
    (home / "Library" / "Application Support" / "MediaSearchAgent" / "config.yaml").write_text("keep")
    (home / "Library" / "LaunchAgents").mkdir(parents=True)
    (home / "Library" / "LaunchAgents" / "ai.openara.mediasearchagent.plist").write_text("<plist/>")
    (home / ".local" / "bin").mkdir(parents=True)
    (home / ".local" / "bin" / "msa").write_text("#!/bin/bash")

    plan = migration.plan_migration(platform="darwin", home=home)
    migration.execute_migration(plan, run=lambda *a, **k: None)
    assert not (home / "Library" / "LaunchAgents" / "ai.openara.mediasearchagent.plist").exists()
    assert not (home / ".local" / "bin" / "msa").exists()
    assert (home / "Library" / "Application Support" / "MediaSearchAgent" / "config.yaml").read_text() == "keep"


# ── Linux: nothing to migrate ─────────────────────────────────────────────────


def test_linux_plan_is_empty(tmp_path):
    plan = migration.plan_migration(platform="linux", home=tmp_path)
    assert plan.is_empty()


# ── sweep_legacy_install: never raises, idempotent, honours env ───────────────


def test_sweep_never_raises_on_missing_env(monkeypatch):
    # No LOCALAPPDATA/APPDATA/USERPROFILE + a linux platform → empty plan, no throw.
    monkeypatch.setattr(migration.sys, "platform", "linux")
    plan = migration.sweep_legacy_install(env={}, run=lambda *a, **k: None)
    assert plan.is_empty()


def test_sweep_builds_windows_plan_from_env(tmp_path, monkeypatch):
    monkeypatch.setattr(migration.sys, "platform", "win32")
    (tmp_path / "LOCALAPPDATA").mkdir()
    (tmp_path / "APPDATA").mkdir()
    (tmp_path / "USERPROFILE").mkdir()
    env = {
        "LOCALAPPDATA": str(tmp_path / "LOCALAPPDATA"),
        "APPDATA": str(tmp_path / "APPDATA"),
        "USERPROFILE": str(tmp_path / "USERPROFILE"),
    }
    logs: list[str] = []
    plan = migration.sweep_legacy_install(env=env, log=logs.append, run=lambda *a, **k: None)
    assert not plan.is_empty()
    assert plan.scheduled_task == "MediaSearchAgent"
    assert any("legacy migration" in m for m in logs)


# ── shim first-run wiring: never blocks launch ────────────────────────────────


def test_shim_sweep_helper_swallows_errors(monkeypatch):
    """The shim's belt-and-braces call must never raise — a migration hiccup can't block launch."""
    from app import __main__ as shim
    import logging

    def _boom(*_a, **_k):
        raise RuntimeError("boom")

    monkeypatch.setattr(migration, "sweep_legacy_install", _boom)
    shim._sweep_legacy_install(logging.getLogger("test-migration-sweep"))  # must not raise


# ── parity with the NSIS hook allowlist ───────────────────────────────────────


def test_nsh_and_python_share_the_identity_and_datadir_guard():
    """The project-owned NSIS hook and this module must agree on identity + the DataDir it must
    never touch — the two migration entry points (NSIS PREINSTALL, shim sweep) are kept in parity."""
    text = NSH.read_text(encoding="utf-8")
    assert "ai.openara.mediasearchagent" in text  # app-private identity for POSTUNINSTALL
    assert "MediaSearchAgent" in text
    # The hook documents that it never touches the DataDir.
    assert "%USERPROFILE%\\MediaSearchAgent" in text or "DataDir" in text
