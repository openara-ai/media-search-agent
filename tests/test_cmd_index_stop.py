"""Tests for `msa index stop` — the cooperative-stop CLI helper.

The CLI writes the same sentinel file that IndexerManager.stop() writes, then
waits for the indexer subprocess to exit. These tests drive _handle_stop
directly against fake indexer subprocesses to validate the contract
end-to-end without spinning up the API.
"""
from __future__ import annotations

import os
import subprocess
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest


def _stub_pipeline_module(monkeypatch: pytest.MonkeyPatch):
    """Install a stub `msa_indexer.pipeline` in sys.modules so `cmd_index.handle()`'s
    `from msa_indexer.pipeline import run_index, ...` resolves without importing
    the real (heavy) module — which transitively pulls in PIL / torch / etc.

    Using `monkeypatch.setattr("msa_indexer.pipeline.run_index", ...)` forces the
    real import to happen first (so setattr has a real module to patch), which
    fails in lightweight environments lacking PIL. By installing the stub in
    sys.modules BEFORE handle() runs, the `from ... import` finds the stub
    and skips the real import chain entirely.

    Returns the stub module so the test can install its own `run_index`,
    `run_export`, etc. as needed.
    """
    import sys as _sys
    import types as _types

    fake = _types.ModuleType("msa_indexer.pipeline")
    # Default no-op implementations; tests override these to capture state
    # or assert call ordering.
    fake.run_index = lambda cfg, stop_event=None: None
    fake.run_export = lambda cfg: None
    fake.run_dry_run = lambda cfg: None
    fake.run_export_dry_run = lambda cfg: None
    monkeypatch.setitem(_sys.modules, "msa_indexer.pipeline", fake)
    return fake


