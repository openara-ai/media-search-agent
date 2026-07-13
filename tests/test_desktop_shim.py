"""Offline unit tests for the committed desktop-shell shim (src-tauri/backend/app/).

The shim lives outside the normal Python package tree (it is a bundle resource staged next to
the venv), so this suite puts src-tauri/backend on sys.path and imports ``app.*`` directly. All
provisioning is exercised **offline** — no real uv, no network, no torch — via the injectable
``runner`` / ``nvidia_detector`` / ``on_stage`` hooks (M-7/S-1 spec §1.2).

Guardrail under test: provisioning NEVER writes to or deletes from DataDir outside the two
specced paths (config.yaml bootstrap iff-absent, version.txt on success).
"""

import json
import os
import signal
import sys
import threading
import time
import urllib.request
from pathlib import Path

import pytest

_SHIM_ROOT = Path(__file__).resolve().parents[1] / "src-tauri" / "backend"
if str(_SHIM_ROOT) not in sys.path:
    sys.path.insert(0, str(_SHIM_ROOT))

from app import __main__ as shim  # noqa: E402
from app import applog  # noqa: E402
from app import migration  # noqa: E402
from app import provision  # noqa: E402
from app import responder  # noqa: E402


@pytest.fixture
def clean_root_logger():
    """Snapshot/restore root-logger handlers + level so the unified-log tests don't leak a
    handler (and its open file) into the rest of the run."""
    import logging

    root = logging.getLogger()
    before = root.handlers[:]
    level = root.level
    try:
        yield root
    finally:
        for handler in root.handlers[:]:
            if handler not in before:
                root.removeHandler(handler)
                try:
                    handler.close()
                except Exception:
                    pass
        root.setLevel(level)


# ── app_private_root: abspath, not resolve (symlink trap) ─────────────


def test_app_private_root_detects_venv_layout(tmp_path):
    exe = tmp_path / "root" / ".venv" / "bin" / "python3"
    exe.parent.mkdir(parents=True)
    exe.write_text("")
    assert provision.app_private_root(exe) == tmp_path / "root"


def test_app_private_root_windows_scripts_layout(tmp_path):
    exe = tmp_path / "root" / ".venv" / "Scripts" / "python.exe"
    exe.parent.mkdir(parents=True)
    exe.write_text("")
    assert provision.app_private_root(exe) == tmp_path / "root"


def test_app_private_root_none_for_system_python(tmp_path):
    exe = tmp_path / "usr" / "bin" / "python3"
    exe.parent.mkdir(parents=True)
    exe.write_text("")
    assert provision.app_private_root(exe) is None


# ── torch variant gate ───────────────────────────────────────────────────────


def test_torch_variant_cpu_off_windows(monkeypatch):
    monkeypatch.setattr(provision.os, "name", "posix")
    assert provision.torch_variant({}, detector=lambda: True) == "cpu"


def test_torch_variant_cu128_on_nvidia_windows(monkeypatch):
    monkeypatch.setattr(provision.os, "name", "nt")
    assert provision.torch_variant({}, detector=lambda: True) == "cu128"
    assert provision.torch_variant({}, detector=lambda: False) == "cpu"


def test_torch_variant_env_override_wins(monkeypatch):
    monkeypatch.setattr(provision.os, "name", "nt")
    assert provision.torch_variant({"MSA_TORCH_VARIANT": "cpu"}, detector=lambda: True) == "cpu"


def test_detect_nvidia_fallback_to_nvidia_smi():
    # Non-Windows path: presence == nvidia-smi on PATH (injected).
    assert provision.detect_nvidia(which=lambda _n: "/usr/bin/nvidia-smi", platform="posix") is True
    assert provision.detect_nvidia(which=lambda _n: None, platform="posix") is False


# ── system preflight: arch + free disk (spec §S-2.4) ─────────────────────────


from types import SimpleNamespace


def _disk(free_gib):
    return lambda _p: SimpleNamespace(total=0, used=0, free=int(free_gib * 1024 ** 3))


def test_preflight_rejects_intel_mac(tmp_path):
    with pytest.raises(provision.ProvisionError, match="Apple Silicon"):
        provision.preflight_system(
            tmp_path, platform_name="darwin", machine="x86_64", disk_usage=_disk(100)
        )


def test_preflight_allows_apple_silicon(tmp_path):
    provision.preflight_system(
        tmp_path, platform_name="darwin", machine="arm64", disk_usage=_disk(100)
    )  # no raise


def test_preflight_rejects_arm_windows(tmp_path):
    with pytest.raises(provision.ProvisionError, match="x86_64 only"):
        provision.preflight_system(
            tmp_path, platform_name="win32", machine="ARM64", disk_usage=_disk(100)
        )


def test_preflight_allows_x64_windows(tmp_path):
    provision.preflight_system(
        tmp_path, platform_name="win32", machine="AMD64", disk_usage=_disk(100)
    )  # no raise


def test_preflight_rejects_low_disk(tmp_path):
    with pytest.raises(provision.ProvisionError, match="free disk space"):
        provision.preflight_system(
            tmp_path, platform_name="darwin", machine="arm64", disk_usage=_disk(4.5)
        )


def test_preflight_disk_check_is_fail_open_on_stat_error(tmp_path):
    def _boom(_p):
        raise OSError("cannot stat")

    # A stat failure must not block launch (installers warn-and-continue).
    provision.preflight_system(
        tmp_path, platform_name="darwin", machine="arm64", disk_usage=_boom
    )


def test_preflight_disk_uses_nearest_existing_ancestor(tmp_path):
    seen = {}

    def _usage(p):
        seen["path"] = p
        return SimpleNamespace(total=0, used=0, free=100 * 1024 ** 3)

    missing = tmp_path / "does" / "not" / "exist" / ".venv"
    provision.preflight_system(
        missing, platform_name="linux", machine="x86_64", disk_usage=_usage
    )
    # Resolved to an existing ancestor, not the missing leaf.
    assert Path(seen["path"]).exists()


def test_preflight_skips_disk_gate_when_check_disk_false(tmp_path):
    """Warm launch (deps already provisioned): check_disk=False skips the ≥5 GB gate so a
    since-shrunk disk can't block a start that downloads nothing (spec §S-2.4)."""
    provision.preflight_system(
        tmp_path, platform_name="darwin", machine="arm64",
        disk_usage=_disk(1.0), check_disk=False,  # 1 GB would normally raise
    )  # no raise — the disk gate is skipped


def test_preflight_arch_guard_stays_fatal_even_with_disk_skipped(tmp_path):
    """ARCH is always fatal — an unsupported CPU can never run MSA, hot path or not — so it must
    raise even when the disk gate is skipped."""
    with pytest.raises(provision.ProvisionError, match="Apple Silicon"):
        provision.preflight_system(
            tmp_path, platform_name="darwin", machine="x86_64",
            disk_usage=_disk(100), check_disk=False,
        )


# ── requirements filtering ───────────────────────────────────────────────────


def test_filter_requirements_strips_torch_facenet_ranker():
    txt = "\n".join([
        "# comment",
        "numpy<2",
        "torch==2.6.0",
        "torchvision",
        "facenet-pytorch>=2.6.0",
        "msa-ranker==0.1.0",
        "open-clip-torch",  # must NOT be stripped (only exact torch/torchvision)
        "pillow",
    ])
    out = provision.filter_requirements(txt)
    assert "numpy<2" in out and "pillow" in out and "open-clip-torch" in out
    assert "# comment" in out
    assert "torch==2.6.0" not in out
    assert "\ntorchvision" not in out
    assert "facenet-pytorch" not in out
    assert "msa-ranker" not in out


