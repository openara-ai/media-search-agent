"""First-run dependency provisioning for the bundled desktop sidecar (M-7/S-1 spec §1.2).

The vendored supervisor's ``provision_python`` (``main.rs``) creates only a **bare** venv
(``uv python install`` + ``uv venv``) — no ``uv pip install`` (a template gap, its
reference backend was pure-stdlib). MSA needs its whole stack, so this module — run by the
``app`` shim before importing ``msa_apps`` — installs it into the app-private venv on first
launch and no-ops on every launch after (a fingerprint marker). It keeps ``main.rs`` /
``src-tauri/`` unedited (ADR-012).

The logic is transplanted from the two platform installers:
  - Windows ``installer/windows-native/shell/install.ps1`` (``Test-NvidiaPresent``,
    ``Install-Torch``, ``Install-AppRuntime``, ``Install-FacenetPytorch``,
    ``Initialize-Config``, ``Test-VersionDowngrade``);
  - macOS/Linux ``installer/macos/shell/install.sh`` (``install_packages``, ``setup_config``).

Everything here is **stdlib-only and injectable** (``runner`` / ``nvidia_detector`` /
``on_stage``), so the suite exercises it **offline** — no real ``uv``, no network, no torch.

Install order (spec §1.2), all ``uv`` invocations pinned into the app-private dir:
  torch (+cu128 index-url on NVIDIA Windows, else CPU wheels) → ``-r`` the platform
  requirements (torch/torchvision/facenet/ranker lines stripped) → app ``--no-deps`` →
  ``facenet-pytorch --no-deps`` → the vendored ``msa_ranker-*.whl`` ``--no-deps`` if staged.
"""

from __future__ import annotations

import hashlib
import json
import os
import platform as _platform_mod
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

try:  # py3.11+
    import tomllib
except ModuleNotFoundError:  # pragma: no cover — MSA pins CPython 3.12
    tomllib = None  # type: ignore

# ── constants ────────────────────────────────────────────────────────────────
_MARKER_NAME = ".msa-deps.json"          # provision state for THIS venv (Tier-1: removed with it)
_PROJECT_DIRNAME = "msa"                  # <resources>/backend/msa (staged by stage-desktop-backend.sh)
_WHEELS_DIRNAME = "wheels"               # <resources>/backend/wheels (ranker wheel, private builds)
# cu128 wheels support Blackwell sm_120 (RTX 5000) and Ampere/Ada (install.ps1 $TorchIndexUrl).
_TORCH_CUDA_INDEX = "https://download.pytorch.org/whl/cu128"
_VERSION_FILE = "version.txt"            # DataDir/version.txt — downgrade guard (spec §1.2)
_CONFIG_TEMPLATE = "config.yaml.template"

# Stripped from the requirements file: torch/torchvision are installed first (so the resolver
# can't replace the gated wheel — install.ps1/sh rationale); facenet-pytorch + msa-ranker are
# installed separately --no-deps (ADR-011). Everything else stays.
_STRIP_RE = re.compile(r"^\s*(torch|torchvision|facenet[-_]pytorch|msa[-_]ranker)\b", re.IGNORECASE)

_VENV_BIN_DIRS = {"bin", "Scripts"}
_VENV_DIR = ".venv"

# Windows process-group flag (absent on POSIX; only referenced under os.name == "nt").
_CREATE_NEW_PROCESS_GROUP = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0x00000200)
# POSIX SIGTERM→SIGKILL grace when the reaper tears down an in-flight uv subtree.
_REAP_GRACE_S = 0.5


class ProvisionError(RuntimeError):
    """First-run provisioning failed — the shim surfaces this as the responder error state."""


# ── path resolution ──────────────────────────────────────────────────────────


def app_private_root(exe: str | os.PathLike[str] | None = None) -> Path | None:
    """The app-private data root the supervisor provisioned the venv into, inferred from the
    running interpreter (``sys.executable`` by default). Returns ``None`` when the interpreter
    isn't in the expected ``<root>/.venv/<bin>/python`` layout (system python under dev/CI).

    Normalizes **without following symlinks** (``abspath``, not ``resolve``): a uv
    venv's ``.venv/bin/python3`` is a symlink to the uv-managed base CPython, so ``resolve()``
    would chase it out of the ``.venv`` layout and mis-detect ``None``."""
    raw = os.fspath(exe) if exe is not None else sys.executable
    exe_path = Path(os.path.abspath(raw))
    bin_dir = exe_path.parent
    venv_dir = bin_dir.parent
    if bin_dir.name in _VENV_BIN_DIRS and venv_dir.name == _VENV_DIR:
        return venv_dir.parent
    return None


def resource_root() -> Path:
    """The bundle's ``<Resources>`` dir, from this shim's location
    (``<Resources>/backend/app/provision.py`` → ``<Resources>``)."""
    return Path(__file__).resolve().parent.parent.parent


def staged_project_dir() -> Path:
    """The staged MSA project tree (``pyproject.toml`` + ``src/`` + requirements + config
    template) → ``<Resources>/backend/msa`` (beside the ``app`` shim)."""
    return Path(__file__).resolve().parent.parent / _PROJECT_DIRNAME


def staged_wheels_dir() -> Path:
    """The staged optional wheels dir (ranker) → ``<Resources>/backend/wheels``."""
    return Path(__file__).resolve().parent.parent / _WHEELS_DIRNAME


def uv_binary(root: Path) -> Path:
    """The app-owned ``uv`` the supervisor extracted into ``<root>/bin`` (``extract_uv``)."""
    name = "uv.exe" if sys.platform.startswith("win") else "uv"
    return root / "bin" / name


def venv_python(root: Path) -> Path:
    """The venv interpreter (``Scripts/python.exe`` on Windows, ``bin/python3`` elsewhere)."""
    venv = root / _VENV_DIR
    if sys.platform.startswith("win"):
        return venv / "Scripts" / "python.exe"
    return venv / "bin" / "python3"


# ── torch variant gate (NVIDIA presence, not driver version — spec §1.2) ─────


def detect_nvidia(
    *, which=shutil.which, run=subprocess.run, platform: str | None = None
) -> bool:
    """True iff the machine has an NVIDIA discrete GPU. Replicates ``Test-NvidiaPresent``:
    a WMI ``Win32_VideoController`` query on Windows (filtering the RDP virtual / Microsoft
    Basic adapters), falling back to ``nvidia-smi`` on PATH. NVIDIA *presence* at install time
    gates cu128 vs CPU wheels — CUDA wheels on a non-NVIDIA box crash the Windows loader
    before Python runs (``feedback_torch_wheel_selection_vs_driver_check``); driver version is
    a runtime-only concern. Any failure ⇒ False (CPU is the safe default)."""
    plat = platform if platform is not None else os.name
    if plat == "nt":
        try:
            out = run(
                ["powershell", "-NoProfile", "-NonInteractive", "-Command",
                 "Get-CimInstance Win32_VideoController | Select-Object -ExpandProperty Name"],
                capture_output=True, text=True, timeout=20,
            )
            for line in (out.stdout or "").splitlines():
                up = line.upper()
                if "NVIDIA" in up and not re.search(r"VIRTUAL|REMOTE|BASIC", up):
                    return True
        except Exception:
            pass  # fall through to nvidia-smi
    return which("nvidia-smi") is not None