def _isolate_run_dir(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> dict:
    """Point MSA_LOG_DIR + log_dir at tmp_path so cmd_index stop hits an
    isolated run_dir / pid_file / stop_file / msa_log."""
    log_dir = tmp_path / "logs"
    log_dir.mkdir()
    run_dir = log_dir / "run"
    run_dir.mkdir()
    config_path = tmp_path / "config.yaml"
    config_path.write_text(f"log_dir: {log_dir}\n")
    monkeypatch.setenv("MSA_LOG_DIR", str(log_dir))
    monkeypatch.setenv("MSA_CONFIG_PATH", str(config_path))
    return {
        "config_path": config_path,
        "log_dir": log_dir,
        "run_dir": run_dir,
        "pid_file": run_dir / "indexer.pid",
        "stop_file": run_dir / "indexer.stop",
        "msa_log": log_dir / "msa.log",
    }


def _spawn_with_reaper(script: str, ready_file: Path) -> subprocess.Popen:
    """Spawn a subprocess in its own session/group, start a background reaper,
    and block until the fake creates `ready_file` (signal handlers registered).

    Two things matter:
    - **Reaper**: on POSIX, os.kill(pid, 0) returns success for a zombie (exited
      child whose parent hasn't wait()ed yet). In production the indexer's
      parent (uvicorn / IndexerManager._monitor) reaps it via proc.wait(), so
      _stop_pid_alive correctly reports dead. Mirrored here.
    - **Ready file**: SIGTERM from _handle_stop arrives within milliseconds of
      writing the PID file. If the fake hasn't yet registered its SIG_IGN
      handler, default SIGTERM action terminates it and the timeout test
      passes for the wrong reason. The fake writes ready_file *after* setting
      up its handlers, and the caller blocks until then.
    """
    popen_kwargs: dict = {}
    if sys.platform == "win32":
        popen_kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        popen_kwargs["start_new_session"] = True
    # The trailing "msa_indexer-fake-for-test" arg shows up in the process's
    # cmdline (via ps / /proc/<pid>/cmdline / WMI Win32_Process). This makes
    # _stop_pid_is_indexer recognise the fake as a legitimate indexer so
    # `_handle_stop` doesn't refuse to signal it. Tests that need to verify
    # the *opposite* (refuse-to-signal a non-indexer PID) spawn their own
    # subprocess without this marker.
    proc = subprocess.Popen(
        [sys.executable, "-c", script, "msa_indexer-fake-for-test"],
        **popen_kwargs,
    )
    threading.Thread(target=proc.wait, daemon=True).start()
    deadline = time.monotonic() + 5.0
    while time.monotonic() < deadline:
        if ready_file.exists():
            return proc
        if proc.poll() is not None:
            raise RuntimeError(
                f"Fake indexer exited before signaling ready (rc={proc.returncode})"
            )
        time.sleep(0.05)
    proc.kill()
    raise RuntimeError("Fake indexer did not signal ready within 5s")


def _spawn_fake_indexer_responsive(
    stop_file: Path, ready_file: Path
) -> subprocess.Popen:
    """A subprocess that polls `stop_file` and exits cleanly when it appears —
    mimics the cmd_index.py watcher thread."""
    script = (
        "import os, sys, time\n"
        f"stop_file = {str(stop_file)!r}\n"
        f"ready_file = {str(ready_file)!r}\n"
        "open(ready_file, 'w').close()\n"
        "for _ in range(200):  # up to 100s\n"
        "    if os.path.exists(stop_file):\n"
        "        sys.exit(0)\n"
        "    time.sleep(0.5)\n"
        "sys.exit(0)\n"
    )
    return _spawn_with_reaper(script, ready_file)


def _spawn_fake_indexer_ignoring(
    stop_file: Path, ready_file: Path
) -> subprocess.Popen:
    """A subprocess that ignores the sentinel and just sleeps. Used for the
    timeout test. On POSIX, also ignore SIGTERM so the kill fast-path from
    _handle_stop doesn't reap it before --wait elapses.
    """
    script = (
        "import signal, sys, time\n"
        f"ready_file = {str(ready_file)!r}\n"
        "if hasattr(signal, 'SIGTERM'):\n"
        "    signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
        "if hasattr(signal, 'SIGBREAK'):\n"
        "    signal.signal(signal.SIGBREAK, signal.SIG_IGN)\n"
        "open(ready_file, 'w').close()\n"
        "time.sleep(120)\n"
    )
    return _spawn_with_reaper(script, ready_file)


def _kill_subprocess(proc: subprocess.Popen) -> None:
    if proc.poll() is None:
        try:
            proc.kill()
        except (ProcessLookupError, OSError):
            pass
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        pass


def test_stop_no_pid_file(tmp_path, monkeypatch, capsys):
    """No PID file → prints 'No indexer is currently running' and returns 0."""
    paths = _isolate_run_dir(monkeypatch, tmp_path)
    from msa_cli import cmd_index

    args = SimpleNamespace(config=str(paths["config_path"]), wait=10.0, quiet=False)
    cmd_index._handle_stop(args)
    out = capsys.readouterr().out
    assert "No indexer is currently running" in out
    assert str(paths["pid_file"]) in out


def test_stop_stale_pid_cleans_up(tmp_path, monkeypatch, capsys):
    """PID file points at a dead PID → reports it and removes the stale file."""
    paths = _isolate_run_dir(monkeypatch, tmp_path)
    # Write a definitely-dead PID by spawning then waiting.
    proc = subprocess.Popen([sys.executable, "-c", "pass"])
    proc.wait(timeout=5)
    paths["pid_file"].write_text(str(proc.pid))
    from msa_cli import cmd_index

    args = SimpleNamespace(config=str(paths["config_path"]), wait=10.0, quiet=False)
    cmd_index._handle_stop(args)
    out = capsys.readouterr().out
    assert "Stale PID" in out
    assert not paths["pid_file"].exists(), (
        "stop() must clean up a stale PID file so the next start isn't blocked"
    )


def test_stop_refuses_to_signal_non_indexer_pid(tmp_path, monkeypatch, capsys):
    """If the PID in indexer.pid is alive but its cmdline is NOT `msa index
    run`, stop must treat it as stale and refuse to signal — protects against
    the case where the kernel reused the indexer's PID for an unrelated
    process after a crash that didn't unlink the PID file. Without this
    check, `msa index stop` would SIGTERM the wrong process.
    """
    paths = _isolate_run_dir(monkeypatch, tmp_path)

    # Spawn a long-running subprocess whose cmdline does NOT contain
    # "msa index run" — mimics a PID reused by some other program.
    not_an_indexer = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(60)"],
    )
    try:
        paths["pid_file"].write_text(str(not_an_indexer.pid))
        from msa_cli import cmd_index

        args = SimpleNamespace(
            config=str(paths["config_path"]), wait=5.0,
            quiet=False, require_running=False,
        )
        cmd_index._handle_stop(args)
        out = capsys.readouterr().out

        # Must report the PID as not-an-indexer and clean up the stale file.
        assert "cmdline does not match" in out or "PID has been reused" in out, (
            f"Expected explicit non-indexer-PID message, got:\n{out}"
        )
        assert not paths["pid_file"].exists(), (
            "Stale (PID-reused) PID file must be unlinked"
        )
        # Critical: the unrelated subprocess must still be running. If
        # _handle_stop had signaled it, it would be dead by now.
        assert not_an_indexer.poll() is None, (
            "stop() signaled an unrelated process whose PID happened to match. "
            "This is the safety bug fix is preventing — see Codex P1 / Copilot."
        )
        # Sentinel must NOT have been written either (we never reached
        # the write step in _handle_stop).
        assert not paths["stop_file"].exists()
    finally:
        not_an_indexer.kill()
        try:
            not_an_indexer.wait(timeout=5)
        except subprocess.TimeoutExpired:
            pass