# ── uv progress parsing → finer stage pct (spec §S-2.2) ──────────────────────


def test_parse_uv_progress_byte_fraction():
    assert provision.parse_uv_progress("torch 383.0MiB/766.0MiB") == pytest.approx(0.5, abs=0.01)
    assert provision.parse_uv_progress("Downloaded 1.0GiB/4.0GiB") == pytest.approx(0.25, abs=0.01)


def test_parse_uv_progress_percent_token():
    assert provision.parse_uv_progress("Downloading torch (45%)") == pytest.approx(0.45)
    assert provision.parse_uv_progress("100%") == pytest.approx(1.0)


def test_parse_uv_progress_none_for_plain_lines():
    assert provision.parse_uv_progress("Resolved 50 packages in 1.2s") is None
    assert provision.parse_uv_progress("Prepared torch") is None


def test_lerp_maps_fraction_into_stage_band():
    assert provision._lerp(10, 50, 0.0) == 10
    assert provision._lerp(10, 50, 0.5) == 30
    assert provision._lerp(10, 50, 1.0) == 50
    assert provision._lerp(10, 50, 2.0) == 50  # clamped


def test_run_step_emits_finer_progress_from_uv_lines(tmp_path):
    """When the injected runner surfaces uv byte progress via on_line, ensure_dependencies emits
    an intra-stage pct interpolated into that step's band (torch = 10..50)."""
    project = _stage_project(tmp_path)
    _root, exe = _stage_root(tmp_path)
    emitted = []

    def runner(cmd, env, log_path, on_line=None):
        if on_line is not None and "torch" in " ".join(cmd) and "torchvision" in " ".join(cmd):
            on_line("torch 383.0MiB/766.0MiB")  # ~50% of the torch step
        return 0

    provision.ensure_dependencies(
        {"SIDECAR_PORT": "5000", "MSA_TORCH_VARIANT": "cpu"},
        exe=exe, project=project, runner=runner, nvidia_detector=lambda: False,
        on_stage=lambda stage, pct, detail="", log="": emitted.append((stage, pct)),
    )
    # torch band is 10..50; ~50% → ~30, strictly between the lo and hi bounds.
    torch_pcts = [pct for stage, pct in emitted if stage == "deps-torch"]
    assert any(10 < pct < 50 for pct in torch_pcts), torch_pcts


# ── live "Downloading <file>…" detail from uv -v wheel URLs ──────────────────


def test_torch_and_reqs_commands_are_verbose():
    """The two heavy download steps pass ``-v`` so uv logs the per-wheel URLs that
    parse_uv_download turns into live filenames (piped uv is otherwise silent)."""
    uv, interp = Path("/uv"), Path("/py")
    assert "-v" in provision.torch_install_command(uv, interp, "cu128")
    assert "-v" in provision.reqs_install_command(uv, interp, Path("/reqs.txt"))
    # the small local steps stay quiet (no -v flood for a --no-deps local install)
    assert "-v" not in provision.app_install_command(uv, interp, Path("/proj"))


def test_parse_uv_download_extracts_wheel_filename():
    torch = "DEBUG No cache entry for: https://download.pytorch.org/whl/cu128/torch-2.6.0%2Bcu128-cp312-cp312-win_amd64.whl"
    # full wheel basename, with the %2B local-version tag decoded back to '+'
    assert provision.parse_uv_download(torch) == "torch-2.6.0+cu128-cp312-cp312-win_amd64.whl"
    cudnn = "DEBUG No cache entry for: https://x/nvidia_cudnn_cu12-9.1.0.70-py3-none-manylinux2014_x86_64.whl"
    assert provision.parse_uv_download(cudnn) == "nvidia_cudnn_cu12-9.1.0.70-py3-none-manylinux2014_x86_64.whl"


def test_parse_uv_download_ignores_metadata_cache_hits_and_plain_lines():
    # .whl.metadata is a resolution probe, not a wheel download
    assert provision.parse_uv_download("No cache entry for: https://x/torch-2.6.0-cp312.whl.metadata") is None
    # a cache HIT is not a download — and the download.pytorch.org HOST must not fool the guard
    hit = "DEBUG Found fresh response for: https://download.pytorch.org/whl/cu128/torch-2.6.0%2Bcu128-cp312-cp312-win_amd64.whl"
    assert provision.parse_uv_download(hit) is None
    assert provision.parse_uv_download("Prepared 6 packages in 1.2s") is None
    assert provision.parse_uv_download("Resolved 50 packages in 1.2s") is None


def test_run_step_reports_wheel_files_and_keeps_step_label_detail(tmp_path):
    """A uv -v wheel URL is reported to on_file (the rolling file list) — NOT folded into the
    responder ``detail``, which stays the stable step label. And the ``%2B`` in a cu128 URL is
    treated as a filename, NEVER percent-parsed to 0% (which would yank the bar to the stage floor)."""
    project = _stage_project(tmp_path)
    _root, exe = _stage_root(tmp_path)
    emitted, files = [], []

    def runner(cmd, env, log_path, on_line=None):
        if on_line is not None and "torchvision" in " ".join(cmd):
            on_line("DEBUG No cache entry for: https://download.pytorch.org/whl/cu128/"
                    "torch-2.6.0%2Bcu128-cp312-cp312-win_amd64.whl")
        return 0

    provision.ensure_dependencies(
        {"SIDECAR_PORT": "5000", "MSA_TORCH_VARIANT": "cu128"},
        exe=exe, project=project, runner=runner, nvidia_detector=lambda: True,
        on_stage=lambda stage, pct, detail="", log="": emitted.append((stage, pct, detail)),
        on_file=files.append,
    )
    # the wheel filename reached the file list (decoded), not the detail line
    assert "torch-2.6.0+cu128-cp312-cp312-win_amd64.whl" in files, files
    torch_emits = [(pct, detail) for stage, pct, detail in emitted if stage == "deps-torch"]
    assert all("Downloading" not in detail for _pct, detail in torch_emits), torch_emits
    assert any(detail == "Installing PyTorch (cu128)" for _pct, detail in torch_emits), torch_emits
    # the %2B URL never dropped the torch bar below its 10 floor (spurious 0% would have)
    assert min(pct for pct, _detail in torch_emits) >= 10, torch_emits


# ── byte-backed progress from install-volume growth ──────────────────────────


def test_download_fraction_is_monotone_and_clamped_below_one():
    assert provision._download_fraction(0, 4.5e9) == 0.0
    assert provision._download_fraction(2e9, 4.5e9) == pytest.approx(0.444, abs=0.01)
    assert provision._download_fraction(9e9, 4.5e9) == provision._MONITOR_CLAMP  # never reaches 1.0
    assert provision._download_fraction(100, 0) == 0.0  # no estimate → no fraction


def test_expected_step_bytes_zero_for_small_local_steps():
    assert provision._expected_step_bytes("torch", "cu128") > provision._expected_step_bytes("torch", "cpu") > 0
    assert provision._expected_step_bytes("reqs", "cpu") > 0
    # app/facenet/ranker download nothing worth pacing → no monitor
    assert provision._expected_step_bytes("app", "cpu") == 0
    assert provision._expected_step_bytes("facenet", "cu128") == 0


def test_step_progress_advance_is_forward_only_and_detail_aware():
    prog = provision._StepProgress(10, 50, "Installing PyTorch (cu128)")
    assert prog.advance(30, None) == (30, "Installing PyTorch (cu128)")
    assert prog.advance(20, None) is None                       # backward pct ignored
    assert prog.advance(None, "Downloading torch…") == (30, "Downloading torch…")
    assert prog.advance(30, "Downloading torch…") is None       # no change
    assert prog.advance(40, None) == (40, "Downloading torch…")


