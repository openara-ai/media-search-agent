import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch


def test_start_does_not_force_export_to_qdrant(tmp_path, monkeypatch):
    from msa_apps.search_api import indexer_manager as mod

    monkeypatch.setattr(mod.IndexerManager, "_restore_from_pid_file", lambda self: None)

    manager = mod.IndexerManager()

    log_dir = tmp_path / "logs"
    config_path = tmp_path / "config.yaml"
    config_path.write_text("media_sources: []\n")

    fake_thread = MagicMock()
    fake_thread.start = MagicMock()
    fake_proc = MagicMock()
    fake_proc.pid = 12345

    with patch("msa_settings.load_config", return_value=SimpleNamespace(log_dir=log_dir)), \
         patch("msa_query.storage.qdrant_client.close_shared_client"), \
         patch("subprocess.Popen", return_value=fake_proc) as mock_popen, \
         patch("threading.Thread", return_value=fake_thread):
        result = manager.start(str(config_path), str(tmp_path / ".venv" / "bin"))

    cmd = mock_popen.call_args.args[0]

    assert result["status"] == "running"
    assert "--export-to-qdrant" not in cmd
    assert cmd[-1] == "--no-console-log"
    script_name = "msa.exe" if sys.platform == "win32" else "msa"
    assert cmd[:4] == [
        str(Path(tmp_path / ".venv" / "bin") / script_name),
        "index",
        "run",
        "--config",
    ]


def _isolate_run_dir(monkeypatch, tmp_path):
    """Point the module-level _RUN_DIR / pid / sentinel paths at tmp_path.

    Uses raising=False on _INDEXER_STOP_FILE so this fixture also works
    against the pre-fix module (where the attribute doesn't exist yet) —
    that way pre-fix runs fail loudly on the *behavioural* assertion, not
    on test-setup AttributeError.
    """
    from msa_apps.search_api import indexer_manager as mod

    run_dir = tmp_path / "run"
    run_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(mod, "_RUN_DIR", run_dir)
    monkeypatch.setattr(mod, "_INDEXER_PID_FILE", run_dir / "indexer.pid")
    monkeypatch.setattr(
        mod, "_INDEXER_STOP_FILE", run_dir / "indexer.stop", raising=False
    )
    # _monitor reopens the shared Qdrant client after the subprocess exits; we
    # don't want that side-effect (it would try to open a real DB on disk).
    monkeypatch.setattr(
        "msa_query.storage.qdrant_client.reopen_shared_client", lambda: None
    )
    return mod, run_dir


def test_start_clears_stale_stop_sentinel(tmp_path, monkeypatch):
    """A sentinel left over from a previous run (API killed mid-stop, hard
    reboot, etc.) must be cleared before the next indexer launches.

    This is the keystone protection against stale-sentinel rot: even if every
    other cleanup site silently failed, the very next start() makes the new
    run see a clean slate. Without this, a stale sentinel would cause the
    next indexer to abort itself on first poll of the watcher thread —
    "stop requested" before it had even processed a file.
    """
    mod, run_dir = _isolate_run_dir(monkeypatch, tmp_path)
    monkeypatch.setattr(mod.IndexerManager, "_restore_from_pid_file", lambda self: None)

    # Simulate a leftover sentinel from a previous (uncleanly stopped) run.
    stale_sentinel = run_dir / "indexer.stop"
    stale_sentinel.write_text("stale-run-id")
    assert stale_sentinel.exists()

    mgr = mod.IndexerManager()

    log_dir = tmp_path / "logs"
    config_path = tmp_path / "config.yaml"
    config_path.write_text("media_sources: []\n")

    fake_proc = MagicMock()
    fake_proc.pid = 12345
    with patch("msa_settings.load_config", return_value=SimpleNamespace(log_dir=log_dir)), \
         patch("msa_query.storage.qdrant_client.close_shared_client"), \
         patch("subprocess.Popen", return_value=fake_proc), \
         patch("threading.Thread", return_value=MagicMock()):
        mgr.start(str(config_path), str(tmp_path / ".venv" / "bin"))

    assert not stale_sentinel.exists(), (
        "start() must remove any leftover stop sentinel from a previous run. "
        "If a stale sentinel survives, the indexer subprocess's watcher will "
        "see it on first poll and exit immediately."
    )
    # Belt-and-suspenders: the in-memory flag is also reset.
    assert mgr._stop_requested is False