def test_stop_refuses_non_indexer_pid_exits_one_in_require_running(
    tmp_path, monkeypatch, capsys
):
    """Same as above but with --require-running: must exit 1, not 0."""
    paths = _isolate_run_dir(monkeypatch, tmp_path)
    not_an_indexer = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(60)"],
    )
    try:
        paths["pid_file"].write_text(str(not_an_indexer.pid))
        from msa_cli import cmd_index

        args = SimpleNamespace(
            config=str(paths["config_path"]), wait=5.0,
            quiet=False, require_running=True,
        )
        with pytest.raises(SystemExit) as exc:
            cmd_index._handle_stop(args)
        assert exc.value.code == 1
        assert not_an_indexer.poll() is None, (
            "stop() must not signal a non-indexer PID even in --require-running mode"
        )
    finally:
        not_an_indexer.kill()
        try:
            not_an_indexer.wait(timeout=5)
        except subprocess.TimeoutExpired:
            pass


def test_stop_cooperative_exits_within_wait(tmp_path, monkeypatch, capsys):
    """Fake indexer that exits on sentinel → _handle_stop reports clean exit."""
    paths = _isolate_run_dir(monkeypatch, tmp_path)
    proc = _spawn_fake_indexer_responsive(paths["stop_file"], tmp_path / "ready")
    try:
        paths["pid_file"].write_text(str(proc.pid))
        from msa_cli import cmd_index

        args = SimpleNamespace(config=str(paths["config_path"]), wait=10.0, quiet=False)
        cmd_index._handle_stop(args)
        out = capsys.readouterr().out
        assert "Stop requested for indexer (PID" in out
        assert "Indexer stopped cleanly" in out, (
            f"Expected clean-exit message; got:\n{out}"
        )
        # Sentinel cleaned up after successful stop.
        assert not paths["stop_file"].exists()
    finally:
        _kill_subprocess(proc)


def test_stop_timeout_exits_nonzero(tmp_path, monkeypatch, capsys):
    """Fake indexer that ignores sentinel + SIGTERM → command exits 1 after wait."""
    paths = _isolate_run_dir(monkeypatch, tmp_path)
    proc = _spawn_fake_indexer_ignoring(paths["stop_file"], tmp_path / "ready")
    try:
        paths["pid_file"].write_text(str(proc.pid))
        from msa_cli import cmd_index

        args = SimpleNamespace(config=str(paths["config_path"]), wait=2.0, quiet=True)
        with pytest.raises(SystemExit) as exc:
            cmd_index._handle_stop(args)
        assert exc.value.code == 1
        out = capsys.readouterr().out
        assert "did not exit within" in out
        assert "still pending" in out
    finally:
        _kill_subprocess(proc)


def test_stop_require_running_exits_one_when_no_pid_file(tmp_path, monkeypatch, capsys):
    """--require-running flips the no-indexer case from idempotent rc=0 to
    rc=1. BVT scripts use this so 'no indexer found' is a hard failure —
    otherwise Phase A passes for the wrong reason if PID publication regresses.
    """
    paths = _isolate_run_dir(monkeypatch, tmp_path)
    from msa_cli import cmd_index

    args = SimpleNamespace(
        config=str(paths["config_path"]), wait=10.0,
        quiet=False, require_running=True,
    )
    with pytest.raises(SystemExit) as exc:
        cmd_index._handle_stop(args)
    assert exc.value.code == 1
    assert "No indexer is currently running" in capsys.readouterr().out


def test_stop_require_running_exits_one_for_stale_pid(tmp_path, monkeypatch, capsys):
    """--require-running treats a stale PID file the same as no PID file:
    the indexer wasn't actually there to stop."""
    paths = _isolate_run_dir(monkeypatch, tmp_path)
    proc = subprocess.Popen([sys.executable, "-c", "pass"])
    proc.wait(timeout=5)
    paths["pid_file"].write_text(str(proc.pid))
    from msa_cli import cmd_index

    args = SimpleNamespace(
        config=str(paths["config_path"]), wait=10.0,
        quiet=False, require_running=True,
    )
    with pytest.raises(SystemExit) as exc:
        cmd_index._handle_stop(args)
    assert exc.value.code == 1
    out = capsys.readouterr().out
    assert "Stale PID" in out
    # Stale PID still gets cleaned up regardless of mode.
    assert not paths["pid_file"].exists()


def test_stop_quiet_suppresses_progress(tmp_path, monkeypatch, capsys):
    """--quiet suppresses the per-poll progress lines and the success summary."""
    paths = _isolate_run_dir(monkeypatch, tmp_path)
    proc = _spawn_fake_indexer_responsive(paths["stop_file"], tmp_path / "ready")
    try:
        paths["pid_file"].write_text(str(proc.pid))
        from msa_cli import cmd_index

        args = SimpleNamespace(config=str(paths["config_path"]), wait=10.0, quiet=True)
        cmd_index._handle_stop(args)
        out = capsys.readouterr().out
        assert "Stop requested" not in out
        assert "Indexer stopped cleanly" not in out
    finally:
        _kill_subprocess(proc)