def torch_variant(env: dict[str, str], *, detector=detect_nvidia) -> str:
    """``"cu128"`` on NVIDIA Windows, else ``"cpu"``. An explicit ``MSA_TORCH_VARIANT``
    override wins (test/support hook)."""
    override = (env.get("MSA_TORCH_VARIANT") or "").strip().lower()
    if override in ("cpu", "cu128"):
        return override
    if os.name == "nt" and detector():
        return "cu128"
    return "cpu"


# ── system preflight: arch + free disk, BEFORE any download (spec §S-2.4) ────

_MIN_FREE_GB = 5.0  # matches Test-SystemRequirements / install.sh:1021 — the heavy first run
# stages ≈2 GB of torch + ≈1.5 GB of models; refuse below 5 GB rather than half-download.

# Per-stage installed footprint (GiB) subtracted from _MIN_FREE_GB when RESUMING a partial provision
# (finding B / Codex PR #164): the bytes a COMPLETED stage wrote are already on disk, so a resume
# must not re-charge the full fresh-install budget — that rejects a machine that barely fit the ≈2 GB
# torch install and now has adequate, but sub-5 GB, headroom for the small remainder. Estimates are
# deliberately CONSERVATIVE (below the real footprint) so the sized gate never UNDER-demands: a
# resume with genuinely-insufficient space for the remaining work still fails loud. Keys are the
# ledger step_ids (see ensure_dependencies); an unrecognized id contributes nothing.
_STAGE_INSTALLED_GB = {
    "torch": 2.0,     # cu128 wheels ≈2.5 GB installed; subtract a conservative 2.0
    "reqs": 1.0,      # numpy/scipy/opencv/faiss/transformers ≈1.5 GB; subtract 1.0
    "app": 0.05,
    "facenet": 0.05,
    "ranker": 0.05,
}
# Floor for a resume: even a near-done resume needs headroom for the last step + a safety margin, so
# the sized gate never drops below this no matter how much a partial ledger has already installed.
_MIN_RESUME_FREE_GB = 1.0


def machine_arch(*, machine: str | None = None) -> str:
    """Normalized (lowercased) CPU arch. Injectable so the suite exercises every platform
    offline (``platform.machine()`` returns e.g. ``arm64`` on Apple Silicon, ``AMD64`` on
    Windows x64)."""
    raw = machine if machine is not None else _platform_mod.machine()
    return (raw or "").strip().lower()


def _nearest_existing(path: Path) -> Path:
    """The path itself if it exists, else the closest existing ancestor — the app-private root
    may not exist yet on first run, and ``shutil.disk_usage`` needs a real directory."""
    p = Path(os.path.abspath(path))
    while not p.exists() and p != p.parent:
        p = p.parent
    return p


def free_gb(path: Path, *, usage=shutil.disk_usage) -> float:
    """Free space (GiB) on the volume that will hold ``path``. Fail-open (``inf``) when the
    volume can't be probed — mirrors the installers' warn-and-continue rather than blocking a
    launch on a stat error."""
    try:
        return usage(str(_nearest_existing(path))).free / (1024 ** 3)
    except OSError:
        return float("inf")


def preflight_system(
    root: Path | None = None,
    *,
    platform_name: str | None = None,
    machine: str | None = None,
    disk_usage=shutil.disk_usage,
    min_free_gb: float = _MIN_FREE_GB,
    check_disk: bool = True,
) -> None:
    """Cheap go/no-go gate run BEFORE any download (spec §S-2.4): a supported-arch guard
    (Apple Silicon on macOS, x86_64 on Windows — ports ``Test-WindowsArchitecture`` /
    ``install.sh``'s Intel-Mac stop) and a ≥5 GB free-disk check on the install volume (ports
    ``Test-SystemRequirements``). Raises :class:`ProvisionError` with an ACTIONABLE message so
    the shim surfaces it as the responder error state — no half-downloaded 2 GB torch on a
    machine that can't run it. Everything is injectable so the suite runs offline.

    The ARCH guard is UNCONDITIONAL — an unsupported CPU can never run MSA, hot path or not.
    The ≥5 GB DISK guard exists only to refuse a fresh/partial multi-GB install, so callers pass
    ``check_disk=False`` on the warm hot path (deps already provisioned, nothing to download):
    a since-shrunk disk must not block a launch that downloads nothing."""
    plat = platform_name if platform_name is not None else sys.platform
    arch = machine_arch(machine=machine)
    if plat == "darwin" and arch not in ("arm64", "aarch64"):
        raise ProvisionError(
            f"MediaSearchAgent requires an Apple Silicon (arm64) Mac; this machine reports "
            f"'{arch or 'unknown'}'. Intel Macs are not supported — only Apple Silicon bundles "
            "are published."
        )
    if plat.startswith("win") and arch not in ("amd64", "x86_64", "x86-64"):
        raise ProvisionError(
            f"MediaSearchAgent supports Windows x86_64 only; this machine reports "
            f"'{arch or 'unknown'}'. ARM64 Windows is not yet supported."
        )
    if not check_disk:
        return  # warm launch: deps already installed, nothing to download — skip the disk gate
    target = root if root is not None else (app_private_root() or Path.home())
    free = free_gb(target, usage=disk_usage)
    if free < min_free_gb:
        raise ProvisionError(
            f"Not enough free disk space: MediaSearchAgent needs at least {min_free_gb:.0f} GB "
            f"free to install Python and the ML libraries, but only {free:.1f} GB is available "
            "on the install volume. Free up space and relaunch."
        )


# ── requirements ─────────────────────────────────────────────────────────────


def requirements_file(project: Path) -> Path:
    """The platform requirements contract: ``requirements-windows.txt`` on Windows, else
    ``requirements-api.txt`` (the lean runtime set installers ship), falling back to
    ``requirements.txt``."""
    candidates = (
        ["requirements-windows.txt", "requirements-api.txt", "requirements.txt"]
        if os.name == "nt"
        else ["requirements-api.txt", "requirements.txt"]
    )
    for name in candidates:
        p = project / name
        if p.exists():
            return p
    return project / candidates[0]  # nonexistent → ensure_dependencies raises a clear error


def filter_requirements(text: str) -> str:
    """Drop torch/torchvision/facenet-pytorch/msa-ranker lines (installed separately), keeping
    comments and everything else. Matches the installers' strip-then-install-explicitly flow."""
    kept = [ln for ln in text.splitlines() if ln.lstrip().startswith("#") or not _STRIP_RE.match(ln)]
    return "\n".join(kept) + "\n"


# ── fingerprint marker ───────────────────────────────────────────────────────


def app_version(project: Path) -> str:
    """The staged app version from ``pyproject.toml`` (stamped from the git tag by
    stage-desktop-backend.sh). Falls back to ``0.0.0`` when unreadable."""
    pp = project / "pyproject.toml"
    if tomllib is None or not pp.exists():
        return "0.0.0"
    try:
        data = tomllib.loads(pp.read_text(encoding="utf-8"))
        return str(data.get("project", {}).get("version", "0.0.0"))
    except Exception:
        return "0.0.0"