def test_download_monitor_no_emit_when_stopped_before_first_tick():
    """Wait-first: an already-stopped monitor never samples — the offline suite's instant runner
    is stopped before the first interval, so the monitor stays silent (deterministic tests)."""
    prog = provision._StepProgress(10, 50, "x")
    emits = []
    stop = threading.Event()
    stop.set()
    thread = provision._start_download_monitor(
        stop, tmp_path_stub(), prog, 4_500_000_000, lambda pct, detail: emits.append((pct, detail)),
        disk_free=lambda _p: 1, interval=5.0,
    )
    if thread is not None:
        thread.join(timeout=1.0)
    assert emits == []


def test_download_monitor_advances_bar_from_shrinking_free_space():
    """A real (blocking) install: as the install volume shrinks, the monitor pushes a monotone,
    byte-backed pct into the torch 10..50 band."""
    prog = provision._StepProgress(10, 50, "Installing PyTorch (cu128)")
    emits = []
    samples = [10_000_000_000, 8_000_000_000, 5_500_000_000]  # baseline, then shrinking
    idx = {"i": 0}

    def disk_free(_path):
        value = samples[min(idx["i"], len(samples) - 1)]
        idx["i"] += 1
        return int(value)

    def on_advance(pct, detail):
        payload = prog.advance(pct, detail)
        if payload is not None:
            emits.append(payload[0])

    stop = threading.Event()
    thread = provision._start_download_monitor(
        stop, tmp_path_stub(), prog, 4_500_000_000, on_advance, disk_free=disk_free, interval=0.01,
    )
    time.sleep(0.1)
    stop.set()
    if thread is not None:
        thread.join(timeout=1.0)
    assert emits, "monitor never advanced the bar"
    assert emits == sorted(emits)                     # forward-only
    assert all(10 < pct <= 50 for pct in emits)       # within the torch band


def tmp_path_stub() -> Path:
    """The monitor only stats the volume via the injected ``disk_free`` in these tests; any real,
    existing directory satisfies the baseline read it does before the loop."""
    return Path(__file__).resolve().parent


# ── fingerprint marker + no-op fast path ─────────────────────────────────────


def _stage_project(tmp_path, *, version="1.2.3", reqs="numpy\n") -> Path:
    project = tmp_path / "backend" / "msa"
    project.mkdir(parents=True)
    (project / "pyproject.toml").write_text(f'[project]\nname="media-search-agent"\nversion="{version}"\n')
    (project / "requirements-api.txt").write_text(reqs)
    return project


def _stage_root(tmp_path):
    """A fake app-private root with a bundled uv + venv layout."""
    root = tmp_path / "root"
    (root / "bin").mkdir(parents=True)
    (root / "bin" / ("uv.exe" if sys.platform.startswith("win") else "uv")).write_text("")
    venv_bin = root / ".venv" / ("Scripts" if sys.platform.startswith("win") else "bin")
    venv_bin.mkdir(parents=True)
    exe = venv_bin / ("python.exe" if sys.platform.startswith("win") else "python3")
    exe.write_text("")
    return root, exe


def test_ensure_dependencies_noop_without_sidecar_port(tmp_path):
    project = _stage_project(tmp_path)
    assert provision.ensure_dependencies({}, project=project) is None


def test_ensure_dependencies_installs_in_spec_order(tmp_path, monkeypatch):
    project = _stage_project(tmp_path)
    root, exe = _stage_root(tmp_path)
    calls = []

    def runner(cmd, env, log_path, on_line=None):
        calls.append(cmd)
        # assert the app-owned uv discipline is on every invocation
        assert env["UV_NO_CONFIG"] == "1"
        assert env["UV_PYTHON_INSTALL_BIN"] == "0"
        assert env["UV_CACHE_DIR"].endswith("uv-cache")
        return 0

    result = provision.ensure_dependencies(
        {"SIDECAR_PORT": "5000", "MSA_TORCH_VARIANT": "cpu"},
        exe=exe, project=project, runner=runner, nvidia_detector=lambda: False,
    )
    assert result == root
    joined = [" ".join(c) for c in calls]
    # order: torch -> requirements -> app --no-deps -> facenet --no-deps
    assert "torch" in joined[0] and "torchvision" in joined[0]
    assert "--index-url" not in joined[0]  # cpu variant
    assert "-r" in joined[1]
    assert "--no-deps" in joined[2] and str(project) in joined[2]
    assert "facenet-pytorch>=2.6.0" in joined[3]
    # marker written -> a second call is a no-op (runner not invoked again)
    calls.clear()
    provision.ensure_dependencies(
        {"SIDECAR_PORT": "5000", "MSA_TORCH_VARIANT": "cpu"},
        exe=exe, project=project, runner=runner, nvidia_detector=lambda: False,
    )
    assert calls == []


def test_ensure_dependencies_cu128_index_url(tmp_path):
    project = _stage_project(tmp_path)
    _root, exe = _stage_root(tmp_path)
    calls = []
    provision.ensure_dependencies(
        {"SIDECAR_PORT": "5000", "MSA_TORCH_VARIANT": "cu128"},
        exe=exe, project=project, runner=lambda c, e, l, on_line=None: calls.append(c) or 0,
    )
    assert "--index-url" in " ".join(calls[0])
    assert provision._TORCH_CUDA_INDEX in " ".join(calls[0])


def test_ensure_dependencies_fails_loud_on_uv_error(tmp_path):
    project = _stage_project(tmp_path)
    _root, exe = _stage_root(tmp_path)
    with pytest.raises(provision.ProvisionError, match="uv exit 1"):
        provision.ensure_dependencies(
            {"SIDECAR_PORT": "5000", "MSA_TORCH_VARIANT": "cpu"},
            exe=exe, project=project, runner=lambda c, e, l, on_line=None: 1,
        )


# ── resumable per-stage ledger: kill-resume DoD (spec §S-2.2) ────────────────


def test_ensure_dependencies_resumes_past_completed_steps(tmp_path):
    """kill -9 mid-install → relaunch resumes past finished steps, no restart of the ≈2 GB
    install. First run: torch + reqs succeed, the app step fails (simulating a crash). The
    all-done marker must be absent; the per-step ledger records torch + reqs. Relaunch: those
    two are SKIPPED, app + facenet run, and the all-done marker is written."""
    project = _stage_project(tmp_path)
    root, exe = _stage_root(tmp_path)
    env = {"SIDECAR_PORT": "5000", "MSA_TORCH_VARIANT": "cpu"}
    marker = root / ".venv" / ".msa-deps.json"

    calls1 = []

    def runner_fail_on_app(cmd, e, l, on_line=None):
        calls1.append(cmd)
        return 0 if len(calls1) < 3 else 1  # torch, reqs ok; app (3rd) fails

    with pytest.raises(provision.ProvisionError):
        provision.ensure_dependencies(
            env, exe=exe, project=project, runner=runner_fail_on_app, nvidia_detector=lambda: False
        )

    assert provision._read_marker(marker) is None  # install did not finish → no all-done marker
    ledger = json.loads(marker.read_text())
    assert set(ledger["progress"]["completed"]) == {"torch", "reqs"}  # only rc==0 steps recorded

    calls2 = []
    result = provision.ensure_dependencies(
        env, exe=exe, project=project, runner=lambda c, e, l, on_line=None: calls2.append(c) or 0,
        nvidia_detector=lambda: False,
    )
    assert result == root
    joined2 = [" ".join(c) for c in calls2]
    assert not any("torchvision" in j for j in joined2)   # torch skipped on resume
    assert not any(" -r " in f" {j} " for j in joined2)   # reqs skipped on resume
    assert any("--no-deps" in j and str(project) in j for j in joined2)  # app ran
    assert any("facenet-pytorch" in j for j in joined2)   # facenet ran
    assert provision._read_marker(marker) is not None     # all-done marker now written

    # A third launch is the hot no-op path.
    calls3 = []
    provision.ensure_dependencies(
        env, exe=exe, project=project, runner=lambda c, e, l, on_line=None: calls3.append(c) or 0,
        nvidia_detector=lambda: False,
    )
    assert calls3 == []