def test_run_branch_clears_stale_stop_sentinel(tmp_path, monkeypatch):
    """Standalone `msa index run` must clear any stale `indexer.stop` from a
    prior interrupted run BEFORE starting the watcher thread. Otherwise the
    watcher's first poll would see the leftover file, set stop_event, and
    the pipeline would exit before processing any files.

    A stale sentinel can survive:
      - A timed-out `msa index stop` (returns rc=1 without unlinking).
      - A crash between sentinel write and exit.
      - Manual interruption (Ctrl-C, etc.) of `msa index stop`.

    Mirrors the API-side invariant covered by
    `test_start_clears_stale_stop_sentinel` in test_indexer_manager.py.
    Verifies in-process by stubbing run_index — when run_index is called,
    the stale sentinel must already be gone and stop_event must NOT be set.
    """
    log_dir = tmp_path / "logs"
    log_dir.mkdir()
    run_dir = log_dir / "run"
    run_dir.mkdir()
    index_dir = tmp_path / "index"
    index_dir.mkdir()

    sentinel = run_dir / "indexer.stop"
    sentinel.write_text("from-previous-interrupted-run")
    assert sentinel.exists()

    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        f"log_dir: {log_dir}\n"
        f"index_dir: {index_dir}\n"
        f"sqlite_path: {index_dir / 'media.sqlite'}\n"
        "media_sources: []\n"
        "log_level: WARNING\n"
    )

    monkeypatch.setenv("MSA_LOG_DIR", str(log_dir))
    monkeypatch.setenv("MSA_CONFIG_PATH", str(config_path))

    # Stub the pipeline module BEFORE handle() runs so the import resolves
    # without pulling in PIL / torch — see _stub_pipeline_module().
    fake_pipeline = _stub_pipeline_module(monkeypatch)

    # Capture state at the exact moment run_index is invoked.
    observations: dict = {}

    def fake_run_index(cfg, stop_event=None):
        observations["sentinel_exists"] = sentinel.exists()
        observations["stop_event_set"] = (
            stop_event.is_set() if stop_event is not None else None
        )
        # Let the watcher thread exit so the test cleans up cleanly.
        if stop_event is not None:
            stop_event.set()

    fake_pipeline.run_index = fake_run_index

    args = SimpleNamespace(
        index_cmd="run", config=str(config_path), no_console_log=True,
        dry_run=False, media_source_override=None, export_to_qdrant=False,
        log_level=None, image_only=False, video_only=False,
        reprocess_gps=False, reprocess_objects=False, reprocess_faces=False,
        reprocess_embeddings=False, reprocess_all=False,
    )

    from msa_cli import cmd_index
    cmd_index.handle(args)

    assert observations.get("sentinel_exists") is False, (
        "Stale stop sentinel survived startup — the watcher would have set "
        "stop_event on its first poll and the pipeline would exit before "
        "processing any files. Add a `Path(stop_file).unlink(missing_ok=True)` "
        "between the env-var default and the watcher thread start."
    )
    assert observations.get("stop_event_set") is False, (
        "stop_event was set when run_index was called — the watcher reacted "
        "to the stale sentinel before it was cleared."
    )


def test_run_branch_preserves_api_provided_sentinel(tmp_path, monkeypatch):
    """When the API parent has set MSA_INDEXER_STOP_FILE before spawning
    the indexer subprocess, the child must NOT unlink the sentinel at
    startup — otherwise a stop request issued by the API *during* child
    startup would be erased.

    Walk-through of the race the fix protects against:
      1. API parent: `IndexerManager.start()` clears any stale sentinel,
         then `Popen` the child with MSA_INDEXER_STOP_FILE set.
      2. API parent writes child PID immediately after Popen returns.
      3. Before the child reaches its setup code, the user clicks Stop.
      4. API parent: `IndexerManager.stop()` writes the sentinel.
      5. Child finally gets to its startup code.

    If the child unconditionally unlinks the sentinel at this point, the
    legitimate stop request from step 4 is lost — particularly bad on
    Windows where there's no SIGTERM fallback and the stop hangs until
    `--wait` timeout.

    The fix: clear stale sentinel only when the child defaulted
    MSA_INDEXER_STOP_FILE itself (standalone path). When the API parent
    set it explicitly, trust them — they already cleared stale state.
    """
    log_dir = tmp_path / "logs"
    log_dir.mkdir()
    run_dir = log_dir / "run"
    run_dir.mkdir()
    index_dir = tmp_path / "index"
    index_dir.mkdir()

    # Simulate the parent-set sentinel — a legitimate stop request that
    # arrived during the child's startup window.
    parent_sentinel = run_dir / "indexer.stop"
    parent_sentinel.write_text("legitimate-stop-from-API-during-child-startup")

    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        f"log_dir: {log_dir}\n"
        f"index_dir: {index_dir}\n"
        f"sqlite_path: {index_dir / 'media.sqlite'}\n"
        "media_sources: []\n"
        "log_level: WARNING\n"
    )

    monkeypatch.setenv("MSA_LOG_DIR", str(log_dir))
    monkeypatch.setenv("MSA_CONFIG_PATH", str(config_path))
    # The API parent provided the env var explicitly (mirrors
    # IndexerManager.start setting sub_env["MSA_INDEXER_STOP_FILE"]).
    monkeypatch.setenv("MSA_INDEXER_STOP_FILE", str(parent_sentinel))

    # Stub the pipeline module BEFORE handle() runs — see
    # _stub_pipeline_module() for why monkeypatch.setattr on the dotted
    # path doesn't work in lightweight envs.
    fake_pipeline = _stub_pipeline_module(monkeypatch)

    observations: dict = {}

    def fake_run_index(cfg, stop_event=None):
        # By the time run_index is called, the parent-provided sentinel
        # must still exist so the watcher can observe it.
        observations["sentinel_exists"] = parent_sentinel.exists()
        if stop_event is not None:
            stop_event.set()  # let watcher exit

    fake_pipeline.run_index = fake_run_index

    args = SimpleNamespace(
        index_cmd="run", config=str(config_path), no_console_log=True,
        dry_run=False, media_source_override=None, export_to_qdrant=False,
        log_level=None, image_only=False, video_only=False,
        reprocess_gps=False, reprocess_objects=False, reprocess_faces=False,
        reprocess_embeddings=False, reprocess_all=False,
    )

    from msa_cli import cmd_index
    cmd_index.handle(args)

    assert observations.get("sentinel_exists") is True, (
        "Child erased a parent-provided stop sentinel during startup. "
        "The API parent already cleared stale state before spawning; "
        "anything in the sentinel file when the child starts is a "
        "legitimate stop request (e.g., user clicked Stop during child "
        "init). Clear stale sentinel ONLY in the standalone path where "
        "this process defaulted MSA_INDEXER_STOP_FILE."
    )