def source_digest(project: Path) -> str:
    """Content hash of the staged ``*.py`` sources so a code-only rebuild (same version)
    reinstalls without a version bump. Deterministic; ``""`` when the project stages no
    sources (offline test bundle)."""
    files = sorted(p for p in project.rglob("*.py") if "__pycache__" not in p.parts and p.is_file())
    if not files:
        return ""
    h = hashlib.sha256()
    for p in files:
        h.update(str(p.relative_to(project)).encode())
        h.update(b"\0")
        h.update(p.read_bytes())
        h.update(b"\0")
    return h.hexdigest()


def fingerprint(reqs: bytes, variant: str, version: str, source: str = "") -> str:
    """Hash of the (filtered) requirements + torch variant + app version + source digest. A
    changed dep set, wheel variant, version, OR code yields a new fingerprint → reinstall;
    unchanged → the fast no-op path."""
    h = hashlib.sha256()
    for part in (variant, version):
        h.update(part.encode())
        h.update(b"\0")
    h.update(reqs)
    if source:
        h.update(b"\0")
        h.update(source.encode())
    return h.hexdigest()


def _read_marker(path: Path) -> str | None:
    """The all-done fingerprint (S-1 fast path): present ONLY once every install step succeeded.
    A partially-provisioned venv has a ``progress`` block but no top-level ``fingerprint``, so
    this returns ``None`` and the caller re-enters the install (resuming past finished steps)."""
    try:
        data = json.loads(path.read_text())
        return data.get("fingerprint") if isinstance(data, dict) else None
    except (OSError, ValueError):
        return None


def _read_progress(path: Path, fp: str) -> set[str]:
    """The set of install steps already completed for fingerprint ``fp`` — the kill-resume
    ledger (spec §S-2.2). Empty when the marker is absent/unreadable OR records a DIFFERENT
    fingerprint (a changed dep set / torch variant / app version invalidates partial progress →
    reinstall from scratch). A step lands here ONLY after its ``uv`` call returned ``rc==0``, so
    an interrupted step is always safely re-run (uv is idempotent)."""
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        return set()
    if not isinstance(data, dict):
        return set()
    prog = data.get("progress")
    if not isinstance(prog, dict) or prog.get("fingerprint") != fp:
        return set()
    done = prog.get("completed")
    return set(done) if isinstance(done, list) else set()


def _atomic_write_json(path: Path, payload: dict) -> None:
    """Write JSON via a temp file + ``os.replace`` so a crash mid-write can never leave a
    half-written (corrupt, unparseable) ledger — the resume path depends on it staying readable."""
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(payload, indent=2))
    os.replace(tmp, path)


def _record_stage(path: Path, fp: str, step_id: str, variant: str, version: str, completed: set[str]) -> None:
    """Persist that ``step_id`` finished (called only on ``rc==0``) so a kill before the WHOLE
    install completes resumes past it next launch. The top-level all-done ``fingerprint`` marker
    is still written only once every step succeeds (:func:`_write_marker`)."""
    completed.add(step_id)
    _atomic_write_json(path, {
        "torch_variant": variant,
        "app_version": version,
        "progress": {"fingerprint": fp, "completed": sorted(completed)},
    })


def _write_marker(path: Path, fp: str, variant: str, version: str) -> None:
    """The all-done marker (S-1 fast path), written only after every step succeeded. Drops the
    partial ``progress`` block — next launch matches the fingerprint and no-ops."""
    _atomic_write_json(path, {"fingerprint": fp, "torch_variant": variant, "app_version": version})


def _is_within(child: Path, parent: Path) -> bool:
    """True when ``child`` is at or under ``parent`` (normalized, symlink-agnostic — abspath not
    resolve)."""
    try:
        Path(os.path.abspath(child)).relative_to(Path(os.path.abspath(parent)))
        return True
    except ValueError:
        return False


def _assert_ledger_owned(marker: Path, root: Path, env: dict[str, str]) -> None:
    """Invariant (ADR-009/ADR-012): the provision ledger lives ONLY in the app-private runtime
    venv (``<root>/.venv/.msa-deps.json``) and NEVER under DataDir (config.yaml / index /
    thumbnails). Cheap guard so a future path-wiring bug can't ever strand provisioning state in
    durable user data — the ledger is disposable, Tier-1-with-the-venv."""
    expected = root / _VENV_DIR / _MARKER_NAME
    if os.path.abspath(marker) != os.path.abspath(expected):
        raise ProvisionError(
            f"provision ledger {marker} is not the app-private venv marker {expected}"
        )
    data_dir = (env.get("MSA_DATA_DIR") or "").strip()
    if data_dir and _is_within(marker, Path(data_dir)):
        raise ProvisionError(
            f"refusing to write the provision ledger under DataDir ({data_dir}) — it must live "
            "in the app-private runtime venv, never in durable user data"
        )


# ── uv command construction + env ────────────────────────────────────────────


def install_env(root: Path, base: dict[str, str]) -> dict[str, str]:
    """uv env mirroring the supervisor's app-owned discipline (``run_uv`` in main.rs): keep
    the CPython install + cache inside the app-private dir (Tier-1 uninstall), ignore uv config
    *files*, and suppress the ~/.local/bin launcher shim so the interpreter stays app-owned."""
    env = dict(base)
    env["UV_CACHE_DIR"] = str(root / "uv-cache")
    env["UV_PYTHON_INSTALL_DIR"] = str(root / "python")
    env["UV_NO_CONFIG"] = "1"
    env["UV_PYTHON_INSTALL_BIN"] = "0"
    return env


def torch_install_command(uv: Path, interp: Path, variant: str) -> list[str]:
    # ``-v`` so uv logs the per-wheel URLs it fetches (``No cache entry for: …/torch-*.whl``).
    # Piped uv is otherwise silent for the whole ≈2 GB download; those lines are what
    # :func:`parse_uv_download` turns into the live "Downloading <file>…" detail (spec §S-2.2).
    cmd = [str(uv), "pip", "install", "-v", "--python", str(interp), "torch", "torchvision"]
    if variant == "cu128":
        cmd += ["--index-url", _TORCH_CUDA_INDEX]
    return cmd


def reqs_install_command(uv: Path, interp: Path, reqs_path: Path) -> list[str]:
    # ``-v`` for the same live-filename reason as torch: the requirements set (numpy/opencv/
    # faiss/transformers) is the second heavy download and otherwise pipes silently.
    return [str(uv), "pip", "install", "-v", "--python", str(interp), "-r", str(reqs_path)]


def app_install_command(uv: Path, interp: Path, project: Path) -> list[str]:
    return [str(uv), "pip", "install", "--python", str(interp), "--no-deps", str(project)]


def facenet_install_command(uv: Path, interp: Path) -> list[str]:
    return [str(uv), "pip", "install", "--python", str(interp), "--no-deps", "facenet-pytorch>=2.6.0"]


def ranker_install_command(uv: Path, interp: Path, wheel: Path) -> list[str]:
    return [str(uv), "pip", "install", "--python", str(interp), "--no-deps", str(wheel)]


def find_ranker_wheel(wheels_dir: Path) -> Path | None:
    """The vendored ``msa_ranker-*.whl`` if staged (private builds only — ADR-011)."""
    if not wheels_dir.is_dir():
        return None
    hits = sorted(wheels_dir.glob("msa_ranker-*.whl"))
    return hits[0] if hits else None