def test_user_stop_classifies_non_zero_exit_as_stopped(tmp_path, monkeypatch):
    """When the user has pressed Stop, IndexerManager must classify the
    subprocess exit as 'stopped' — not 'error' — even if the subprocess
    exits with a non-zero return code.

    Repro of the Windows symptom: pressing Stop triggers CTRL_BREAK, the
    Intel Fortran runtime (linked via NumPy/SciPy/sklearn) aborts the
    process with 'forrtl: error (200)' and rc=1 before Python can shut
    down cleanly. Before the fix, the API surfaced this as status='error'
    with a red dot in the UI — visually indistinguishable from a real
    crash, even though the user had just asked to stop.
    """
    mod, _run_dir = _isolate_run_dir(monkeypatch, tmp_path)
    monkeypatch.setattr(mod.IndexerManager, "_restore_from_pid_file", lambda self: None)

    mgr = mod.IndexerManager()

    # A subprocess that exits with rc=1 shortly after launch — simulates the
    # post-stop crash path (Fortran abort, library SIGABRT, etc.) where the
    # cooperative shutdown was preempted.
    proc = subprocess.Popen([sys.executable, "-c", "raise SystemExit(1)"])
    mgr._process = proc
    mgr._status = "running"
    mgr._run_id = "test-run"
    mgr._log_path = tmp_path / "msa.log"
    # This is the flag stop() sets. Mark it directly so the test exercises
    # the classifier without depending on signal/sentinel delivery (which is
    # platform-specific and tested separately by manual QA).
    mgr._stop_requested = True

    mgr._monitor()  # blocks until proc exits, then classifies

    status = mgr.get_status()
    assert status["return_code"] == 1, (
        "Test setup expects rc=1 (non-zero exit). If this asserts, the "
        "subprocess didn't exit as planned and the rest of the test is moot."
    )
    assert status["status"] == "stopped", (
        f"User-initiated stop was classified as {status['status']!r}. "
        "When stop() was called, the manager must record 'stopped' even on "
        "non-zero subprocess exit — otherwise the UI shows 'Error' (red dot) "
        "for what the user just asked to do. This is the Windows-visible bug."
    )


def test_clean_exit_without_stop_request_is_complete(tmp_path, monkeypatch):
    """Regression guard: a successful run (rc=0, no stop requested) must
    still classify as 'complete' after the stop-classification change.
    """
    mod, _ = _isolate_run_dir(monkeypatch, tmp_path)
    monkeypatch.setattr(mod.IndexerManager, "_restore_from_pid_file", lambda self: None)
    # _monitor calls reset_query_engine on rc=0 — stub it out.
    monkeypatch.setattr(
        "msa_apps.search_api.deps.reset_query_engine", lambda: None
    )

    mgr = mod.IndexerManager()
    proc = subprocess.Popen([sys.executable, "-c", "pass"])
    mgr._process = proc
    mgr._status = "running"
    mgr._run_id = "test-run"
    mgr._log_path = tmp_path / "msa.log"
    # No stop requested.

    mgr._monitor()

    status = mgr.get_status()
    assert status["return_code"] == 0
    assert status["status"] == "complete"


def test_crash_without_stop_request_is_error(tmp_path, monkeypatch):
    """Regression guard: a genuine crash (rc!=0, no stop) must still be
    classified as 'error' — the fix only changes behaviour when stop was
    explicitly requested.
    """
    mod, _ = _isolate_run_dir(monkeypatch, tmp_path)
    monkeypatch.setattr(mod.IndexerManager, "_restore_from_pid_file", lambda self: None)

    mgr = mod.IndexerManager()
    proc = subprocess.Popen([sys.executable, "-c", "raise SystemExit(1)"])
    mgr._process = proc
    mgr._status = "running"
    mgr._run_id = "test-run"
    mgr._log_path = tmp_path / "msa.log"

    mgr._monitor()

    status = mgr.get_status()
    assert status["return_code"] == 1
    assert status["status"] == "error"


def test_get_status_respects_stop_when_monitor_hasnt_run_yet(tmp_path, monkeypatch):
    """get_status() has a fallback that runs when the subprocess has exited
    but the monitor hasn't observed it yet (notably after API restart with
    a stale PID file, or in the brief window before _monitor / _monitor_pid
    sees the exit). That fallback used to hard-code 'complete', which would
    leak a "Complete" status to the UI for what was actually a user stop.

    Asserts the fallback honours the same _stop_requested contract that
    _monitor does.
    """
    mod, run_dir = _isolate_run_dir(monkeypatch, tmp_path)
    monkeypatch.setattr(mod.IndexerManager, "_restore_from_pid_file", lambda self: None)

    mgr = mod.IndexerManager()
    # Simulate a running indexer where stop was requested, then the subprocess
    # exited but neither _monitor nor _monitor_pid has run yet.
    mgr._status = "running"
    mgr._run_id = "test-run"
    mgr._stop_requested = True
    # No live process — _pid_alive will return False and trigger the fallback.
    mgr._process = None
    # Write a PID file pointing at a definitely-dead PID (a tiny subprocess
    # that exits immediately).
    proc = subprocess.Popen([sys.executable, "-c", "pass"])
    proc.wait(timeout=5)
    mod._write_pid(proc.pid)

    status = mgr.get_status()
    assert status["status"] == "stopped", (
        f"get_status() fallback returned {status['status']!r}. When the user "
        "has requested stop, the fallback must classify as 'stopped' — not "
        "'complete' — or the UI briefly shows the wrong terminal state."
    )