@pytest.fixture
def lifecycle_workspace(tmp_path: Path):
    """Isolated workspace + config + msa binary path for slow tests that
    spawn the real indexer subprocess. Skips if real-media fixtures or
    the msa console script aren't available.

    Returns a SimpleNamespace with attributes:
        ws, log_dir, index_dir, src_dir, config_path, msa_bin, env,
        msa_log, run_dir, pid_file, stop_sentinel, image_count
    """
    import shutil as _shutil

    fixtures_root = Path(__file__).parent / "real_media" / "fixtures" / "derived"
    if not fixtures_root.exists():
        pytest.skip(f"Real-media fixtures not present at {fixtures_root}")
    images = sorted(fixtures_root.glob("*.jpg")) + sorted(fixtures_root.glob("*.heic"))
    if len(images) < 2:
        pytest.skip(f"Need at least 2 image fixtures; found {len(images)}")

    msa_bin = Path(sys.executable).parent / (
        "msa.exe" if sys.platform == "win32" else "msa"
    )
    if not msa_bin.exists():
        pytest.skip(f"msa binary not found at {msa_bin} (pip install -e .?)")

    ws = tmp_path / "ws"
    ws.mkdir()
    log_dir    = ws / "logs"   ; log_dir.mkdir()
    index_dir  = ws / "index"  ; index_dir.mkdir()
    data_dir   = ws / "data"   ; data_dir.mkdir()
    src_dir    = ws / "src"    ; src_dir.mkdir()
    qdrant_dir = ws / "qdrant"
    # Use the full fixture set — 4 was too few; the indexer would finish in
    # the same ~25s window it takes to (a) load CLIP and (b) get to the stop
    # call, leaving nothing for stop to actually interrupt.
    for img in images:
        _shutil.copy2(img, src_dir / img.name)

    # Hand-write the YAML rather than import pyyaml so this test runs in
    # lightweight environments. Layout matches msa_settings.load_config().
    #
    # qdrant_path must be set explicitly: the default ("qdrant") is relative
    # to CWD, which under run-local.sh is the workspace path. A Qdrant client
    # already held open by run-local.sh's API server on that path would
    # collide with our test's export — the embedded Qdrant client takes a
    # single-writer lock on its storage folder.
    config_path = ws / "config.yaml"
    config_path.write_text(
        "media_sources:\n"
        f"  - name: test_fixtures\n"
        f"    path: {src_dir}\n"
        "    read_only: true\n"
        "    enabled: true\n"
        f"index_dir: {index_dir}\n"
        f"sqlite_path: {index_dir / 'media.sqlite'}\n"
        f"qdrant_path: {qdrant_dir}\n"
        f"thumb_dir: {data_dir / 'thumbnails'}\n"
        f"face_thumb_dir: {data_dir / 'face_thumbnails'}\n"
        f"log_dir: {log_dir}\n"
        "log_level: INFO\n"
        "model_name: ViT-L-14\n"
        "pretrained: openai\n"
        "device: cpu\n"
        "enable_face_recognition: false\n"
        "enable_object_detection: false\n"
        "enable_video_object_detection: false\n"
        "enable_video_shot_detection: false\n"
    )

    env = os.environ.copy()
    env["MSA_LOG_DIR"] = str(log_dir)
    # MSA_CONFIG_PATH must be set explicitly: qdrant_export.py has a
    # module-level `S = load_config()` (no path arg) that reads
    # MSA_CONFIG_PATH at import time. If the parent shell has it pointing
    # somewhere else (e.g., run-local.sh exports it to the workspace's
    # config.yaml), `S.qdrant_path` resolves to that other config's path
    # — even though our pipeline uses `--config <test_config>` correctly.
    # The mismatch would put us back on the workspace's Qdrant path and
    # collide with run-local.sh's API server's client.
    env["MSA_CONFIG_PATH"] = str(config_path)
    env["MSA_INDEXER_COMMIT_BATCH_FILES"] = "2"
    env["MSA_INDEXER_COMMIT_BATCH_SECONDS"] = "2"

    return SimpleNamespace(
        ws=ws,
        log_dir=log_dir,
        index_dir=index_dir,
        src_dir=src_dir,
        qdrant_dir=qdrant_dir,
        config_path=config_path,
        msa_bin=msa_bin,
        env=env,
        msa_log=log_dir / "msa.log",
        run_dir=log_dir / "run",
        pid_file=log_dir / "run" / "indexer.pid",
        stop_sentinel=log_dir / "run" / "indexer.stop",
        image_count=len(images),
    )