def test_dependencies_complete_true_after_install_false_on_change(tmp_path):
    """The hot-path predicate that gates the disk preflight: False for a fresh venv, True once the
    all-done fingerprint marker matches (warm launch — nothing to download), and False again once
    the fingerprint changes (a new dep set would reinstall)."""
    project = _stage_project(tmp_path)
    _root, exe = _stage_root(tmp_path)
    env = {"SIDECAR_PORT": "5000", "MSA_TORCH_VARIANT": "cpu"}

    # Fresh venv: no marker → not complete → disk gate must run.
    assert provision.dependencies_complete(
        env, exe=exe, project=project, nvidia_detector=lambda: False
    ) is False

    # A successful install writes the all-done marker → complete → disk gate can be skipped.
    provision.ensure_dependencies(
        env, exe=exe, project=project,
        runner=lambda c, e, l, on_line=None: 0, nvidia_detector=lambda: False,
    )
    assert provision.dependencies_complete(
        env, exe=exe, project=project, nvidia_detector=lambda: False
    ) is True

    # A changed requirements set invalidates the fingerprint → not complete.
    (project / "requirements-api.txt").write_text("numpy\nscipy\n")
    assert provision.dependencies_complete(
        env, exe=exe, project=project, nvidia_detector=lambda: False
    ) is False


def test_dependencies_complete_false_for_partial_resume_ledger(tmp_path):
    """A kill-resume (partial) ledger has per-step progress but no top-level fingerprint, so it is
    NOT complete — a resuming launch must still run the disk preflight before downloading the
    remaining steps (the S-2 kill-resume DoD is not regressed)."""
    project = _stage_project(tmp_path)
    _root, exe = _stage_root(tmp_path)
    env = {"SIDECAR_PORT": "5000", "MSA_TORCH_VARIANT": "cpu"}
    calls = []

    def runner_fail_on_app(cmd, e, l, on_line=None):
        calls.append(cmd)
        return 0 if len(calls) < 3 else 1  # torch, reqs ok; app fails → partial ledger

    with pytest.raises(provision.ProvisionError):
        provision.ensure_dependencies(
            env, exe=exe, project=project, runner=runner_fail_on_app, nvidia_detector=lambda: False
        )
    assert provision.dependencies_complete(
        env, exe=exe, project=project, nvidia_detector=lambda: False
    ) is False


def test_dependencies_complete_false_without_sidecar_port(tmp_path):
    """Not the bundled sidecar (dev/CI) → nothing to provision, nothing to gate → False."""
    project = _stage_project(tmp_path)
    _root, exe = _stage_root(tmp_path)
    assert provision.dependencies_complete({}, exe=exe, project=project) is False


def test_progress_ledger_invalidated_when_fingerprint_changes(tmp_path):
    marker = tmp_path / ".msa-deps.json"
    provision._atomic_write_json(marker, {"progress": {"fingerprint": "OLD", "completed": ["torch"]}})
    assert provision._read_progress(marker, "OLD") == {"torch"}
    assert provision._read_progress(marker, "NEW") == set()  # changed dep set → discard partial
    assert provision._read_marker(marker) is None            # no all-done fingerprint recorded


# ── resume-sized disk preflight (finding B / Codex PR #164) ──────────────────


def _partial_ledger_torch_done(env, exe, project):
    """Drive ensure_dependencies to a kill right after the heavy torch stage: the runner succeeds
    for torch (call 1) then fails on the reqs step (call 2), leaving a partial ledger whose
    ``completed`` set is exactly ``{"torch"}`` — the exact resume shape finding B is about."""
    calls = []

    def _runner(cmd, e, l, on_line=None):
        calls.append(cmd)
        return 0 if len(calls) < 2 else 1  # torch ok, reqs fails → ledger records only torch

    with pytest.raises(provision.ProvisionError):
        provision.ensure_dependencies(
            env, exe=exe, project=project, runner=_runner, nvidia_detector=lambda: False
        )


def test_remaining_install_min_gb_reduces_on_resume(tmp_path):
    """Finding B: a RESUME with the heavy torch stage already recorded complete must NOT re-demand
    the full fresh-install budget — the torch bytes are already on disk. The sized gate drops by
    torch's conservative footprint, but never below the resume floor."""
    project = _stage_project(tmp_path)
    _root, exe = _stage_root(tmp_path)
    env = {"SIDECAR_PORT": "5000", "MSA_TORCH_VARIANT": "cpu"}

    # Fresh venv (no ledger) → full fresh threshold, unchanged.
    assert provision.remaining_install_min_gb(
        env, exe=exe, project=project, nvidia_detector=lambda: False
    ) == pytest.approx(provision._MIN_FREE_GB)

    _partial_ledger_torch_done(env, exe, project)

    reduced = provision.remaining_install_min_gb(
        env, exe=exe, project=project, nvidia_detector=lambda: False
    )
    assert reduced == pytest.approx(provision._MIN_FREE_GB - provision._STAGE_INSTALLED_GB["torch"])
    assert reduced < provision._MIN_FREE_GB          # the whole point: below the fresh budget
    assert reduced >= provision._MIN_RESUME_FREE_GB  # never below the resume floor


def test_resume_proceeds_when_disk_below_fresh_but_adequate_for_remaining(tmp_path):
    """Finding B end-to-end: torch recorded complete + free disk BELOW the 5 GB fresh threshold but
    ABOVE the remaining-work requirement → the resume PROCEEDS (no raise). Pre-fix, the full fresh
    gate rejected every resume on a machine that barely fit the ≈2 GB torch install."""
    project = _stage_project(tmp_path)
    root, exe = _stage_root(tmp_path)
    env = {"SIDECAR_PORT": "5000", "MSA_TORCH_VARIANT": "cpu"}
    _partial_ledger_torch_done(env, exe, project)

    min_gb = provision.remaining_install_min_gb(
        env, exe=exe, project=project, nvidia_detector=lambda: False
    )
    # 3.5 GB: below the 5 GB fresh gate, above the reduced remaining gate (5 - 2 = 3).
    provision.preflight_system(
        root, platform_name="darwin", machine="arm64",
        disk_usage=_disk(3.5), min_free_gb=min_gb, check_disk=True,
    )  # no raise — the resume proceeds to finish the remaining stages


def test_resume_fails_when_disk_insufficient_for_remaining(tmp_path):
    """The relief is bounded: a resume with genuinely-insufficient space for the REMAINING work
    still fails loud with the actionable message (the S-2 kill-resume DoD is not weakened)."""
    project = _stage_project(tmp_path)
    root, exe = _stage_root(tmp_path)
    env = {"SIDECAR_PORT": "5000", "MSA_TORCH_VARIANT": "cpu"}
    _partial_ledger_torch_done(env, exe, project)

    min_gb = provision.remaining_install_min_gb(
        env, exe=exe, project=project, nvidia_detector=lambda: False
    )
    # 2.0 GB is below even the reduced remaining requirement (5 - 2 = 3) → still refused.
    with pytest.raises(provision.ProvisionError, match="free disk space"):
        provision.preflight_system(
            root, platform_name="darwin", machine="arm64",
            disk_usage=_disk(2.0), min_free_gb=min_gb, check_disk=True,
        )