# ── in-flight uv subprocess registry + subtree reaper (PR #162 follow-on) ─────
#
# ``os._exit(0)`` in the shim's reaper (the SIGTERM/SIGINT handler AND the parent-watchdog in
# ``__main__.py``) tears down THIS process but never cascades to its children — so an in-flight
# ``uv pip install`` (≈2 GB torch, minutes long) launched with a plain ``subprocess.run`` would
# be reparented to init and orphaned on a quit / force-quit during first-run provisioning. To
# close that gap without editing the read-only vendored ``main.rs`` (ADR-012): every uv step is
# launched in its OWN session/process-group (POSIX ``start_new_session``; Windows
# ``CREATE_NEW_PROCESS_GROUP``) and registered here as the live child, so the reaper can kill the
# whole subtree *before* it exits. The registry is a single module-level reference: reads/writes
# are atomic under the GIL and there is NO lock — the SIGTERM handler must be async-signal-safe,
# and a lock the spawning thread might hold when the signal interrupts it could deadlock the
# handler (finding requirement #2).
_active_child: "subprocess.Popen[str] | None" = None


def _register_active_child(proc: "subprocess.Popen[str]") -> None:
    global _active_child
    _active_child = proc


def _clear_active_child(proc: "subprocess.Popen[str]") -> None:
    global _active_child
    if _active_child is proc:  # don't clobber a newer step's registration
        _active_child = None


def _terminate_subtree(proc: "subprocess.Popen[str]", *, grace_s: float = _REAP_GRACE_S) -> None:
    """Kill ``proc`` and its whole session/process-group (POSIX) or process tree (Windows).
    Best-effort and lock-free: every failure is swallowed so a teardown race never raises in the
    reaper. POSIX SIGTERMs the group then escalates to SIGKILL after a short grace (signalling an
    already-dead group is harmless — ``ESRCH`` is swallowed); Windows uses ``taskkill /T /F``,
    consistent with the repo's existing Windows reaper patterns (WIN-004/005) and the
    ``CREATE_NEW_PROCESS_GROUP`` launch."""
    pid = proc.pid
    if os.name == "nt":
        try:
            subprocess.run(
                ["taskkill", "/T", "/F", "/PID", str(pid)], capture_output=True, check=False
            )
        except Exception:  # taskkill missing / access denied — fall back to a direct kill
            try:
                proc.kill()
            except Exception:
                pass
        return
    try:
        pgid = os.getpgid(pid)
    except OSError:
        pgid = pid  # already reaped, or no such pgid — signal the pid directly below
    try:
        os.killpg(pgid, signal.SIGTERM)
    except OSError:
        pass
    deadline = time.monotonic() + grace_s
    while time.monotonic() < deadline:
        if proc.poll() is not None:  # non-blocking; returns None if wait() holds _waitpid_lock
            return
        time.sleep(0.05)
    try:
        os.killpg(pgid, signal.SIGKILL)
    except OSError:
        pass


def reap_active_child() -> None:
    """Terminate the in-flight provisioning subprocess subtree, if any. Called by the shim's
    SIGTERM/SIGINT handler AND the parent-watchdog (``__main__.py``) *before* ``os._exit(0)``,
    which would otherwise orphan the running ``uv`` install. A no-op between uv steps (the
    common case: ``_active_child`` is ``None``). Async-signal-safe: one reference read, no locks,
    minimal work."""
    child = _active_child
    if child is None:
        return
    _terminate_subtree(child)


# ── uv progress parsing → finer StartupGate stage progress (spec §S-2.2) ─────
#
# uv suppresses its indicatif progress bars when stdout is NOT a TTY (our case: we pipe it),
# so in practice a byte-level fraction rarely appears and the stage-weighted estimate holds.
# But when uv DOES surface progress (a TTY, a future build, or verbose output), we track it so
# the setup screen advances *within* a stage instead of freezing on the big torch download.
_PCT_RE = re.compile(r"(\d{1,3}(?:\.\d+)?)\s*%")
_FRAC_RE = re.compile(
    r"([\d.]+)\s*([KMGT]?i?B)\s*/\s*([\d.]+)\s*([KMGT]?i?B)", re.IGNORECASE
)
_UNIT = {
    "B": 1, "KB": 1e3, "MB": 1e6, "GB": 1e9, "TB": 1e12,
    "KIB": 2 ** 10, "MIB": 2 ** 20, "GIB": 2 ** 30, "TIB": 2 ** 40,
}


def parse_uv_progress(line: str) -> float | None:
    """Best-effort ``0.0..1.0`` fraction from one uv output line, or ``None`` when the line has
    no progress signal (the common piped case). Matches a ``<done>/<total>`` byte pair first
    (most specific), then a bare ``N%`` token."""
    m = _FRAC_RE.search(line)
    if m:
        done = float(m.group(1)) * _UNIT.get(m.group(2).upper(), 1)
        total = float(m.group(3)) * _UNIT.get(m.group(4).upper(), 1)
        if total > 0:
            return max(0.0, min(1.0, done / total))
    m = _PCT_RE.search(line)
    if m:
        return max(0.0, min(1.0, float(m.group(1)) / 100.0))
    return None


def _lerp(lo: int, hi: int, frac: float) -> int:
    """Map a ``0..1`` fraction into the ``[lo, hi]`` pct sub-range for the current stage."""
    return int(lo + (hi - lo) * max(0.0, min(1.0, frac)))


# ── live "Downloading <file>…" detail from uv -v wheel URLs (spec §S-2.2) ────
#
# With ``-v`` (added to the torch/reqs commands), piped uv logs one line per wheel it fetches,
# e.g. ``DEBUG No cache entry for: https://…/torch-2.6.0%2Bcu128-cp312-cp312-win_amd64.whl``. We
# surface each wheel filename to the responder's rolling file list, so the setup screen shows the
# actual files landing (an installer-style scrolling list) instead of a frozen label. Only lines
# that signal a genuine fetch (``No cache entry for:`` / a ``Downloading`` verb) count — a warm
# ``Found … response`` wheel isn't being downloaded, and the ``download.pytorch.org`` host must not
# fool the guard into counting one. NOTE: these URLs carry ``%2B`` for ``+`` (cu128!), which
# ``_PCT_RE`` would misread as ``0%`` — so ``_on_line`` routes a matched download line to the file
# list and NEVER percent-parses it (would yank the bar to the stage floor). The ``(?:$|\s)`` tail
# excludes the ``.whl.metadata`` resolution probes.
_WHEEL_URL_RE = re.compile(r"/([^/\s]+\.whl)(?:$|\s)", re.IGNORECASE)


def parse_uv_download(line: str) -> str | None:
    """The wheel FILENAME uv is *downloading* on this ``-v`` line (e.g.
    ``torch-2.6.0+cu128-cp312-cp312-win_amd64.whl``), or ``None`` when the line isn't a fetch.
    Guards on uv's fetch-about-to-happen marker (``No cache entry for:`` — a cached wheel logs
    ``Found … response`` and isn't a download) or an explicit ``Downloading`` verb, and only
    matches a real ``.whl`` (not ``.whl.metadata``). The guard is a PREFIX/phrase check, not a bare
    ``"download"`` substring, so the ``download.pytorch.org`` host in a cu128 URL can't masquerade a
    cache HIT as a download. ``%2B`` (the URL-encoded ``+`` in a cu128 local-version tag) is decoded
    back to ``+`` for display."""
    low = line.lower()
    if "no cache entry" not in low and "downloading " not in low:
        return None
    m = _WHEEL_URL_RE.search(line)
    if m is None:
        return None
    return m.group(1).replace("%2B", "+").replace("%2b", "+")