def _wait_for_pid_file(pid_file: Path, proc: subprocess.Popen, timeout: float) -> None:
    """Block until pid_file appears or the subprocess exits or timeout fires."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if pid_file.exists():
            return
        if proc.poll() is not None:
            raise AssertionError(
                f"Indexer exited before writing PID file (rc={proc.returncode})"
            )
        time.sleep(0.25)
    raise AssertionError(f"PID file {pid_file} not written within {timeout}s")


def _wait_for_log_marker(log_path: Path, marker: str, timeout: float,
                         proc: subprocess.Popen | None = None) -> bool:
    """Wait for `marker` to appear in log_path. Returns True if seen, False
    if the subprocess exited first (when proc is provided) or timeout."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if log_path.exists() and marker in log_path.read_text(errors="replace"):
            return True
        if proc is not None and proc.poll() is not None:
            return False
        time.sleep(0.5)
    return False


def _terminate(proc: subprocess.Popen, fout) -> None:
    """Cleanup helper: close stdout capture + terminate/kill if alive."""
    try:
        fout.close()
    except OSError:
        pass
    if proc.poll() is None:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=5)


@pytest.mark.slow
def test_standalone_run_writes_pid_and_responds_to_stop(lifecycle_workspace):
    """End-to-end gate for the BVT Phase A claim.

    BVT validators launch the indexer directly via the CLI (`msa index run &`),
    not through the API. For `msa index stop` to actually drive a cooperative
    exit in that path — rather than silently reporting "no indexer running"
    and letting the backgrounded run finish naturally — the standalone
    `msa index run` handler must:

      1. Write its own PID to <MSA_LOG_DIR>/run/indexer.pid at startup.
      2. Default MSA_INDEXER_STOP_FILE so the daemon watcher thread engages.

    This test spawns the real `msa` binary, asserts the PID file appears,
    runs `msa index stop --require-running` (the BVT-mode flag that fails
    fast if no indexer is found), and asserts the indexer process exits
    rc=0 with no `forrtl: error` in the log. Without the P1 fix, BVT
    Phase A is a silent no-op and the forrtl regression gate is not
    load-bearing.
    """
    ws = lifecycle_workspace
    log_dir = ws.log_dir
    msa_bin = ws.msa_bin
    config_path = ws.config_path
    env = ws.env
    msa_log = ws.msa_log
    pid_file = ws.pid_file
    indexer_capture = ws.ws / "indexer-capture.log"

    fout = open(indexer_capture, "wb")
    proc = subprocess.Popen(
        [str(msa_bin), "index", "run", "--config", str(config_path)],
        env=env, stdout=fout, stderr=subprocess.STDOUT,
    )
    # Reap zombies in the background so _stop_pid_alive correctly reports
    # dead processes — pytest is the indexer's parent here, and on POSIX
    # os.kill(zombie_pid, 0) returns success until wait() collects the child.
    # In real BVT, bash + SIGCHLD reaps backgrounded children similarly.
    threading.Thread(target=proc.wait, daemon=True).start()
    try:
        # P1 assertion: PID file appears at startup (before the slow CLIP load
        # since the write happens immediately after acquire_instance_lock).
        deadline = time.monotonic() + 30.0
        while time.monotonic() < deadline:
            if pid_file.exists():
                break
            if proc.poll() is not None:
                fout.flush()
                raise AssertionError(
                    f"Indexer exited before writing PID file (rc={proc.returncode}). "
                    f"Capture:\n{indexer_capture.read_text(errors='replace')[-2000:]}"
                )
            time.sleep(0.25)
        assert pid_file.exists(), (
            f"Standalone `msa index run` did not write PID file at {pid_file} "
            "within 30s. `msa index stop` cannot find this run — BVT Phase A "
            "would not actually exercise the cooperative-stop path."
        )
        assert int(pid_file.read_text().strip()) == proc.pid

        # Drive the stop immediately. The indexer's daemon watcher engages
        # as soon as the run handler installs it (before CLIP load), so the
        # sentinel write is observed even during model loading; stop_event
        # then takes effect at the pipeline's first per-file checkpoint.
        # Waiting for BATCH_COMMIT here would risk the small fixture set
        # finishing before stop could land.
        #
        # `--require-running` is the BVT-mode flag: if the indexer has
        # already exited (or its PID was never published), stop exits 1
        # instead of returning 0 with "No indexer is currently running".
        # This is the load-bearing assertion that prevents Phase A from
        # passing for the wrong reason.
        stop_result = subprocess.run(
            [str(msa_bin), "index", "stop", "--config", str(config_path),
             "--wait", "180", "--require-running"],
            env=env, capture_output=True, text=True, timeout=240,
        )
        assert stop_result.returncode == 0, (
            f"msa index stop --require-running returned rc={stop_result.returncode}. "
            "If the message says 'No indexer is currently running', the indexer "
            "finished before stop landed — Phase A would also fail in BVT.\n"
            f"stdout: {stop_result.stdout}\nstderr: {stop_result.stderr}"
        )
        assert "Indexer stopped cleanly" in stop_result.stdout, (
            f"Expected 'Indexer stopped cleanly' (stop drove the exit), got:\n"
            f"{stop_result.stdout}"
        )

        rc = proc.wait(timeout=30)
        assert rc == 0, (
            f"Indexer exited rc={rc} after `msa index stop` — expected 0. "
            f"Capture:\n{indexer_capture.read_text(errors='replace')[-2000:]}"
        )

        # Load-bearing on Windows: no Intel Fortran abort in any captured stream.
        for source_name, content in [
            ("msa.log",        msa_log.read_text(errors="replace") if msa_log.exists() else ""),
            ("stdout+stderr",  indexer_capture.read_text(errors="replace")),
        ]:
            assert "forrtl: error" not in content, (
                f"'forrtl: error' in {source_name} — Intel Fortran handler "
                "aborted the process instead of the cooperative-stop path "
                "engaging. Regression of WIN-006."
            )

        # PID file cleaned up by the indexer's finally block.
        assert not pid_file.exists(), (
            "PID file survived clean shutdown — the run-handler's finally "
            "cleanup did not unlink it"
        )
    finally:
        try:
            fout.close()
        except OSError:
            pass
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=5)