def test_stop_writes_sentinel_and_records_intent(tmp_path, monkeypatch):
    """stop() must (a) flip _stop_requested so subsequent classification is
    correct and (b) write the cooperative-stop sentinel file the indexer
    subprocess watches for. The sentinel is the Windows-safe stop path that
    avoids triggering Intel Fortran's CTRL_BREAK handler.
    """
    mod, run_dir = _isolate_run_dir(monkeypatch, tmp_path)
    monkeypatch.setattr(mod.IndexerManager, "_restore_from_pid_file", lambda self: None)

    mgr = mod.IndexerManager()
    # Long-running subprocess we can stop without it exiting first.
    # Isolate it in its own session/process group so the SIGTERM fan-out
    # from stop() (os.killpg) doesn't take down the pytest runner.
    popen_kwargs = {}
    if sys.platform == "win32":
        popen_kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        popen_kwargs["start_new_session"] = True
    proc = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(30)"],
        **popen_kwargs,
    )
    try:
        mgr._process = proc
        mgr._status = "running"
        mgr._run_id = "test-run"
        mgr._log_path = tmp_path / "msa.log"

        result = mgr.stop()

        assert result["status"] == "stopping"
        assert mgr._stop_requested is True
        assert (run_dir / "indexer.stop").exists(), (
            "stop() must write the sentinel file so the indexer subprocess "
            "can detect the request without relying on signals (which fail "
            "on Windows due to Fortran runtime hijacking CTRL_BREAK)."
        )
    finally:
        # stop() may have already terminated the child on POSIX via the
        # process-group SIGTERM. Guard the teardown so a race-induced
        # ProcessLookupError doesn't make this test flaky.
        if proc.poll() is None:
            try:
                proc.kill()
            except (ProcessLookupError, OSError):
                pass
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            pass


def test_stop_returns_error_when_sentinel_write_fails(tmp_path, monkeypatch):
    """If the sentinel cannot be written, stop() must surface an error and NOT
    flip _stop_requested. On Windows the sentinel is the only stop path — a
    silent failure would leave the UI thinking "stopping" while the indexer
    keeps running. Caller (the HTTP layer) maps this to a 500 so the UI's
    stop mutation enters its error state.
    """
    mod, run_dir = _isolate_run_dir(monkeypatch, tmp_path)
    monkeypatch.setattr(mod.IndexerManager, "_restore_from_pid_file", lambda self: None)
    # Force the sentinel write to fail.
    monkeypatch.setattr(mod, "_write_stop_sentinel", lambda run_id: False)

    mgr = mod.IndexerManager()

    popen_kwargs = {}
    if sys.platform == "win32":
        popen_kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        popen_kwargs["start_new_session"] = True
    proc = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(30)"],
        **popen_kwargs,
    )
    try:
        mgr._process = proc
        mgr._status = "running"
        mgr._run_id = "test-run"
        mgr._log_path = tmp_path / "msa.log"

        result = mgr.stop()

        assert result["status"] == "error", (
            f"stop() returned {result['status']!r} when sentinel write failed; "
            "expected 'error' so the HTTP layer can return 500."
        )
        assert "detail" in result, "Error response must carry a user-facing detail"
        assert mgr._stop_requested is False, (
            "When the sentinel write fails, _stop_requested must remain False — "
            "otherwise _monitor would misclassify the eventual exit as 'stopped' "
            "for a run that was never actually asked to stop."
        )
    finally:
        if proc.poll() is None:
            try:
                proc.kill()
            except (ProcessLookupError, OSError):
                pass
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            pass


def test_stop_doesnt_override_classification_of_already_dead_process(tmp_path, monkeypatch):
    """If the subprocess has already exited (e.g., crashed with rc!=0) but
    _monitor hasn't observed it yet, a late stop() call must NOT flip
    _stop_requested. Otherwise the real crash gets reported as a user stop,
    hiding actionable failure signals. Defer to _monitor's rc-based path.
    """
    mod, run_dir = _isolate_run_dir(monkeypatch, tmp_path)
    monkeypatch.setattr(mod.IndexerManager, "_restore_from_pid_file", lambda self: None)

    mgr = mod.IndexerManager()

    # A subprocess that exits immediately with rc=1 — simulates the crash
    # that lands in the race window before _monitor sees it.
    proc = subprocess.Popen([sys.executable, "-c", "raise SystemExit(1)"])
    proc.wait(timeout=5)
    assert proc.returncode == 1

    mgr._process = proc
    mgr._status = "running"  # _monitor hasn't reclassified yet
    mgr._run_id = "test-run"
    mgr._log_path = tmp_path / "msa.log"

    result = mgr.stop()

    assert mgr._stop_requested is False, (
        "stop() flipped _stop_requested for an already-dead subprocess. "
        "This would mask the crash as a user stop in _monitor's "
        "classification."
    )
    # The response should NOT claim "stopping"; it must reflect the actual
    # state so the UI doesn't show a stale spinner.
    assert result["status"] != "stopping"
    # And the sentinel must not have been written (we didn't actually request a stop).
    assert not (run_dir / "indexer.stop").exists(), (
        "stop() wrote the sentinel for an already-dead process. The next run "
        "would see a stale sentinel and abort immediately (mitigated by "
        "start()'s defensive sweep, but still misleading)."
    )