# ── real byte-backed progress from install-volume growth (spec §S-2.2) ───────
#
# uv suppresses its byte-progress bars when piped (our case), so ``parse_uv_progress`` almost never
# fires and the torch band would sit at its floor for the whole multi-GB download. Instead we watch
# how much the install volume SHRINKS during the step — real bytes landing (download → cache +
# unzip → venv, both on the app-private volume) — and map that against a per-step footprint estimate
# into the stage's pct band. Approximate by design (other processes touch the volume; the estimate
# is coarse): the pct is kept strictly monotonic (never walks backward) and clamped below 100 until
# the step actually returns rc==0, and the SPA's animated bar guarantees a visible "alive" signal
# regardless of estimate accuracy. Free-space delta (O(1) ``disk_usage``) is used rather than
# summing the cache dir because it captures uv's in-flight temp wherever it lands.
_MONITOR_INTERVAL_S = 1.5
_MONITOR_CLAMP = 0.97  # never let the estimate reach the stage's hi bound before rc==0

# Total install-volume footprint per heavy step (bytes) = compressed download (cache) + unzipped
# install (venv). Deliberately rough; only paces how fast the bar creeps within its band. torch on
# NVIDIA (cu128) is by far the largest (≈2 GB download + ≈2.5 GB unzipped); CPU torch is small.
_STEP_DOWNLOAD_BYTES = {
    ("torch", "cu128"): 4_500_000_000,
    ("torch", "cpu"): 600_000_000,
    ("reqs", "cu128"): 2_000_000_000,
    ("reqs", "cpu"): 2_000_000_000,
}


def _expected_step_bytes(step_id: str, variant: str) -> int:
    """The install-volume footprint estimate for a heavy step, or ``0`` (no disk monitor) for the
    small local steps (app/facenet/ranker) that download nothing worth pacing."""
    return _STEP_DOWNLOAD_BYTES.get((step_id, variant), 0)


def _free_bytes(path: Path) -> int:
    """Free bytes on the volume holding ``path`` (nearest existing ancestor). Raises on a stat
    failure — the monitor catches it and simply stops sampling (falls back to the animated bar)."""
    return shutil.disk_usage(str(_nearest_existing(path))).free


def _download_fraction(written_bytes: float, expected_bytes: float) -> float:
    """A ``0.0.._MONITOR_CLAMP`` fraction of a heavy step from bytes-written-so-far vs the estimate.
    Clamped below 1.0 so the bar never claims the step is done before its uv call returns."""
    if expected_bytes <= 0:
        return 0.0
    return max(0.0, min(_MONITOR_CLAMP, written_bytes / expected_bytes))


class _StepProgress:
    """Mutable, forward-only progress state for one install step, shared by the two writers of a
    heavy step — the uv output reader (``_on_line``, the main provisioning thread) and the
    disk-growth monitor thread. ``advance`` is lock-guarded and monotonic in pct (a stale/spurious
    lower estimate can never walk the bar backward), returning the payload to emit (or ``None`` when
    nothing changed) so the caller emits OUTSIDE the lock — never holding it across the responder."""

    def __init__(self, lo: int, hi: int, detail: str) -> None:
        self.lo, self.hi = lo, hi
        self.pct = lo
        self.detail = detail
        self._lock = threading.Lock()

    def advance(self, pct: int | None, detail: str | None) -> tuple[int, str] | None:
        with self._lock:
            changed = False
            if detail is not None and detail != self.detail:
                self.detail = detail
                changed = True
            if pct is not None and pct > self.pct:  # monotonic: forward-only
                self.pct = pct
                changed = True
            return (self.pct, self.detail) if changed else None


def _start_download_monitor(
    stop: threading.Event,
    volume: Path,
    prog: _StepProgress,
    expected_bytes: int,
    on_advance,
    *,
    disk_free=_free_bytes,
    interval: float = _MONITOR_INTERVAL_S,
) -> threading.Thread | None:
    """Spawn a daemon thread that, while ``stop`` is unset, samples install-volume free space every
    ``interval`` s and pushes a byte-backed pct into ``prog`` (via ``on_advance``). Wait-FIRST
    (``stop.wait(interval)``) so a fast/injected runner — the test path, where the step returns in
    microseconds — is stopped before the first tick and the monitor emits nothing (keeping the
    offline suite deterministic); a real multi-minute install ticks many times. Returns ``None`` if
    the baseline can't be read (→ the animated bar carries liveness alone)."""
    try:
        baseline = disk_free(volume)
    except OSError:
        return None

    def _loop() -> None:
        while not stop.wait(interval):
            try:
                written = baseline - disk_free(volume)
            except OSError:
                continue
            on_advance(_lerp(prog.lo, prog.hi, _download_fraction(written, expected_bytes)), None)

    thread = threading.Thread(target=_loop, name="provision-download-monitor", daemon=True)
    thread.start()
    return thread


def _default_runner(
    cmd: list[str], env: dict[str, str], log_path: Path | None, on_line=None
) -> int:
    """Run uv, streaming its combined output line-by-line: each line is folded into the
    provisioning log (the install error is what a user needs when first-run provisioning fails;
    it otherwise vanishes into the invisible shell log) AND handed to ``on_line`` so the caller
    can parse uv's byte progress for finer stage pct. Streaming (vs the old ``communicate()``)
    is what lets the setup screen advance within a stage; merging stdout into stderr keeps it a
    single pipe so a large output stream can never fill a buffer and deadlock. ``encoding='utf-8',
    errors='replace'`` so a non-ASCII progress line never raises on a legacy-codepage locale.

    Launched via ``Popen`` in its own session (POSIX ``start_new_session``) / process group
    (Windows ``CREATE_NEW_PROCESS_GROUP``) and registered as the in-flight provisioning child, so
    the shim's reaper can kill the whole uv subtree before ``os._exit(0)`` — a plain
    ``subprocess.run`` leaves the ≈2 GB torch install orphaned on a quit/force-quit during
    first-run provisioning (PR #162 follow-on). This launch + registration path is UNCHANGED."""
    popen_kwargs: dict[str, object] = dict(
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,  # merge → one stream, no two-pipe deadlock while we read
        text=True,
        encoding="utf-8",
        errors="replace",
        bufsize=1,
    )
    if os.name == "nt":
        popen_kwargs["creationflags"] = _CREATE_NEW_PROCESS_GROUP
    else:
        popen_kwargs["start_new_session"] = True  # new session/pgrp ⇒ killpg reaps the subtree

    proc = subprocess.Popen(cmd, **popen_kwargs)  # type: ignore[arg-type]
    _register_active_child(proc)
    log_fh = None
    try:
        if log_path is not None:
            try:
                log_fh = log_path.open("a", encoding="utf-8")
                log_fh.write(f"$ {' '.join(cmd)}\n")
            except OSError:
                log_fh = None
        stream = proc.stdout
        if stream is not None:
            for raw in stream:  # blocks until each flush; EOF when the child (or the reaper) ends it
                line = raw.rstrip()
                if log_fh is not None:
                    try:
                        log_fh.write(line + "\n")
                    except OSError:
                        pass
                if on_line is not None:
                    try:
                        on_line(line)
                    except Exception:
                        pass  # a progress callback must never break the install
        rc = proc.wait()
    finally:
        _clear_active_child(proc)
        if log_fh is not None:
            try:
                log_fh.close()
            except OSError:
                pass
    return rc