@pytest.mark.slow
def test_cooperative_stop_completes_qdrant_export(lifecycle_workspace):
    """When `msa index stop` drives a cooperative exit mid-run, the
    indexer's pipeline finalisation must still run the Qdrant export of
    whatever files were committed before the stop. Otherwise local SQLite
    and Qdrant get out of sync — the API can return search results backed
    by SQLite metadata but missing from Qdrant vector queries.

    The expected log markers (in order):
      - "BATCH_COMMIT" — at least one batch landed before stop
      - "Stop requested — finishing graceful shutdown with Qdrant export"
        — the stop-aware export branch engaged
      - "Qdrant image/video export complete (image=N..." — export wrote
    """
    ws = lifecycle_workspace
    indexer_capture = ws.ws / "indexer-capture.log"

    fout = open(indexer_capture, "wb")
    proc = subprocess.Popen(
        [str(ws.msa_bin), "index", "run", "--config", str(ws.config_path)],
        env=ws.env, stdout=fout, stderr=subprocess.STDOUT,
    )
    threading.Thread(target=proc.wait, daemon=True).start()
    try:
        _wait_for_pid_file(ws.pid_file, proc, timeout=60.0)

        # Wait for BATCH_COMMIT so we know at least one file is durably
        # committed before stop arrives — otherwise Qdrant export has
        # nothing to write and the assertion below is vacuous.
        if not _wait_for_log_marker(ws.msa_log, "BATCH_COMMIT", timeout=180.0, proc=proc):
            log_text = ws.msa_log.read_text(errors="replace") if ws.msa_log.exists() else ""
            raise AssertionError(
                f"No BATCH_COMMIT within 180s — cannot validate Qdrant export "
                f"with empty SQLite state.\nLog tail:\n{log_text[-1500:]}"
            )

        # Drive cooperative stop.
        stop_result = subprocess.run(
            [str(ws.msa_bin), "index", "stop", "--config", str(ws.config_path),
             "--wait", "180", "--require-running"],
            env=ws.env, capture_output=True, text=True, timeout=240,
        )
        assert stop_result.returncode == 0, (
            f"msa index stop --require-running rc={stop_result.returncode}\n"
            f"stdout: {stop_result.stdout}\nstderr: {stop_result.stderr}"
        )

        rc = proc.wait(timeout=60)
        assert rc == 0, f"Indexer rc={rc} after cooperative stop"

        log_text = ws.msa_log.read_text(errors="replace")

        # The stop-aware export branch must have logged its intent.
        assert "Stop requested — finishing graceful shutdown with Qdrant export" in log_text, (
            "Cooperative stop did not engage the Qdrant export branch. The "
            "indexer's pipeline.run_index finalisation must run the export "
            "when stop_event is set AND local_index_changed is True. Check "
            "pipeline.py around the 'should_export_to_qdrant' decision.\n"
            f"Log tail:\n{log_text[-1500:]}"
        )
        # And it must have completed.
        assert "Qdrant image/video export complete" in log_text, (
            "Qdrant export was initiated by the cooperative stop but did "
            "not complete (no 'Qdrant image/video export complete' line). "
            "SQLite and Qdrant are now out of sync.\n"
            f"Log tail:\n{log_text[-1500:]}"
        )
    finally:
        _terminate(proc, fout)