def test_fresh_install_still_fails_on_low_disk(tmp_path):
    """A FRESH install (empty ledger) keeps the full 5 GB fresh threshold — the resume relief must
    not weaken the first-run gate."""
    project = _stage_project(tmp_path)
    root, exe = _stage_root(tmp_path)
    env = {"SIDECAR_PORT": "5000", "MSA_TORCH_VARIANT": "cpu"}

    min_gb = provision.remaining_install_min_gb(
        env, exe=exe, project=project, nvidia_detector=lambda: False
    )
    assert min_gb == pytest.approx(provision._MIN_FREE_GB)  # no ledger → full budget
    with pytest.raises(provision.ProvisionError, match="free disk space"):
        provision.preflight_system(
            root, platform_name="darwin", machine="arm64",
            disk_usage=_disk(4.5), min_free_gb=min_gb, check_disk=True,
        )


def test_ledger_refuses_to_write_under_datadir(tmp_path):
    """The ledger invariant: never persist provisioning state in durable user data. Pointing
    DataDir at the app-private root would put the marker under it — must be refused before any
    install runs."""
    project = _stage_project(tmp_path)
    root, exe = _stage_root(tmp_path)
    env = {"SIDECAR_PORT": "5000", "MSA_TORCH_VARIANT": "cpu", "MSA_DATA_DIR": str(root)}
    with pytest.raises(provision.ProvisionError, match="under DataDir"):
        provision.ensure_dependencies(
            env, exe=exe, project=project, runner=lambda c, e, l, on_line=None: 0, nvidia_detector=lambda: False
        )


def test_atomic_write_json_leaves_no_tmp_file(tmp_path):
    path = tmp_path / ".msa-deps.json"
    provision._atomic_write_json(path, {"a": 1})
    assert json.loads(path.read_text()) == {"a": 1}
    assert not (tmp_path / ".msa-deps.json.tmp").exists()  # temp replaced, not left behind


# ── config bootstrap + downgrade guard: DataDir invariant ────────────────────


def test_bootstrap_config_creates_when_absent(tmp_path):
    template = tmp_path / "config.yaml.template"
    template.write_text("api:\n  port: 8000\n")
    cfg_dir = tmp_path / "cfg"
    assert provision.bootstrap_config(cfg_dir, template) is True
    assert (cfg_dir / "config.yaml").read_text() == "api:\n  port: 8000\n"


def test_bootstrap_config_never_overwrites(tmp_path):
    template = tmp_path / "config.yaml.template"
    template.write_text("NEW\n")
    cfg_dir = tmp_path / "cfg"
    cfg_dir.mkdir()
    (cfg_dir / "config.yaml").write_text("EXISTING USER CONFIG\n")
    assert provision.bootstrap_config(cfg_dir, template) is False
    assert (cfg_dir / "config.yaml").read_text() == "EXISTING USER CONFIG\n"  # untouched


def test_check_downgrade_refuses_older(tmp_path):
    vf = tmp_path / "version.txt"
    vf.write_text("0.4.0\n")
    with pytest.raises(provision.ProvisionError, match="Refusing to downgrade"):
        provision.check_downgrade("0.3.2", vf)


def test_check_downgrade_allows_upgrade_and_fresh(tmp_path):
    vf = tmp_path / "version.txt"
    vf.write_text("0.3.0\n")
    provision.check_downgrade("0.4.0", vf)  # upgrade → no raise
    provision.check_downgrade("0.4.0", tmp_path / "absent.txt")  # fresh → no raise


def test_write_version_roundtrip(tmp_path):
    vf = tmp_path / "data" / "version.txt"
    provision.write_version(vf, "0.4.0")
    assert vf.read_text().strip() == "0.4.0"


# ── unified rotating desktop log (spec §S-2.5) ───────────────────────────────


def test_applog_configures_rotating_handler_and_writes(tmp_path, clean_root_logger):
    path = applog.configure(tmp_path)
    assert path == tmp_path / "msa-desktop.log"
    marked = [h for h in clean_root_logger.handlers if getattr(h, "_msa_desktop_unified", False)]
    assert len(marked) == 1

    applog.logger().info("shim lifecycle message")
    for h in marked:
        h.flush()
    assert "shim lifecycle message" in path.read_text(encoding="utf-8")


def test_applog_configure_is_idempotent(tmp_path, clean_root_logger):
    applog.configure(tmp_path)
    applog.configure(tmp_path)  # second call must not stack a second handle on the file (LOG-001)
    marked = [h for h in clean_root_logger.handlers if getattr(h, "_msa_desktop_unified", False)]
    assert len(marked) == 1


def test_applog_shares_sentinel_with_sidecar_mirror(tmp_path, clean_root_logger):
    """The shim's applog and the sidecar's mirror must agree on the sentinel so that within one
    shim→uvicorn process only ONE rotating handle exists on the file."""
    from msa_apps.search_api import sidecar

    applog.configure(tmp_path)
    # The sidecar mirror sees the shim's handler and no-ops (no second handler).
    sidecar.configure_unified_log(tmp_path)
    marked = [h for h in clean_root_logger.handlers if getattr(h, "_msa_desktop_unified", False)]
    assert len(marked) == 1


# ── responder: /health provisioning + error payloads, CORS ───────────────────


def test_responder_serves_provisioning_then_error(free_tcp_port):
    status = responder.ProvisionStatus()
    status.set_stage("deps-torch", 42, "Installing PyTorch", log="/tmp/p.log")
    r = responder.Responder(free_tcp_port, status)
    r.start()
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{free_tcp_port}/health", timeout=5) as resp:
            body = json.loads(resp.read())
        assert body == {
            "status": "provisioning", "stage": "deps-torch", "pct": 42,
            "detail": "Installing PyTorch", "log": "/tmp/p.log", "files": [],
        }
        status.fail("torch install failed", log="/tmp/p.log")
        with urllib.request.urlopen(f"http://127.0.0.1:{free_tcp_port}/health", timeout=5) as resp:
            body = json.loads(resp.read())
        assert body["status"] == "error" and body["detail"] == "torch install failed"
    finally:
        r.stop()


def test_responder_push_file_rolling_list_dedups_and_caps():
    """push_file feeds the setup screen's rolling file list: deduped against the most-recent entry
    (the in-flight file is emitted repeatedly), capped newest-last, blanks ignored."""
    status = responder.ProvisionStatus()
    status.push_file("torch-2.6.0+cu128.whl")
    status.push_file("torch-2.6.0+cu128.whl")  # same file still downloading — deduped
    status.push_file("")                          # blank ignored
    status.push_file("nvidia-cublas-cu12.whl")
    assert status.snapshot()["files"] == ["torch-2.6.0+cu128.whl", "nvidia-cublas-cu12.whl"]
    # cap: only the newest _MAX_FILES survive, in order
    for i in range(responder.ProvisionStatus._MAX_FILES + 3):
        status.push_file(f"pkg-{i}.whl")
    files = status.snapshot()["files"]
    assert len(files) == responder.ProvisionStatus._MAX_FILES
    assert files[-1] == f"pkg-{responder.ProvisionStatus._MAX_FILES + 2}.whl"