# ── config bootstrap + downgrade guard (DataDir — spec §1.2) ─────────────────


def bootstrap_config(data_dir: Path, template: Path) -> bool:
    """Copy the staged ``config.yaml.template`` → ``<DataDir>/config.yaml`` **iff absent**
    (never overwrite — port of ``Initialize-Config`` / ``setup_config``). Returns True when it
    created the file. Raises :class:`ProvisionError` if the template is missing but config is
    absent. NEVER touches an existing config, index, or thumbnails (the DataDir invariant)."""
    config_path = data_dir / "config.yaml"
    if config_path.exists():
        return False
    if not template.exists():
        raise ProvisionError(f"config template missing at {template} — cannot bootstrap config")
    data_dir.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(template, config_path)
    return True


def _parse_version(v: str) -> tuple:
    """Best-effort numeric version tuple for downgrade comparison (leading ``v`` and pre-release
    suffixes tolerated). Returns () when unparseable → the guard skips (fail-open, like the
    installers' ConvertTo-MsaVersionObject-returns-null path)."""
    m = re.match(r"v?(\d+)\.(\d+)\.(\d+)", v.strip())
    return tuple(int(x) for x in m.groups()) if m else ()


def check_downgrade(new_version: str, version_file: Path, *, allow: bool = False) -> None:
    """Refuse to start an OLDER app over a newer DataDir (port of ``Test-VersionDowngrade``):
    the SQLite schema in ``index/media.sqlite`` can move forward between versions, and an older
    binary opening a newer DB can corrupt it. Silent no-op for a fresh install (no version
    file), an unparseable version, or ``allow=True``. Raises :class:`ProvisionError` on a
    genuine downgrade. This READS DataDir/version.txt; it never deletes anything."""
    if allow or not version_file.exists():
        return
    try:
        existing = version_file.read_text(encoding="utf-8").splitlines()[0].strip()
    except (OSError, IndexError):
        return
    new_t, old_t = _parse_version(new_version), _parse_version(existing)
    if not new_t or not old_t:
        return
    if new_t < old_t:
        raise ProvisionError(
            f"Refusing to downgrade from {existing} to {new_version}: downgrades can corrupt "
            "index/media.sqlite if the schema moved forward. Reinstall the newer version, or "
            "remove version.txt to force."
        )


def write_version(version_file: Path, version: str) -> None:
    """Record the installed app version in ``<DataDir>/version.txt`` (write-on-success). The
    only other DataDir write besides the config bootstrap."""
    version_file.parent.mkdir(parents=True, exist_ok=True)
    version_file.write_text(version + "\n", encoding="utf-8")


# ── orchestration ────────────────────────────────────────────────────────────


def dependencies_complete(
    env: dict[str, str] | None = None,
    *,
    exe: str | os.PathLike[str] | None = None,
    project: Path | None = None,
    nvidia_detector=detect_nvidia,
) -> bool:
    """True iff the app-private venv ALREADY satisfies the staged fingerprint — the S-1 hot path
    where :func:`ensure_dependencies` will no-op (no download, no resume). Mirrors that function's
    fast-path check (identical fingerprint inputs) but performs NO install and never raises.

    Used to gate the ≥5 GB disk preflight (spec §S-2.4): that gate guards a fresh/partial multi-GB
    install, so a warm launch that provisions nothing must not be blocked by a since-shrunk disk.
    Defensive by construction — ANY inability to positively confirm the all-done marker (not the
    bundled sidecar, no ``.venv`` layout, missing/unreadable project or requirements, or a
    partial/mismatched marker) returns False, so the caller runs the disk gate and provisioning
    proceeds to surface the real state. A kill-resume (partial) ledger is NOT complete: it has no
    top-level fingerprint, so ``_read_marker`` yields None and a resuming launch keeps the gate."""
    environ = os.environ if env is None else env
    if "SIDECAR_PORT" not in environ:
        return False  # not the bundled sidecar (dev/CI) — no provisioning, nothing to gate
    root = app_private_root(exe)
    if root is None:
        return False
    project = project if project is not None else staged_project_dir()
    reqs_path = requirements_file(project)
    if not (project / "pyproject.toml").exists() or not reqs_path.exists():
        return False
    try:
        filtered = filter_requirements(reqs_path.read_text(encoding="utf-8"))
    except OSError:
        return False
    variant = torch_variant(environ, detector=nvidia_detector)
    version = app_version(project)
    marker = root / _VENV_DIR / _MARKER_NAME
    fp = fingerprint(filtered.encode(), variant, version, source_digest(project))
    return _read_marker(marker) == fp


def remaining_install_min_gb(
    env: dict[str, str] | None = None,
    *,
    exe: str | os.PathLike[str] | None = None,
    project: Path | None = None,
    nvidia_detector=detect_nvidia,
) -> float:
    """Free-GB the disk preflight should demand, sized to the REMAINING provisioning work.

    A FRESH install (empty ledger) needs the full :data:`_MIN_FREE_GB`. A RESUME (a partial
    ``.venv/.msa-deps.json`` ledger left by a kill mid-install) needs less: each stage already
    recorded complete wrote its bytes to disk on the earlier run, so re-charging the whole
    fresh-install budget would reject a machine that barely fit the heavy ≈2 GB torch stage and now
    has adequate — but sub-5 GB — headroom for the small remainder (finding B / Codex PR #164). We
    subtract each completed stage's conservative installed footprint (:data:`_STAGE_INSTALLED_GB`)
    from the budget, floored at :data:`_MIN_RESUME_FREE_GB` so a near-done resume still requires real
    headroom for the last step. Only the ledger's fingerprint-matched ``completed`` steps count — a
    changed dep set / torch variant / app version discards partial progress (``_read_progress``), so
    a stale ledger correctly falls back to the full fresh budget.

    Defensive by construction: ANY inability to positively read a partial ledger (not the bundled
    sidecar, no ``.venv`` layout, missing/unreadable project or requirements, or an empty/mismatched
    ledger) returns the full :data:`_MIN_FREE_GB` — it never UNDER-demands. The caller still decides
    *whether* to run the disk gate via :func:`dependencies_complete`; this only sizes it."""
    environ = os.environ if env is None else env
    if "SIDECAR_PORT" not in environ:
        return _MIN_FREE_GB  # not the bundled sidecar (dev/CI) — no resume state to consult
    root = app_private_root(exe)
    if root is None:
        return _MIN_FREE_GB
    project = project if project is not None else staged_project_dir()
    reqs_path = requirements_file(project)
    if not (project / "pyproject.toml").exists() or not reqs_path.exists():
        return _MIN_FREE_GB
    try:
        filtered = filter_requirements(reqs_path.read_text(encoding="utf-8"))
    except OSError:
        return _MIN_FREE_GB
    variant = torch_variant(environ, detector=nvidia_detector)
    version = app_version(project)
    marker = root / _VENV_DIR / _MARKER_NAME
    fp = fingerprint(filtered.encode(), variant, version, source_digest(project))
    completed = _read_progress(marker, fp)
    if not completed:
        return _MIN_FREE_GB  # fresh install (or a fingerprint-invalidated ledger) → full budget
    reclaimed = sum(_STAGE_INSTALLED_GB.get(step, 0.0) for step in completed)
    return max(_MIN_RESUME_FREE_GB, _MIN_FREE_GB - reclaimed)