@pytest.mark.slow
def test_noncooperative_kill_recovers_on_restart(lifecycle_workspace):
    """SIGKILL the indexer mid-run, then start it again. The second run
    must detect/clean up stale state (PID file, instance lock, possibly
    a partially-written sentinel, SQLite WAL) and complete successfully.

    What can survive a hard kill (SIGKILL on POSIX, TerminateProcess on
    Windows — both bypass Python's signal handlers, atexit, and finally):
      - indexer.pid pointing at a now-dead PID
      - msa-indexer.lock with a stale lock-owner PID
      - indexer.stop possibly present if it was being written
      - SQLite WAL in some mid-write state (SQLite's own crash recovery
        handles this; we don't have to do anything)

    The second run must:
      1. Not be blocked by the stale instance lock
      2. Not be confused by the stale PID file
      3. Not exit immediately due to a stale sentinel
      4. Process all files and export to Qdrant
    """
    ws = lifecycle_workspace
    capture1 = ws.ws / "indexer-1-capture.log"
    capture2 = ws.ws / "indexer-2-capture.log"

    # ── First run: start, advance past startup, then SIGKILL ──────────────
    fout1 = open(capture1, "wb")
    proc1 = subprocess.Popen(
        [str(ws.msa_bin), "index", "run", "--config", str(ws.config_path)],
        env=ws.env, stdout=fout1, stderr=subprocess.STDOUT,
    )
    threading.Thread(target=proc1.wait, daemon=True).start()
    try:
        _wait_for_pid_file(ws.pid_file, proc1, timeout=60.0)
    finally:
        # SIGKILL on POSIX / TerminateProcess on Windows — bypasses all
        # Python-level cleanup, simulating crash/OOM/Task-Manager-kill.
        proc1.kill()
        try:
            proc1.wait(timeout=10)
        except subprocess.TimeoutExpired:
            pass
        try:
            fout1.close()
        except OSError:
            pass

    # On-disk state at this point is whatever the kernel left behind. We
    # do NOT manually unlink anything — the recovery surface is what the
    # next `msa index run` does on its own.

    # ── Second run: must complete + export ────────────────────────────────
    fout2 = open(capture2, "wb")
    # Pass --export-to-qdrant so we explicitly assert export works post-recovery.
    proc2 = subprocess.Popen(
        [str(ws.msa_bin), "index", "run", "--config", str(ws.config_path),
         "--export-to-qdrant"],
        env=ws.env, stdout=fout2, stderr=subprocess.STDOUT,
    )
    try:
        rc = proc2.wait(timeout=300.0)
        capture2_text = capture2.read_text(errors="replace")
        log_text = ws.msa_log.read_text(errors="replace") if ws.msa_log.exists() else ""

        assert rc == 0, (
            f"Second indexer run failed (rc={rc}) — non-cooperative kill "
            f"left state the next run could not recover from.\n"
            f"Capture (last 2KB):\n{capture2_text[-2000:]}\n"
            f"--- msa.log (last 2KB) ---\n{log_text[-2000:]}"
        )
        assert "Indexing complete" in log_text, (
            f"Second run did not reach 'Indexing complete' — recovery "
            f"failed somewhere in the pipeline.\n{log_text[-2000:]}"
        )
        assert "Qdrant image/video export complete" in log_text, (
            f"Second run completed but Qdrant export did not — recovery "
            f"left the export path broken.\n{log_text[-2000:]}"
        )
        # No Fortran abort across either run.
        capture1_text = capture1.read_text(errors="replace")
        for source_name, content in [
            ("msa.log",           log_text),
            ("run-1 capture",     capture1_text),
            ("run-2 capture",     capture2_text),
        ]:
            assert "forrtl: error" not in content, (
                f"'forrtl: error' in {source_name} — Intel Fortran handler "
                f"aborted. Regression of WIN-006."
            )
    finally:
        _terminate(proc2, fout2)