def test_responder_cors_preflight(free_tcp_port):
    status = responder.ProvisionStatus()
    r = responder.Responder(free_tcp_port, status)
    r.start()
    try:
        req = urllib.request.Request(
            f"http://127.0.0.1:{free_tcp_port}/health", method="OPTIONS",
            headers={"Origin": "tauri://localhost"},
        )
        with urllib.request.urlopen(req, timeout=5) as resp:
            assert resp.status == 204
            assert resp.headers.get("Access-Control-Allow-Origin") == "*"
    finally:
        r.stop()


def test_wait_for_free_port_after_stop(free_tcp_port):
    status = responder.ProvisionStatus()
    r = responder.Responder(free_tcp_port, status)
    r.start()
    r.stop()
    assert responder.wait_for_free_port(free_tcp_port) is True


# ── reap-safety: SIGTERM/watchdog armed BEFORE provisioning (PR #162 findings #1 + #3) ─


@pytest.fixture
def restore_signals():
    """Save/restore SIGTERM+SIGINT so a test that arms the reaper doesn't leak handlers into the
    rest of the run (``signal.signal`` is process-global)."""
    old_term = signal.getsignal(signal.SIGTERM)
    old_int = signal.getsignal(signal.SIGINT)
    try:
        yield
    finally:
        signal.signal(signal.SIGTERM, old_term)
        signal.signal(signal.SIGINT, old_int)


def test_reaper_installs_sigterm_but_no_watchdog_without_supervisor_pid(restore_signals):
    thread = shim._install_reaper({}, exit_fn=lambda _c: None)
    assert thread is None  # no SUPERVISOR_PID ⇒ handler only, no watchdog thread
    assert signal.getsignal(signal.SIGTERM) not in (signal.SIG_DFL, signal.SIG_IGN)


def test_reaper_watchdog_exits_when_supervisor_dies(restore_signals):
    """The parent-watchdog ``os._exit(0)``s the moment the supervisor is gone — the scenario a
    hard-kill during the minutes-long first-run install used to leave uncovered."""
    exited: dict[str, int] = {}
    fired = threading.Event()
    ticks = {"n": 0}

    def fake_exit(code: int) -> None:
        exited["code"] = code
        fired.set()

    def present() -> bool:
        ticks["n"] += 1
        return ticks["n"] < 2  # alive during the first check, gone on the second

    thread = shim._install_reaper(
        {"SUPERVISOR_PID": "424242"},
        exit_fn=fake_exit,
        sleep=lambda _s: None,
        present=present,
    )
    assert thread is not None
    assert fired.wait(timeout=2.0), "watchdog did not fire after the supervisor 'died'"
    assert exited["code"] == 0