def ensure_dependencies(
    env: dict[str, str] | None = None,
    *,
    exe: str | os.PathLike[str] | None = None,
    project: Path | None = None,
    runner=_default_runner,
    on_stage=None,
    on_file=None,
    nvidia_detector=detect_nvidia,
    log_path: Path | None = None,
    disk_free=_free_bytes,
    monitor_interval: float = _MONITOR_INTERVAL_S,
) -> Path | None:
    """Install MSA's stack into the app-private venv on first run; no-op after.

    Returns the app-private root when provisioning is in scope (the bundled sidecar), else
    ``None``: a no-op for source/dev/CI (``SIDECAR_PORT`` unset, or the interpreter isn't in a
    ``.venv`` layout). Two-tier idempotence via ``.venv/.msa-deps.json``: the top-level
    fingerprint marker (written **only** after every step succeeds) is the all-done fast path,
    and a per-step ``progress`` ledger (each step recorded ONLY on ``rc==0``) makes an
    interrupted first run — e.g. ``kill -9`` mid-torch — resume past the steps it already
    finished on the next launch instead of restarting the ≈2 GB install (spec §S-2.2).

    Raises :class:`ProvisionError` when we *are* the bundled sidecar but cannot provision (the
    staged project or bundled ``uv`` is missing, or a uv step exits non-zero) — fail-loud."""
    environ = os.environ if env is None else env
    if "SIDECAR_PORT" not in environ:
        return None  # not the bundled sidecar — deps came from the editable dev install
    root = app_private_root(exe)
    if root is None:
        return None  # not a .venv layout — honest fallback (matches the upstream template's relocate)

    project = project if project is not None else staged_project_dir()
    if not (project / "pyproject.toml").exists():
        raise ProvisionError(f"staged project not found at {project} — build staging is broken")

    _emit = on_stage or (lambda *_a, **_k: None)
    _emit_file = on_file or (lambda *_a, **_k: None)
    variant = torch_variant(environ, detector=nvidia_detector)
    reqs_path = requirements_file(project)
    if not reqs_path.exists():
        raise ProvisionError(f"requirements file not found at {reqs_path}")
    filtered = filter_requirements(reqs_path.read_text(encoding="utf-8"))
    version = app_version(project)

    marker = root / _VENV_DIR / _MARKER_NAME
    fp = fingerprint(filtered.encode(), variant, version, source_digest(project))
    if _read_marker(marker) == fp:
        _emit("models-pending", 100, "Dependencies already installed")
        return root  # hot path — every launch after the first

    _assert_ledger_owned(marker, root, environ)  # app-private venv only, never DataDir
    completed = _read_progress(marker, fp)         # kill-resume: skip already-finished steps

    uv = uv_binary(root)
    if not uv.exists():
        raise ProvisionError(f"bundled uv not found at {uv}")
    interp = Path(exe) if exe is not None else Path(sys.executable)
    uv_env = install_env(root, dict(environ))
    (root / _VENV_DIR).mkdir(parents=True, exist_ok=True)  # marker dir must exist before recording

    volume = _nearest_existing(root)

    def _run_step(
        step_id: str, stage: str, pct_lo: int, pct_hi: int, detail: str, cmd: list[str],
        *, expected_bytes: int = 0,
    ) -> None:
        if step_id in completed:
            _emit(stage, pct_hi, f"{detail} — already installed")
            return  # resumed: uv already applied this step on a previous launch
        prog = _StepProgress(pct_lo, pct_hi, detail)
        _emit(stage, prog.pct, prog.detail)

        def _advance(pct: int | None, new_detail: str | None) -> None:
            payload = prog.advance(pct, new_detail)
            if payload is not None:
                _emit(stage, payload[0], payload[1])  # emit outside the step lock

        def _on_line(line: str) -> None:
            fname = parse_uv_download(line)
            if fname is not None:
                _emit_file(fname)  # rolling file list; NEVER percent-parse a URL line (its %2B
                return             # would misread as 0% and yank the bar to the stage floor)
            frac = parse_uv_progress(line)
            if frac is not None:                          # rare (TTY/verbose): real uv byte fraction
                _advance(_lerp(pct_lo, pct_hi, frac), None)

        # Heavy steps (torch/reqs): a disk-growth monitor advances the bar with real bytes landing
        # while piped uv is silent. Small local steps pass expected_bytes=0 → no monitor.
        stop = threading.Event()
        monitor = (
            _start_download_monitor(stop, volume, prog, expected_bytes, _advance,
                                    disk_free=disk_free, interval=monitor_interval)
            if expected_bytes > 0 else None
        )
        try:
            rc = runner(cmd, uv_env, log_path, on_line=_on_line)
        finally:
            stop.set()
            if monitor is not None:
                monitor.join(timeout=2.0)
        if rc != 0:
            raise ProvisionError(f"{detail} failed (uv exit {rc}) — see the provisioning log")
        _record_stage(marker, fp, step_id, variant, version, completed)  # persist ONLY on rc==0

    # Each step owns a pct sub-range; uv byte progress (when exposed) interpolates within it,
    # else the low bound is a stage-weighted estimate. torch is the heaviest → the widest band.
    # 1) torch first (gated) so the resolver can't replace the wheel.
    _run_step("torch", "deps-torch", 10, 50, f"Installing PyTorch ({variant})",
              torch_install_command(uv, interp, variant),
              expected_bytes=_expected_step_bytes("torch", variant))
    # 2) the platform requirements (torch/facenet/ranker stripped). Only materialize the temp
    #    requirements file when the step actually needs to run (resume skips it otherwise).
    if "reqs" in completed:
        _emit("deps-app", 78, "Installing Python packages — already installed")
    else:
        fd, tmp = tempfile.mkstemp(prefix="msa-reqs-", suffix=".txt")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(filtered)
            _run_step("reqs", "deps-app", 50, 78, "Installing Python packages",
                      reqs_install_command(uv, interp, Path(tmp)),
                      expected_bytes=_expected_step_bytes("reqs", variant))
        finally:
            try:
                os.unlink(tmp)
            except OSError:
                pass
    # 3) the app itself, --no-deps — built from a writable TEMP COPY, never in place.
    #    setuptools' build backend writes ``src/<pkg>.egg-info`` into the tree it builds, and
    #    the staged tree lives inside the app bundle: read-only when macOS App Translocation
    #    runs a quarantined app straight from the DMG (uv exit 1, "could not create
    #    'src/media_search_agent.egg-info': Read-only file system"), and not ours to mutate
    #    even where it happens to be writable (bundle integrity). Like the reqs temp file
    #    above, only materialize the copy when the step actually needs to run — the disk
    #    budget credits completed steps, so a wasted copy could fail a valid low-space resume.
    if "app" in completed:
        _emit("deps-app", 86, "Installing MediaSearchAgent — already installed")
    else:
        with tempfile.TemporaryDirectory(prefix="msa-app-build-") as build_tmp:
            build_copy = Path(build_tmp) / project.name
            shutil.copytree(project, build_copy,
                            ignore=shutil.ignore_patterns("*.egg-info", "__pycache__"))
            _run_step("app", "deps-app", 78, 86, "Installing MediaSearchAgent",
                      app_install_command(uv, interp, build_copy))
    # 4) facenet-pytorch --no-deps (after torch so the resolver doesn't downgrade it).
    _run_step("facenet", "deps-app", 86, 94, "Installing face recognition",
              facenet_install_command(uv, interp))
    # 5) the vendored ranker wheel --no-deps, if staged (private builds — ADR-011).
    wheel = find_ranker_wheel(staged_wheels_dir())
    if wheel is not None:
        _run_step("ranker", "deps-app", 94, 99, "Installing ranker",
                  ranker_install_command(uv, interp, wheel))

    _write_marker(marker, fp, variant, version)  # all-done fast path for next launch
    _emit("models-pending", 100, "Dependencies ready")
    return root


# ── ADR-009 platform directory map (single source; the shim delegates here) ───


def platform_dirs() -> dict[str, Path]:
    """The ADR-009 per-platform DataDir/config/cache/log map. The single source of truth the
    shim (``__main__``) and the headless entry (below) both use, so a headless-provisioned box
    lands its config.yaml / version.txt / logs in exactly the locations a GUI first run would."""
    home = Path.home()
    if os.name == "nt":
        local_raw = os.environ.get("LOCALAPPDATA")
        local = Path(local_raw) if local_raw else home / "AppData" / "Local"
        return {
            "data": home / "MediaSearchAgent",
            "config": home / "MediaSearchAgent",
            "cache": local / "MediaSearchAgent" / "Cache",
            "log": local / "MediaSearchAgent" / "logs",
        }
    if sys.platform == "darwin":
        base = home / "Library" / "Application Support" / "MediaSearchAgent"
        return {
            "data": base,
            "config": base,
            "cache": home / "Library" / "Caches" / "MediaSearchAgent",
            "log": home / "Library" / "Logs" / "MediaSearchAgent",
        }
    return {  # Linux / WSL2 (the shim runs here only via the S-3 --headless bootstrap)
        "data": home / ".local" / "share" / "MediaSearchAgent",
        "config": home / ".config" / "MediaSearchAgent",
        "cache": home / ".cache" / "MediaSearchAgent",
        "log": home / ".local" / "share" / "MediaSearchAgent" / "logs",
    }


def resolved_dirs() -> dict[str, Path]:
    """Platform defaults, with any pre-set ``MSA_*`` override respected (matches the ``msa_settings``
    precedence so the shim, the sidecar, and the headless entry all agree)."""
    d = platform_dirs()
    if os.environ.get("MSA_DATA_DIR"):
        d["data"] = Path(os.environ["MSA_DATA_DIR"])
        d["config"] = Path(os.environ["MSA_DATA_DIR"])  # config lives beside data (ADR-009)
    if os.environ.get("MSA_CACHE_DIR"):
        d["cache"] = Path(os.environ["MSA_CACHE_DIR"])
    if os.environ.get("MSA_LOG_DIR"):
        d["log"] = Path(os.environ["MSA_LOG_DIR"])
    return d


# ── headless provisioning entry: `python -m app.provision` (spec §S-3 item 4) ─


def headless_main(argv: list[str] | None = None) -> int:
    """The runnable entry the thin bootstraps' ``--headless`` / ``-Headless`` path invokes with the
    app-private **venv** python (``<root>/.venv/<bin>/python -m app.provision``). The bootstrap has
    already located the bundle and run ``uv python install`` + ``uv venv`` (same ``UV_*`` pins); we
    do exactly what a GUI first run does minus the responder/uvicorn handoff — preflight,
    ``ensure_dependencies`` (fingerprint-gated, resumable), config bootstrap, version stamp — so a
    headless box ends in the same state and can then serve the SPA via ``msa api start``. All path
    logic stays here (the bootstrap only locates the bundle). Idempotent; exit 0 on success, 1 on a
    :class:`ProvisionError` or a wrong (non-venv) interpreter."""
    root = app_private_root()
    if root is None:
        sys.stderr.write(
            "app.provision: must be run with the app-private venv python "
            "(sys.executable is not in a <root>/.venv/<bin>/python layout) -- the bootstrap creates "
            "the venv with `uv venv` first, then runs `<root>/.venv/<bin>/python -m app.provision`.\n"
        )
        return 1

    # ensure_dependencies gates on SIDECAR_PORT (to tell the bundled sidecar apart from dev/CI); in
    # headless mode we ARE provisioning the bundle, so mark it present without touching os.environ.
    env = dict(os.environ)
    env.setdefault("SIDECAR_PORT", "headless")

    dirs = resolved_dirs()
    data_dir, config_dir, log_dir = dirs["data"], dirs["config"], dirs["log"]
    try:
        log_dir.mkdir(parents=True, exist_ok=True)
    except OSError:
        pass
    provision_log = log_dir / f"provision-headless-{time.strftime('%Y%m%d-%H%M%S')}.log"

    def _emit(stage: str, pct: int, detail: str = "", log_field: str = "") -> None:
        sys.stderr.write(f"[provision] {stage} {pct}% {detail}\n")
        sys.stderr.flush()

    def _emit_file(filename: str) -> None:
        sys.stderr.write(f"[provision] downloading {filename}\n")
        sys.stderr.flush()

    project = staged_project_dir()
    if not (project / "pyproject.toml").exists():
        sys.stderr.write(f"app.provision: staged project not found at {project} -- bundle is broken\n")
        return 1
    version = app_version(project)
    try:
        needs = not dependencies_complete(env, project=project)
        # Belt-and-braces legacy migration on first run — the SAME guarded sweep the GUI shim runs
        # (`app.__main__.main`), so `--headless` also clears a stale legacy LaunchAgent / scheduled
        # task that would otherwise auto-launch the GUI and defeat headless. All allowlist / realpath
        # / DataDir-never / self-install guards stay intact (run_first_run_sweep -> sweep_legacy_install);
        # it never raises and never blocks provisioning. Gated to first run (needs) like the GUI, and
        # placed BEFORE the disk gate so any freed legacy models/venv count toward preflight.
        if needs:
            from app import migration  # local import: keep provision stdlib-only at module load
            migration.run_first_run_sweep(
                os.environ, log=lambda m: sys.stderr.write(f"[migration] {m}\n")
            )
        min_free = remaining_install_min_gb(env, project=project)
        preflight_system(check_disk=needs, min_free_gb=min_free)
        check_downgrade(version, data_dir / _VERSION_FILE)
        ensure_dependencies(env=env, on_stage=_emit, on_file=_emit_file, log_path=provision_log)
        bootstrap_config(config_dir, project / _CONFIG_TEMPLATE)
        write_version(data_dir / _VERSION_FILE, version)
    except ProvisionError as exc:
        sys.stderr.write(f"[provision] FAILED: {exc}\n  see {provision_log}\n")
        return 1
    sys.stderr.write("[provision] complete -- run `msa api start` to serve the SPA at the config port\n")
    return 0


if __name__ == "__main__":  # `python -m app.provision`
    raise SystemExit(headless_main())