def test_main_arms_reaper_before_ensure_dependencies(
    tmp_path, monkeypatch, free_tcp_port, restore_signals, clean_root_logger
):
    """The SIGTERM handler and parent-watchdog must be live at the moment provisioning starts —
    not only after it finishes (the whole ≈2 GB install window must be orphan-safe)."""
    captured: dict[str, object] = {}

    def fake_ensure(*_a, **_k):
        captured["sigterm"] = signal.getsignal(signal.SIGTERM)
        captured["watchdog_alive"] = any(
            t.name == "shim-parent-watchdog" and t.is_alive() for t in threading.enumerate()
        )
        raise RuntimeError("boom")  # route through the failure fallback (which must not block)

    monkeypatch.setattr(provision, "ensure_dependencies", fake_ensure)
    monkeypatch.setattr(shim, "_hold_error_state_inline", lambda: None)
    monkeypatch.setenv("SIDECAR_PORT", str(free_tcp_port))
    monkeypatch.setenv("SUPERVISOR_PID", str(os.getpid()))  # our own pid ⇒ watchdog sees it alive
    monkeypatch.setenv("MSA_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("MSA_CACHE_DIR", str(tmp_path / "cache"))
    monkeypatch.setenv("MSA_LOG_DIR", str(tmp_path / "logs"))

    shim.main()

    assert captured["sigterm"] not in (signal.SIG_DFL, signal.SIG_IGN)
    assert captured["watchdog_alive"] is True


def test_main_skips_disk_gate_on_warm_launch(
    tmp_path, monkeypatch, free_tcp_port, restore_signals, clean_root_logger
):
    """Warm launch (deps already complete): main() calls preflight_system with check_disk=False so
    a since-shrunk disk doesn't block a start that downloads nothing (spec §S-2.4)."""
    captured: dict[str, object] = {}
    monkeypatch.setattr(provision, "dependencies_complete", lambda *_a, **_k: True)

    def fake_preflight(*_a, check_disk=True, **_k):
        captured["check_disk"] = check_disk

    monkeypatch.setattr(provision, "preflight_system", fake_preflight)

    def stop_here(*_a, **_k):
        raise RuntimeError("stop-here")  # halt before the real backend handoff

    # Stop the flow at the install step so we don't spin up the real backend sidecar.
    monkeypatch.setattr(provision, "ensure_dependencies", stop_here)
    monkeypatch.setattr(shim, "_hold_error_state_inline", lambda: None)
    monkeypatch.setenv("SIDECAR_PORT", str(free_tcp_port))
    monkeypatch.setenv("MSA_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("MSA_CACHE_DIR", str(tmp_path / "cache"))
    monkeypatch.setenv("MSA_LOG_DIR", str(tmp_path / "logs"))
    monkeypatch.delenv("SUPERVISOR_PID", raising=False)

    shim.main()

    assert captured["check_disk"] is False  # warm launch → disk gate skipped


def test_main_runs_disk_gate_when_provisioning_needed(
    tmp_path, monkeypatch, free_tcp_port, restore_signals, clean_root_logger
):
    """First run / resume (deps NOT complete): the disk gate runs (check_disk=True) and a low-disk
    ProvisionError routes to the error-hold fallback — an actionable fail, not a silent proceed.
    This is the S-2 kill-resume DoD: a provisioning-needed launch must still surface low disk."""
    captured: dict[str, object] = {}
    monkeypatch.setattr(provision, "dependencies_complete", lambda *_a, **_k: False)

    def fake_preflight(*_a, check_disk=True, **_k):
        captured["check_disk"] = check_disk
        if check_disk:
            raise provision.ProvisionError("Not enough free disk space: ... Free up space and relaunch.")

    monkeypatch.setattr(provision, "preflight_system", fake_preflight)
    held = {"failed": None}
    monkeypatch.setattr(shim, "_hold_error_state_inline", lambda: None)
    orig_fail = responder.ProvisionStatus.fail

    def spy_fail(self, detail, log=""):
        held["failed"] = detail
        return orig_fail(self, detail, log=log)

    monkeypatch.setattr(responder.ProvisionStatus, "fail", spy_fail)
    monkeypatch.setenv("SIDECAR_PORT", str(free_tcp_port))
    monkeypatch.setenv("MSA_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("MSA_CACHE_DIR", str(tmp_path / "cache"))
    monkeypatch.setenv("MSA_LOG_DIR", str(tmp_path / "logs"))
    monkeypatch.delenv("SUPERVISOR_PID", raising=False)

    shim.main()

    assert captured["check_disk"] is True  # provisioning needed → disk gate runs
    assert held["failed"] and "free disk space" in held["failed"]  # actionable error surfaced


@pytest.fixture
def free_tcp_port():
    import socket

    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


# ── uv subtree reaping: the reaper kills the in-flight uv child, not just the shim ───
#
# PR #162 follow-on: os._exit(0) in the reaper does NOT cascade to the uv install child, so uv is
# launched in its OWN session/process-group (provision._default_runner) and terminated by the
# SIGTERM handler AND the parent-watchdog BEFORE exit. These drive that same launch path with a
# stand-in long-running child and assert the whole process group is reaped — no orphan.
# POSIX-guarded (CI is macOS/Linux); the Windows taskkill path isn't exercised here.

_POSIX_ONLY = pytest.mark.skipif(sys.platform == "win32", reason="POSIX process-group reaping")

# Stand-in for `uv`: sleeps long enough to still be running when the reaper fires.
_SLEEPER = [sys.executable, "-c", "import time; time.sleep(30)"]


def _spawn_via_runner(cmd):
    """Run provision._default_runner on a daemon thread (it blocks in communicate() until the
    child exits) and return (thread, Popen) once the child is registered — the exact launch path
    uv uses during provisioning."""
    t = threading.Thread(
        target=lambda: provision._default_runner(cmd, dict(os.environ), None), daemon=True
    )
    t.start()
    for _ in range(300):  # ≤ ~3 s for the child to launch + register
        child = provision._active_child
        if child is not None:
            return t, child
        time.sleep(0.01)
    raise AssertionError("provisioning child was never registered")


def _pid_dead(pid):
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return True
    except PermissionError:
        return False
    return False


def _group_gone(pgid):
    """True once the process group is empty — no member (incl. the leader) survives."""
    try:
        os.killpg(pgid, 0)
    except ProcessLookupError:
        return True
    except PermissionError:
        return False
    return False


def _wait_until(pred, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if pred():
            return True
        time.sleep(0.02)
    return pred()


@pytest.fixture
def reset_active_child():
    """Ensure no registered provisioning child leaks a 30 s sleeper into the rest of the run if an
    assertion fails mid-test."""
    try:
        yield
    finally:
        child = provision._active_child
        provision._active_child = None
        if child is not None and child.poll() is None:
            try:
                if sys.platform != "win32":
                    os.killpg(os.getpgid(child.pid), signal.SIGKILL)
                else:
                    child.kill()
            except OSError:
                pass


@_POSIX_ONLY
def test_default_runner_launches_child_in_own_session(reset_active_child):
    """The launch path isolates uv in its own session/process-group so killpg reaps the subtree,
    and registers it for the reaper to find."""
    _thread, child = _spawn_via_runner(_SLEEPER)
    assert os.getpgid(child.pid) == child.pid  # start_new_session ⇒ child is the group leader
    assert provision._active_child is child


@_POSIX_ONLY
def test_sigterm_handler_reaps_uv_subtree(restore_signals, reset_active_child):
    """The installed SIGTERM handler must terminate the in-flight uv process group before exit —
    the orphan this fix closes. Invokes the handler directly (no real signal ⇒ no risk to the test
    runner) with a stub exit_fn."""
    _thread, child = _spawn_via_runner(_SLEEPER)
    pgid = os.getpgid(child.pid)
    exited: dict[str, int] = {}
    shim._install_reaper({}, exit_fn=lambda code: exited.setdefault("code", code))

    handler = signal.getsignal(signal.SIGTERM)
    assert callable(handler), "reaper did not install a callable SIGTERM handler"
    handler(signal.SIGTERM, None)  # reap uv subtree → exit_fn(0)

    assert exited.get("code") == 0
    assert _wait_until(lambda: _pid_dead(child.pid)), "uv child survived the SIGTERM handler"
    assert _wait_until(lambda: _group_gone(pgid)), "uv process group survived the SIGTERM handler"


@_POSIX_ONLY
def test_watchdog_reaps_uv_subtree(restore_signals, reset_active_child):
    """The parent-watchdog must also kill the in-flight uv process group before os._exit(0)."""
    _thread, child = _spawn_via_runner(_SLEEPER)
    pgid = os.getpgid(child.pid)
    fired = threading.Event()
    ticks = {"n": 0}

    def present():
        ticks["n"] += 1
        return ticks["n"] < 2  # supervisor 'dies' on the second liveness check

    thread = shim._install_reaper(
        {"SUPERVISOR_PID": "424242"},
        exit_fn=lambda _c: fired.set(),
        sleep=lambda _s: None,
        present=present,
    )
    assert thread is not None
    assert fired.wait(timeout=3.0), "watchdog never fired after the supervisor 'died'"
    assert _wait_until(lambda: _pid_dead(child.pid)), "uv child survived the watchdog"
    assert _wait_until(lambda: _group_gone(pgid)), "uv process group survived the watchdog"


# ── headless provisioning entry: `python -m app.provision` (S-3 item 4) ───────


def test_headless_main_rejects_non_venv_interpreter(monkeypatch):
    """Run outside a <root>/.venv/<bin>/python layout (system python) -> exit 1 with guidance."""
    monkeypatch.setattr(provision, "app_private_root", lambda *a, **k: None)
    assert provision.headless_main() == 1


def test_headless_main_provisions_like_a_gui_first_run(tmp_path, monkeypatch):
    """The happy path does exactly what a GUI first run does minus the uvicorn handoff: the guarded
    legacy sweep -> preflight -> ensure_dependencies -> config bootstrap -> version stamp, then exit
    0. The sweep (finding: claude[bot] #4) must be in the headless sequence — first, gated to first
    run, before the disk gate — exactly as the GUI shim runs it. Every heavy step is injected so this
    stays offline (the sweep is stubbed so it never touches the real machine)."""
    root = tmp_path / "root"
    (root / ".venv").mkdir(parents=True)
    calls: list[str] = []
    monkeypatch.setattr(provision, "app_private_root", lambda *a, **k: root)
    monkeypatch.setattr(provision, "resolved_dirs", lambda: {
        "data": tmp_path / "data", "config": tmp_path / "data",
        "cache": tmp_path / "cache", "log": tmp_path / "log",
    })
    monkeypatch.setattr(provision, "staged_project_dir", lambda: tmp_path / "proj")
    (tmp_path / "proj").mkdir()
    (tmp_path / "proj" / "pyproject.toml").write_text("[project]\nname='x'\nversion='0.3.2'\n")
    monkeypatch.setattr(provision, "app_version", lambda *_a, **_k: "0.3.2")
    monkeypatch.setattr(provision, "dependencies_complete", lambda *_a, **_k: False)
    monkeypatch.setattr(provision, "remaining_install_min_gb", lambda *_a, **_k: 5.0)
    # The headless path must run the SAME guarded sweep the GUI shim runs (one code path). Stub it
    # here so the offline suite records the call without touching the real filesystem.
    monkeypatch.setattr(migration, "run_first_run_sweep", lambda *a, **k: calls.append("sweep"))
    monkeypatch.setattr(provision, "preflight_system", lambda *a, **k: calls.append("preflight"))
    monkeypatch.setattr(provision, "check_downgrade", lambda *a, **k: calls.append("downgrade"))
    monkeypatch.setattr(provision, "ensure_dependencies", lambda **k: calls.append("deps") or root)
    monkeypatch.setattr(provision, "bootstrap_config", lambda *a, **k: calls.append("config") or True)
    monkeypatch.setattr(provision, "write_version", lambda *a, **k: calls.append("version"))

    assert provision.headless_main() == 0
    assert calls == ["sweep", "preflight", "downgrade", "deps", "config", "version"]


def test_headless_main_reports_provision_error(tmp_path, monkeypatch):
    root = tmp_path / "root"
    (root / ".venv").mkdir(parents=True)
    monkeypatch.setattr(provision, "app_private_root", lambda *a, **k: root)
    monkeypatch.setattr(provision, "resolved_dirs", lambda: {
        "data": tmp_path / "d", "config": tmp_path / "d", "cache": tmp_path / "c", "log": tmp_path / "l"})
    monkeypatch.setattr(provision, "staged_project_dir", lambda: tmp_path / "proj")
    (tmp_path / "proj").mkdir()
    (tmp_path / "proj" / "pyproject.toml").write_text("[project]\nname='x'\nversion='0.3.2'\n")
    monkeypatch.setattr(provision, "dependencies_complete", lambda *_a, **_k: False)
    monkeypatch.setattr(provision, "remaining_install_min_gb", lambda *_a, **_k: 5.0)
    # The first-run sweep runs before the disk gate; stub it so this offline test never touches the
    # real machine's legacy artifacts (needs=True here routes through the sweep).
    monkeypatch.setattr(migration, "run_first_run_sweep", lambda *a, **k: None)

    def _boom(*a, **k):
        raise provision.ProvisionError("Not enough free disk space")

    monkeypatch.setattr(provision, "preflight_system", _boom)
    assert provision.headless_main() == 1


def test_headless_main_reports_missing_staged_project(tmp_path, monkeypatch):
    root = tmp_path / "root"
    (root / ".venv").mkdir(parents=True)
    monkeypatch.setattr(provision, "app_private_root", lambda *a, **k: root)
    monkeypatch.setattr(provision, "resolved_dirs", lambda: {
        "data": tmp_path / "d", "config": tmp_path / "d", "cache": tmp_path / "c", "log": tmp_path / "l"})
    monkeypatch.setattr(provision, "staged_project_dir", lambda: tmp_path / "missing")
    assert provision.headless_main() == 1


# ── _preload_app: import the app while the responder still owns /health ──────
#
# The first-launch fix for the 2026-07-10 field failure (arch doc §10 risk #6): a transient
# import failure (e.g. AV locking just-written site-packages) must be retried, and a persistent
# one must land in the responder's pollable error state — never a silent thread death after the
# responder is gone. Driven offline via the injectable import_module/sleep hooks.


def _preload_logger(name="test.preload"):
    import logging

    return logging.getLogger(name)


def test_preload_app_succeeds_first_try(tmp_path):
    calls = []
    status = responder.ProvisionStatus()
    ok = shim._preload_app(
        _preload_logger(), status, tmp_path / "prov.log",
        import_module=lambda mod: calls.append(mod),
        sleep=lambda s: (_ for _ in ()).throw(AssertionError("no backoff on success")),
    )
    assert ok is True
    assert calls == [shim._APP_MODULE]
    assert status.snapshot()["status"] == "provisioning"  # never flipped to error


def test_preload_app_retries_transient_failure_then_succeeds(caplog, tmp_path):
    import logging

    n = {"c": 0}

    def flaky(mod):
        n["c"] += 1
        if n["c"] <= 2:  # models the AV window: locked, locked, then released
            raise ImportError("DLL load failed while importing _torch: access denied")

    sleeps = []
    status = responder.ProvisionStatus()
    with caplog.at_level(logging.ERROR, logger="test.preload"):
        ok = shim._preload_app(
            _preload_logger(), status, tmp_path / "prov.log",
            import_module=flaky, sleep=sleeps.append,
        )
    assert ok is True
    assert n["c"] == 3
    assert sleeps == [2.0, 4.0]  # exponential backoff between attempts
    failed = [r for r in caplog.records if "pre-load failed" in r.message]
    assert len(failed) == 2
    assert all(r.exc_info is not None for r in failed)  # full traceback, not a one-liner
    assert status.snapshot()["status"] == "provisioning"  # recovered — no error state


def test_preload_app_exhaustion_flips_responder_to_error(caplog, tmp_path):
    import logging

    def always_locked(mod):
        raise PermissionError("The process cannot access the file")

    sleeps = []
    status = responder.ProvisionStatus()
    with caplog.at_level(logging.ERROR, logger="test.preload"):
        ok = shim._preload_app(
            _preload_logger(), status, tmp_path / "prov.log",
            import_module=always_locked, sleep=sleeps.append,
        )
    assert ok is False
    assert sleeps == [2.0, 4.0, 8.0]  # 4 attempts → 3 backoffs, none after the last
    snap = status.snapshot()
    assert snap["status"] == "error"  # the splash gets a pollable error, not a dead /health
    assert "Backend failed to load" in snap["detail"]
    assert snap["log"] == str(tmp_path / "prov.log")  # open-logs affordance points somewhere real
    assert len([r for r in caplog.records if "pre-load failed" in r.message]) == 4


# ── _install_thread_excepthook: no thread may die silently ───────────────────


@pytest.mark.filterwarnings("ignore::pytest.PytestUnhandledThreadExceptionWarning")
def test_thread_excepthook_logs_uncaught_thread_exception(caplog, monkeypatch):
    # The chained previous hook is pytest's own thread-exception recorder here — its warning is
    # exactly the "default behavior preserved" property we want, so silence it for this test only.
    import logging

    monkeypatch.setattr(threading, "excepthook", threading.excepthook)  # snapshot/auto-restore
    log = _preload_logger("test.excepthook")
    shim._install_thread_excepthook(log)

    def boom():
        raise RuntimeError("worker exploded")

    with caplog.at_level(logging.ERROR, logger="test.excepthook"):
        t = threading.Thread(target=boom, name="doomed-worker")
        t.start()
        t.join(timeout=5.0)

    records = [r for r in caplog.records if "uncaught exception in thread" in r.message]
    assert records, "expected the excepthook to log the thread's death"
    assert "doomed-worker" in records[0].getMessage()
    assert records[0].exc_info is not None  # full traceback in msa-desktop.log, not raw stderr


# ── _refresh_site_packages: .pth files installed mid-process become importable ─
#
# The confirmed cold-first-launch killer (2026-07-11, pywintypes): Python processes
# site-packages .pth files only at interpreter startup, so a package installed DURING the
# provisioning process whose importability rides on a .pth (pywin32-style) is invisible until
# site.addsitedir re-scans. Simulated offline with a fake .pth + package dir.


def test_refresh_site_packages_makes_pth_dirs_importable(tmp_path, monkeypatch):
    import importlib
    import sysconfig

    site_dir = tmp_path / "site-packages"
    lib_dir = site_dir / "fake_win32" / "lib"
    lib_dir.mkdir(parents=True)
    (lib_dir / "fake_pywintypes.py").write_text("MARKER = 'loaded'\n", encoding="utf-8")
    # pywin32.pth-style: a relative dir entry that site must add to sys.path.
    (site_dir / "fake_pywin32.pth").write_text("fake_win32/lib\n", encoding="utf-8")
    monkeypatch.setattr(
        sysconfig, "get_paths", lambda: {"purelib": str(site_dir), "platlib": str(site_dir)}
    )

    # Invisible before the refresh — the interpreter started without this .pth processed.
    with pytest.raises(ModuleNotFoundError):
        importlib.import_module("fake_pywintypes")

    shim._refresh_site_packages(_preload_logger())
    try:
        mod = importlib.import_module("fake_pywintypes")
        assert mod.MARKER == "loaded"  # the .pth dir is now on sys.path — pywintypes-class fixed
    finally:
        sys.path[:] = [p for p in sys.path if str(tmp_path) not in p]
        sys.modules.pop("fake_pywintypes", None)


def test_refresh_site_packages_never_raises(tmp_path, monkeypatch):
    """A site rescan must never block the launch — bogus paths are logged and skipped."""
    import sysconfig

    monkeypatch.setattr(
        sysconfig, "get_paths",
        lambda: {"purelib": str(tmp_path / "does-not-exist"), "platlib": None},
    )
    shim._refresh_site_packages(_preload_logger())  # absent dir + None: both no-ops, no raise
