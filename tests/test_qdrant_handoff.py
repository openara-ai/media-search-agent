"""M-8/S-2 — Qdrant lock window: sentinel-file handshake tests.

Covers the contract in internal/docs/storage/M8_QDRANT_LOCK_WINDOW_PLAN.md:
- grep-gate: every shared-client operation goes through shared_client_op()
  (a bare get_shared_client() fetch outside the accessor module fails here)
- protocol module (qdrant_handoff): request/grant files, run_id echo,
  stale-grant rejection, kill switch
- pipeline-side handshake: request before FIRST Qdrant open, grant/timeout
  paths, lock-retry ladder, loud export_blocked failure, terminal complete
  re-emit
- API-side watcher: drain-before-grant, request-gone reopen, dead-pid
  fail-safe, G9 re-attach parity
- §4 write rejection during the handoff window
"""
import json
import os
import re
import subprocess
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from msa_indexer.db import qdrant_handoff as ho

SRC_ROOT = Path(__file__).resolve().parents[1] / "src"
ACCESSOR_MODULE = "msa_query/storage/qdrant_client.py"
LIFECYCLE_MODULE = "msa_indexer/db/qdrant_handoff.py"


def _dead_pid() -> int:
    """A real, provably-dead pid (spawn a no-op child and reap it)."""
    proc = subprocess.Popen([sys.executable, "-c", "pass"])
    proc.wait(timeout=5)
    return proc.pid


def _write_foreign_request(base: Path, run_id: str, pid: int) -> None:
    """A request recorded for another process (bypasses write_request's own-pid)."""
    ho._write_json_atomic(
        ho.request_path(base), {"run_id": run_id, "pid": pid, "ts": time.time()}
    )


@pytest.fixture(autouse=True)
def _handoff_env(monkeypatch):
    """Ensure the ambient environment never leaks into handshake tests."""
    monkeypatch.delenv("MSA_QDRANT_HANDOFF", raising=False)
    monkeypatch.delenv("MSA_QDRANT_HANDOFF_DIR", raising=False)
    monkeypatch.delenv("MSA_LOG_DIR", raising=False)


# ── Grep-gate (§3.2 / §6.1) ────────────────────────────────────────────────────

def test_grep_gate_no_get_shared_client_outside_accessor_module():
    """No code under src/ may fetch get_shared_client() outside the accessor
    module itself. All shared-client operations must go through the
    shared_client_op() context manager so the in-flight refcount protects
    them from a concurrent close during the handoff drain. A new unguarded
    site is exactly the future in-process-deadlock bug §3.3 guards against —
    convert it to `with shared_client_op() as client:` instead.
    """
    offenders: list[str] = []
    for py in sorted(SRC_ROOT.rglob("*.py")):
        rel = py.relative_to(SRC_ROOT).as_posix()
        if rel == ACCESSOR_MODULE:
            continue
        try:
            text = py.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for lineno, line in enumerate(text.splitlines(), start=1):
            if "get_shared_client" in line:
                offenders.append(f"{rel}:{lineno}: {line.strip()}")
    assert offenders == [], (
        "get_shared_client referenced outside the accessor module — use "
        "shared_client_op() (see module header of qdrant_client.py):\n"
        + "\n".join(offenders)
    )


_RAW_CLEAR_RE = re.compile(r"\b_?clear_(request|grant|all)\b")
_SENTINEL_TOKEN_RE = re.compile(
    r"request_path|granted_path|qdrant\.request|qdrant\.granted|"
    r"REQUEST_FILE_NAME|GRANTED_FILE_NAME"
)


def test_grep_gate_handshake_file_lifecycle_is_owner_aware():
    """Round-3 (PR #202): removing qdrant.request/qdrant.granted is a
    privileged action. No code under src/ outside the lifecycle module may
    call the raw clear primitives or unlink the sentinel files directly —
    every cleanup must route through the owner/liveness-aware
    cleanup_stale(), which never clobbers a LIVE foreign window. A new
    cleanup site that bypasses the helper is exactly the defect class the
    round-3 findings exposed (a second exporter clobbering a granted
    window; API startup deleting a live standalone-export handshake).
    """
    offenders: list[str] = []
    for py in sorted(SRC_ROOT.rglob("*.py")):
        rel = py.relative_to(SRC_ROOT).as_posix()
        if rel == LIFECYCLE_MODULE:
            continue
        try:
            text = py.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for lineno, line in enumerate(text.splitlines(), start=1):
            if _RAW_CLEAR_RE.search(line):
                offenders.append(f"{rel}:{lineno}: {line.strip()}")
            elif "unlink" in line and _SENTINEL_TOKEN_RE.search(line):
                offenders.append(f"{rel}:{lineno}: {line.strip()}")
    assert offenders == [], (
        "handshake sentinel files removed outside qdrant_handoff.py — route "
        "the cleanup through cleanup_stale() (owner/liveness-aware; see the "
        "module header of qdrant_handoff.py):\n" + "\n".join(offenders)
    )


def test_gate_first_window_open_routes_through_lock_retry_ladder():
    """Round-6 (PR #202): the lock-retry ladder is the SINGLE post-timeout
    open policy. In both windowed modes the FIRST embedded-Qdrant open inside
    handoff_window() must route through call_with_lock_retry — a bare first
    open aborts on the API-held lock precisely when the 15 s grant wait timed
    out legitimately (the watcher still inside its 120 s write-drain
    ceiling). run_index's ladder wraps its version read; run_export's wraps
    _do_qdrant_export (export_images_to_qdrant constructs its client before
    any empty-table bailout, so it is the first open in every export-only
    case). Later opens in the same window are post-first-open: the lock is
    proven free and the on-disk request keeps the API from reopening."""
    import inspect
    from msa_indexer import pipeline

    run_index_src = inspect.getsource(pipeline.run_index)
    assert "call_with_lock_retry(get_qdrant_export_version)" in run_index_src, (
        "run_index's version read (its first Qdrant open) left the ladder"
    )
    run_export_src = inspect.getsource(pipeline.run_export)
    assert re.search(
        r"call_with_lock_retry\(\s*lambda: _do_qdrant_export", run_export_src
    ), (
        "run_export's _do_qdrant_export (its first Qdrant open) must route "
        "through call_with_lock_retry"
    )
    assert not re.search(r"=\s*_do_qdrant_export\(", run_export_src), (
        "bare _do_qdrant_export call in run_export bypasses the ladder"
    )


# ── Protocol module (qdrant_handoff) ───────────────────────────────────────────


class TestCleanupStale:
    """cleanup_stale() is THE single lifecycle authority (round-3): it clears
    the handshake files only when the recorded owner is provably not using
    the window — dead pid, our own pid, or no request at all."""

    def test_preserves_live_foreign_request(self, tmp_path):
        proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
        try:
            _write_foreign_request(tmp_path, "theirs", proc.pid)
            ho.write_grant("theirs", tmp_path)
            assert ho.cleanup_stale(tmp_path) is False, (
                "a LIVE foreign window must never be cleared"
            )
            assert ho.read_request(tmp_path)["run_id"] == "theirs"
            assert ho.read_grant(tmp_path)["run_id"] == "theirs"
        finally:
            proc.kill()
            proc.wait(timeout=5)

    def test_clears_dead_owner(self, tmp_path):
        _write_foreign_request(tmp_path, "ghost", _dead_pid())
        ho.write_grant("ghost", tmp_path)
        assert ho.cleanup_stale(tmp_path) is True
        assert not ho.request_path(tmp_path).exists()
        assert not ho.granted_path(tmp_path).exists()

    def test_clears_own_window(self, tmp_path):
        ho.write_request("mine", tmp_path)  # records os.getpid()
        ho.write_grant("mine", tmp_path)
        assert ho.cleanup_stale(tmp_path) is True
        assert not ho.request_path(tmp_path).exists()
        assert not ho.granted_path(tmp_path).exists()

    def test_clears_orphaned_grant_without_request(self, tmp_path):
        ho.write_grant("orphan", tmp_path)
        assert ho.cleanup_stale(tmp_path) is True
        assert not ho.granted_path(tmp_path).exists()

    def test_unverifiable_pid_is_preserved(self, tmp_path):
        """A request whose pid cannot be read is treated as LIVE — the
        fail-safe direction is to never clobber what we cannot prove stale."""
        ho._write_json_atomic(
            ho.request_path(tmp_path),
            {"run_id": "weird", "pid": "not-a-pid", "ts": time.time()},
        )
        assert ho.cleanup_stale(tmp_path) is False
        assert ho.read_request(tmp_path) is not None

    def test_liveness_override_is_honored(self, tmp_path):
        """Callers (the API watcher) pass their own pid_alive so tests and
        platform-specific checks stay consistent with the caller's view."""
        _write_foreign_request(tmp_path, "x", 99999999)
        assert ho.cleanup_stale(tmp_path, pid_alive_fn=lambda pid: True) is False
        assert ho.cleanup_stale(tmp_path, pid_alive_fn=lambda pid: False) is True
        assert not ho.request_path(tmp_path).exists()


class TestWindowActive:
    """window_active() — the READ-ONLY reopen-gating complement of
    cleanup_stale (round-6): True only while a live foreign window exists,
    and it never touches the files."""

    def test_absent_request_is_inactive(self, tmp_path):
        assert ho.window_active(tmp_path) is False
        ho.write_grant("orphan", tmp_path)  # orphaned grant authorizes nothing
        assert ho.window_active(tmp_path) is False
        assert ho.read_grant(tmp_path) is not None, "read-only: grant untouched"

    def test_live_foreign_request_is_active_and_untouched(self, tmp_path):
        proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
        try:
            _write_foreign_request(tmp_path, "theirs", proc.pid)
            assert ho.window_active(tmp_path) is True
            assert ho.read_request(tmp_path)["run_id"] == "theirs", (
                "read-only: the live request must be preserved"
            )
        finally:
            proc.kill()
            proc.wait(timeout=5)

    def test_dead_owner_request_is_inactive_but_preserved(self, tmp_path):
        _write_foreign_request(tmp_path, "ghost", _dead_pid())
        assert ho.window_active(tmp_path) is False
        assert ho.read_request(tmp_path) is not None, (
            "window_active must not clean up — that is cleanup_stale's job"
        )

    def test_own_pid_request_is_inactive(self, tmp_path):
        ho.write_request("mine", tmp_path)  # records os.getpid()
        assert ho.window_active(tmp_path) is False, (
            "our own leftovers are not a foreign window to defer to"
        )

    def test_unverifiable_pid_is_active(self, tmp_path):
        ho._write_json_atomic(
            ho.request_path(tmp_path),
            {"run_id": "weird", "pid": "not-a-pid", "ts": time.time()},
        )
        assert ho.window_active(tmp_path) is True, (
            "fail-safe: never race a window we cannot disprove"
        )
        _write_foreign_request(tmp_path, "x", 99999999)

        def _raises(_pid):
            raise OSError("cannot check")

        assert ho.window_active(tmp_path, pid_alive_fn=_raises) is True

    def test_unparseable_request_honors_claim_grace(self, tmp_path):
        path = ho.request_path(tmp_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("not json", encoding="utf-8")
        assert ho.window_active(tmp_path) is True, (
            "a fresh unparseable file is an in-progress claim"
        )
        old = time.time() - (ho.CLAIM_WRITE_GRACE_SECONDS + 5)
        os.utime(path, (old, old))
        assert ho.window_active(tmp_path) is False, (
            "past the grace it is a crashed-mid-claim leftover"
        )

    def test_liveness_override_is_honored(self, tmp_path):
        _write_foreign_request(tmp_path, "x", 99999999)
        assert ho.window_active(tmp_path, pid_alive_fn=lambda pid: True) is True
        assert ho.window_active(tmp_path, pid_alive_fn=lambda pid: False) is False


class TestHandoffProtocol:
    def test_kill_switch_predicate(self, monkeypatch):
        assert ho.handoff_enabled() is True
        monkeypatch.setenv("MSA_QDRANT_HANDOFF", "off")
        assert ho.handoff_enabled() is False
        monkeypatch.setenv("MSA_QDRANT_HANDOFF", "OFF ")
        assert ho.handoff_enabled() is False
        monkeypatch.setenv("MSA_QDRANT_HANDOFF", "on")
        assert ho.handoff_enabled() is True

    def test_handoff_dir_derivation(self, monkeypatch, tmp_path):
        import msa_settings

        monkeypatch.chdir(tmp_path)
        # Config-anchored (round-5): with no env set, the ACTIVE config's
        # log_dir/run — cwd-independent, unlike the old <cwd>/run rule.
        cfg_log = tmp_path / "cfg-logs"
        monkeypatch.setattr(
            msa_settings, "load_config",
            lambda *a, **k: SimpleNamespace(log_dir=cfg_log),
        )
        assert ho.handoff_dir() == cfg_log / "run"
        # A caller-supplied log_dir (the pipeline passes its --config's
        # cfg.log_dir) is used directly — no config lookup.
        assert (
            ho.handoff_dir(log_dir=tmp_path / "explicit")
            == tmp_path / "explicit" / "run"
        )
        # cwd fallback ONLY when no config can be loaded at all.
        def _no_config(*_a, **_k):
            raise FileNotFoundError("config.yaml not found")
        monkeypatch.setattr(msa_settings, "load_config", _no_config)
        assert ho.handoff_dir() == tmp_path / "run"
        # MSA_LOG_DIR/run — same rule as the pid/stop sentinel dir
        monkeypatch.setenv("MSA_LOG_DIR", str(tmp_path / "logs"))
        assert ho.handoff_dir() == tmp_path / "logs" / "run"
        # explicit override wins (the API passes its run dir to the subprocess)
        monkeypatch.setenv("MSA_QDRANT_HANDOFF_DIR", str(tmp_path / "apirun"))
        assert ho.handoff_dir() == tmp_path / "apirun"

    def test_handoff_dir_same_config_different_cwd_agree(self, monkeypatch, tmp_path):
        """Round-5 regression (PR #202): the API-side watcher and a standalone
        exporter launched from DIFFERENT working directories with no
        MSA_LOG_DIR/MSA_QDRANT_HANDOFF_DIR must resolve the SAME handoff dir
        from the same config. The old <cwd>/run fallback put the request in
        the exporter's cwd while the watcher polled its own — the manual-
        repair `msa index export` then timed out into lock contention."""
        import msa_settings
        from msa_apps.search_api import indexer_manager as im

        cfg_log = tmp_path / "logs"
        monkeypatch.setattr(
            msa_settings, "load_config",
            lambda *a, **k: SimpleNamespace(log_dir=cfg_log),
        )
        api_cwd = tmp_path / "api-cwd"
        export_cwd = tmp_path / "export-cwd"
        api_cwd.mkdir()
        export_cwd.mkdir()

        monkeypatch.chdir(api_cwd)
        api_dir = im._handoff_run_dir()  # what the watcher snapshots
        monkeypatch.chdir(export_cwd)
        exporter_dir = ho.handoff_dir(log_dir=cfg_log)  # what the pipeline passes
        assert api_dir == exporter_dir == cfg_log / "run"

    def test_request_grant_roundtrip_and_clear(self, tmp_path):
        ho.write_request("run-a", tmp_path)
        req = ho.read_request(tmp_path)
        assert req is not None
        assert req["run_id"] == "run-a"
        assert req["pid"] == os.getpid()
        assert "ts" in req

        ho.write_grant("run-a", tmp_path)
        grant = ho.read_grant(tmp_path)
        assert grant is not None and grant["run_id"] == "run-a"

        # Our own window (request pid == this process) → cleanup clears it.
        assert ho.cleanup_stale(tmp_path) is True
        assert ho.read_request(tmp_path) is None
        assert ho.read_grant(tmp_path) is None
        assert not ho.request_path(tmp_path).exists()
        assert not ho.granted_path(tmp_path).exists()

    def test_corrupt_files_read_as_none(self, tmp_path):
        ho.request_path(tmp_path).parent.mkdir(parents=True, exist_ok=True)
        ho.request_path(tmp_path).write_text("{not json", encoding="utf-8")
        ho.granted_path(tmp_path).write_text("[]", encoding="utf-8")  # not a dict
        assert ho.read_request(tmp_path) is None
        assert ho.read_grant(tmp_path) is None

    def test_wait_for_grant_accepts_matching_run_id(self, tmp_path):
        ho.write_grant("mine", tmp_path)
        assert ho.wait_for_grant("mine", tmp_path, timeout=0.5, poll=0.02) is True

    def test_wait_for_grant_removes_mismatched_grant_and_times_out(self, tmp_path):
        """A stale grant surviving a crashed prior run must never authorize a
        new request — it is deleted, and the wait times out (§3.1)."""
        ho.write_grant("someone-else", tmp_path)
        assert ho.wait_for_grant("mine", tmp_path, timeout=0.3, poll=0.02) is False
        assert not ho.granted_path(tmp_path).exists(), "stale grant must be removed"

    def test_handoff_window_writes_request_and_cleans_up(self, monkeypatch, tmp_path):
        monkeypatch.setenv("MSA_QDRANT_HANDOFF_DIR", str(tmp_path))
        monkeypatch.setattr(ho, "GRANT_WAIT_SECONDS", 0.2)
        seen = {}
        with ho.handoff_window():
            seen["request"] = ho.read_request(tmp_path)
        assert seen["request"] is not None and seen["request"]["pid"] == os.getpid()
        assert not ho.request_path(tmp_path).exists()
        assert not ho.granted_path(tmp_path).exists()

    def test_handoff_window_clears_stale_files_before_new_request(self, monkeypatch, tmp_path):
        monkeypatch.setenv("MSA_QDRANT_HANDOFF_DIR", str(tmp_path))
        monkeypatch.setattr(ho, "GRANT_WAIT_SECONDS", 0.2)
        ho.write_request("stale-run", tmp_path)
        ho.write_grant("stale-run", tmp_path)
        with ho.handoff_window():
            req = ho.read_request(tmp_path)
            assert req is not None and req["run_id"] != "stale-run"
            # the stale grant was cleared up-front and never re-appeared
            grant = ho.read_grant(tmp_path)
            assert grant is None or grant["run_id"] == req["run_id"]

    def test_handoff_window_never_clobbers_live_foreign_window(self, monkeypatch, tmp_path):
        """Round-3 (PR #202): a second exporter finding a LIVE foreign
        request must not clear it — the API watcher would read the missing
        request as a release and reopen its shared client while the first
        exporter still holds embedded Qdrant. After the bounded slot wait
        the failure is LOUD (HandoffSlotBusyError), and the first window is
        untouched."""
        monkeypatch.setenv("MSA_QDRANT_HANDOFF_DIR", str(tmp_path))
        monkeypatch.setattr(ho, "SLOT_WAIT_TOTAL_SECONDS", 0.3)
        proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
        try:
            _write_foreign_request(tmp_path, "first-window", proc.pid)
            ho.write_grant("first-window", tmp_path)
            with pytest.raises(ho.HandoffSlotBusyError):
                with ho.handoff_window():
                    pytest.fail("window must not open over a live foreign request")
            req = ho.read_request(tmp_path)
            assert req is not None and req["run_id"] == "first-window", (
                "the live foreign request was clobbered"
            )
            assert ho.read_grant(tmp_path)["run_id"] == "first-window", (
                "the live foreign grant was clobbered"
            )
        finally:
            proc.kill()
            proc.wait(timeout=5)

    def test_handoff_window_serializes_behind_foreign_window(self, monkeypatch, tmp_path):
        """When the live foreign window releases within the slot budget, the
        second requester proceeds with its OWN request — serialized, never
        interleaved."""
        monkeypatch.setenv("MSA_QDRANT_HANDOFF_DIR", str(tmp_path))
        monkeypatch.setattr(ho, "SLOT_WAIT_TOTAL_SECONDS", 5.0)
        monkeypatch.setattr(ho, "GRANT_WAIT_SECONDS", 0.2)
        proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
        releaser = None
        try:
            _write_foreign_request(tmp_path, "first-window", proc.pid)

            def release_after_delay():
                time.sleep(0.3)
                ho._clear_all(tmp_path)  # the foreign owner's own finally-cleanup

            releaser = threading.Thread(target=release_after_delay, daemon=True)
            releaser.start()
            with ho.handoff_window():
                req = ho.read_request(tmp_path)
                assert req is not None and req["run_id"] != "first-window"
                assert req["pid"] == os.getpid()
            assert not ho.request_path(tmp_path).exists()
        finally:
            if releaser is not None:
                releaser.join(timeout=5)
            proc.kill()
            proc.wait(timeout=5)

    def test_handoff_window_reaps_dead_foreign_window_immediately(self, monkeypatch, tmp_path):
        """Crash-cleanup preserved: a DEAD foreign owner's leftovers are
        reaped by the slot wait's first pass — no bounded wait, no failure."""
        monkeypatch.setenv("MSA_QDRANT_HANDOFF_DIR", str(tmp_path))
        monkeypatch.setattr(ho, "GRANT_WAIT_SECONDS", 0.2)
        _write_foreign_request(tmp_path, "crashed", _dead_pid())
        ho.write_grant("crashed", tmp_path)
        start = time.monotonic()
        with ho.handoff_window():
            req = ho.read_request(tmp_path)
            assert req is not None and req["run_id"] != "crashed"
        assert time.monotonic() - start < ho.SLOT_WAIT_TOTAL_SECONDS / 2, (
            "dead-owner leftovers must be reaped immediately, not waited on"
        )

    def test_handoff_window_cleans_up_when_body_raises(self, monkeypatch, tmp_path):
        """finally-cleanup is the first of the three independent unblocks (L5):
        a crashed export must remove both files so the watcher reopens."""
        monkeypatch.setenv("MSA_QDRANT_HANDOFF_DIR", str(tmp_path))
        monkeypatch.setattr(ho, "GRANT_WAIT_SECONDS", 0.2)
        with pytest.raises(RuntimeError):
            with ho.handoff_window():
                assert ho.request_path(tmp_path).exists()
                raise RuntimeError("exporter blew up")
        assert not ho.request_path(tmp_path).exists()
        assert not ho.granted_path(tmp_path).exists()

    def test_handoff_window_noop_when_kill_switch_off(self, monkeypatch, tmp_path):
        monkeypatch.setenv("MSA_QDRANT_HANDOFF", "off")
        monkeypatch.setenv("MSA_QDRANT_HANDOFF_DIR", str(tmp_path))
        with ho.handoff_window():
            assert not ho.request_path(tmp_path).exists(), (
                "kill switch off: no handshake files may be written"
            )

    def test_is_lock_error(self):
        assert ho.is_lock_error(RuntimeError(
            "Storage folder /x is already accessed by another instance of Qdrant client."
        ))
        assert not ho.is_lock_error(RuntimeError("collection missing"))

    def test_call_with_lock_retry_passes_through_success_and_foreign_errors(self):
        assert ho.call_with_lock_retry(lambda: 42) == 42
        calls = []

        def boom():
            calls.append(1)
            raise ValueError("not a lock problem")

        with pytest.raises(ValueError):
            ho.call_with_lock_retry(boom, total_seconds=5.0)
        assert len(calls) == 1, "non-lock errors must not be retried"

    def test_call_with_lock_retry_retries_then_succeeds(self):
        attempts = []

        def flaky():
            attempts.append(1)
            if len(attempts) < 3:
                raise RuntimeError("already accessed by another instance")
            return "ok"

        assert ho.call_with_lock_retry(flaky, total_seconds=5.0, initial_delay=0.01) == "ok"
        assert len(attempts) == 3

    def test_call_with_lock_retry_exhausts_and_reraises(self):
        attempts = []

        def held():
            attempts.append(1)
            raise RuntimeError("already accessed by another instance")

        start = time.monotonic()
        with pytest.raises(RuntimeError, match="already accessed"):
            ho.call_with_lock_retry(held, total_seconds=0.3, initial_delay=0.05)
        assert time.monotonic() - start < 5.0
        assert len(attempts) >= 2, "must retry before giving up"

    def test_requester_budget_outlasts_watcher_write_drain_ceiling(self):
        """Round-4 (PR #202): the requester's total patience before the loud
        export_blocked (grant wait + lock-retry ladder) must outlast the API
        watcher's worst-case LEGITIMATE grant latency (write-drain ceiling +
        reader drain + poll tick) — otherwise a 75–120s bulk label/merge in
        flight at request time yields a spurious export_blocked while the
        watcher is still correctly draining. The grant wait itself stays
        short: it is the only cost a headless (no-API) run pays; the
        alignment lives in the ladder, whose open attempts fail fast while
        the lock is held and succeed the moment the grant-side close lands
        (a grant arriving mid-ladder is honored implicitly)."""
        import importlib.util

        from msa_apps.search_api import indexer_manager as im

        # The conftest autouse fixture shrinks GRANT_WAIT_SECONDS for unit
        # tests — this contract is about the PRODUCTION constants, so load
        # a pristine copy of the module straight from source.
        spec = importlib.util.spec_from_file_location("ho_pristine", ho.__file__)
        pristine = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(pristine)

        watcher_worst_case = (
            im._HANDOFF_WRITE_DRAIN_CEILING_SECONDS
            + im._HANDOFF_READ_DRAIN_SECONDS
            + im._HANDOFF_POLL_SECONDS
        )
        requester_total = (
            pristine.GRANT_WAIT_SECONDS + pristine.LOCK_RETRY_TOTAL_SECONDS
        )
        assert requester_total >= watcher_worst_case + 30.0, (
            f"requester budget ({requester_total:.0f}s) must exceed the "
            f"watcher's worst-case grant latency ({watcher_worst_case:.0f}s) "
            "with margin — see the LOCK_RETRY_TOTAL_SECONDS rationale"
        )
        assert pristine.GRANT_WAIT_SECONDS <= 15.0, (
            "the grant wait is the headless-run cost — alignment must live "
            "in the lock-retry ladder, not here (L6)"
        )
        assert pristine.SLOT_WAIT_TOTAL_SECONDS == pristine.LOCK_RETRY_TOTAL_SECONDS, (
            "slot wait and lock ladder are the same budget class (§3.1); "
            "a foreign window's release includes the same drain latency"
        )


class TestAtomicSlotClaim:
    """Round-4 (PR #202): slot acquisition is claim-by-create. The old
    wait-then-write had a TOCTOU window — two fresh concurrent requesters
    could both pass the slot poll before either write landed, and the
    unconditional write_request() let the last writer replace the first
    request; the displaced requester could then delete the other run's
    grant as stale and race it for embedded Qdrant. The O_CREAT|O_EXCL
    create IS the claim now: exactly one creator can win."""

    def test_exclusive_create_only_one_winner(self, tmp_path):
        results: dict[str, bool] = {}
        n = 8
        barrier = threading.Barrier(n)

        def contender(rid: str) -> None:
            barrier.wait()
            results[rid] = ho._try_create_request(rid, tmp_path)

        threads = [
            threading.Thread(target=contender, args=(f"run-{i}",), daemon=True)
            for i in range(n)
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)
        winners = [rid for rid, won in results.items() if won]
        assert len(winners) == 1, f"exactly one claim must win, got {winners}"
        req = ho.read_request(tmp_path)
        assert req is not None and req["run_id"] == winners[0], (
            "the slot must hold the winner's request — no last-writer-wins"
        )

    def test_claim_loses_when_rival_lands_after_cleanup_check(
        self, tmp_path, monkeypatch
    ):
        """The exact round-4 TOCTOU, made deterministic: cleanup_stale
        reports the slot free, but a rival's request lands before our
        write. The exclusive create loses instead of replacing the rival,
        and the bounded wait honors the rival's LIVE window to timeout."""
        proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
        try:
            real_cleanup = ho.cleanup_stale

            def cleanup_then_rival_lands(base=None, pid_alive_fn=None):
                free = real_cleanup(base, pid_alive_fn)
                if free and not ho.request_path(base).exists():
                    _write_foreign_request(base, "rival-run", proc.pid)
                return free

            monkeypatch.setattr(ho, "cleanup_stale", cleanup_then_rival_lands)
            assert (
                ho._claim_request_slot("mine", tmp_path, timeout=0.5, poll=0.05)
                is False
            )
            req = ho.read_request(tmp_path)
            assert req is not None and req["run_id"] == "rival-run", (
                "the rival's claim was replaced — the create must be exclusive"
            )
        finally:
            proc.kill()
            proc.wait(timeout=5)

    def test_handoff_window_claims_via_exclusive_create(self, monkeypatch, tmp_path):
        """handoff_window must route the claim through the exclusive create,
        never the unconditional write_request overwrite."""
        monkeypatch.setenv("MSA_QDRANT_HANDOFF_DIR", str(tmp_path))
        monkeypatch.setattr(ho, "GRANT_WAIT_SECONDS", 0.2)
        claims: list[str] = []
        real_try = ho._try_create_request

        def spying_try(run_id, base=None):
            claims.append(run_id)
            return real_try(run_id, base)

        monkeypatch.setattr(ho, "_try_create_request", spying_try)
        overwrites: list = []
        monkeypatch.setattr(
            ho, "write_request", lambda *a, **k: overwrites.append(a)
        )
        with ho.handoff_window():
            req = ho.read_request(tmp_path)
            assert req is not None and req["run_id"] == claims[-1]
            assert req["pid"] == os.getpid()
        assert claims, "the claim must go through the exclusive create"
        assert overwrites == [], (
            "handoff_window must not overwrite the request slot"
        )
        assert not ho.request_path(tmp_path).exists()

    def test_unparseable_fresh_request_is_a_live_claim(self, tmp_path):
        """The gap between a rival's O_EXCL create and its content write
        reads as an unparseable file — within the grace it is a LIVE
        in-progress claim: preserved, slot busy."""
        path = ho.request_path(tmp_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("", encoding="utf-8")
        assert ho.cleanup_stale(tmp_path) is False
        assert path.exists(), "an in-progress claim must never be clobbered"

    def test_unparseable_stale_request_is_reaped_after_grace(self, tmp_path):
        """A process that died between create and content write must not
        wedge the slot: past the grace the leftover is cleared."""
        path = ho.request_path(tmp_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("", encoding="utf-8")
        old = time.time() - (ho.CLAIM_WRITE_GRACE_SECONDS + 5.0)
        os.utime(path, (old, old))
        assert ho.cleanup_stale(tmp_path) is True
        assert not path.exists()


# ── Pipeline-side handshake (mocked Qdrant, real SQLite, tiny real images) ─────
#
# Harness mirrors tests/test_fingerprint_fastpath.py: run_index with the ML
# stack stubbed out, a real SQLiteStore on tmp_path underneath, and the
# handshake dir pinned via MSA_QDRANT_HANDOFF_DIR.

class _FakeClipEmbedder:
    dim = 8

    def __init__(self, *_args, **_kwargs):
        pass

    def image_embed(self, images):
        import numpy as np
        return [np.zeros(self.dim, dtype=np.float32) for _ in images]


class _FakeQdrant:
    """Records exports and mirrors the recorded version like real Qdrant."""

    def __init__(self):
        self.export_calls = 0
        self.recorded = None
        self.version_read_observer = None

    def do_export(self, *_args, **_kwargs):
        self.export_calls += 1
        return True

    def record_version(self, seq, ts):
        self.recorded = {"index_version_seq": seq, "index_version_ts": ts}

    def get_version(self):
        if self.version_read_observer is not None:
            self.version_read_observer()
        return dict(self.recorded) if self.recorded else None


class _Granter(threading.Thread):
    """Simulates the API-side watcher: sees the request, writes the grant."""

    def __init__(self, base: Path, run_id_override=None, timeout: float = 10.0):
        super().__init__(daemon=True)
        self.base = base
        self.run_id_override = run_id_override
        self.timeout = timeout
        self.granted_run_id = None

    def run(self):
        deadline = time.monotonic() + self.timeout
        while time.monotonic() < deadline:
            req = ho.read_request(self.base)
            if req is not None:
                rid = self.run_id_override or req.get("run_id")
                ho.write_grant(rid, self.base)
                self.granted_run_id = rid
                return
            time.sleep(0.02)


def _make_images(media_dir: Path, count: int) -> list:
    from PIL import Image
    paths = []
    media_dir.mkdir(parents=True, exist_ok=True)
    for i in range(count):
        p = media_dir / f"img_{i:03d}.jpg"
        Image.new("RGB", (8, 8), color=(i % 256, (i * 7) % 256, 200)).save(p)
        paths.append(p)
    return paths


def _pipeline_config(tmp_path: Path, media_dir: Path, **overrides):
    cfg = SimpleNamespace(
        sqlite_path=str(tmp_path / "media.sqlite"),
        thumb_dir=tmp_path / "thumbs",
        face_thumb_dir=tmp_path / "face-thumbs",
        log_dir=tmp_path / "logs",
        model_name="ViT-L-14",
        pretrained="openai",
        model_version="test-v1",
        device="cpu",
        enable_object_detection=False,
        enable_face_recognition=False,
        media_sources=[SimpleNamespace(name="photos", path=str(media_dir), enabled=True)],
    )
    for k, v in overrides.items():
        setattr(cfg, k, v)
    return cfg


@pytest.fixture
def pipeline_harness(tmp_path, monkeypatch):
    from msa_indexer import pipeline

    media_dir = tmp_path / "photos"
    _make_images(media_dir, 2)
    handoff_dir = tmp_path / "handoff-run"
    monkeypatch.setenv("MSA_QDRANT_HANDOFF_DIR", str(handoff_dir))

    qdrant = _FakeQdrant()
    summaries: list = []
    monkeypatch.setattr(pipeline, "resolve_for_access", lambda p: p)
    monkeypatch.setattr(pipeline, "get_exif_basic", lambda _p: {})
    monkeypatch.setattr(pipeline, "write_thumbnail", lambda *_a, **_k: None)
    monkeypatch.setattr(pipeline, "ClipEmbedder", _FakeClipEmbedder)
    monkeypatch.setattr(pipeline, "_load_historical_perf", lambda _p: (1.0, 30.0, 5.0))
    monkeypatch.setattr(pipeline, "_emit_indexer_summary", lambda **p: summaries.append(p))
    monkeypatch.setattr(pipeline, "get_qdrant_export_version", qdrant.get_version)
    monkeypatch.setattr(pipeline, "record_qdrant_export_version", qdrant.record_version)
    monkeypatch.setattr(pipeline, "_do_qdrant_export", qdrant.do_export)

    config = _pipeline_config(tmp_path, media_dir)
    return SimpleNamespace(
        pipeline=pipeline,
        config=config,
        qdrant=qdrant,
        summaries=summaries,
        handoff_dir=handoff_dir,
        media_dir=media_dir,
        monkeypatch=monkeypatch,
    )


class TestPipelineHandshake:
    def test_request_before_first_qdrant_open_grant_accepted_files_cleaned(self, pipeline_harness, monkeypatch):
        """The request must be on disk before ANY Qdrant client construction —
        the version read included (§3.1). With a granter answering, the run
        proceeds under a grant, and finally-cleanup removes both files."""
        h = pipeline_harness
        # Generous budget: the grant arrives in milliseconds and ends the wait
        # early; this must never flake into the timeout path.
        monkeypatch.setattr(ho, "GRANT_WAIT_SECONDS", 10.0)
        observed = {}

        def observer():
            observed["request_at_version_read"] = ho.read_request(h.handoff_dir)
            observed["grant_at_version_read"] = ho.read_grant(h.handoff_dir)

        h.qdrant.version_read_observer = observer
        granter = _Granter(h.handoff_dir)
        granter.start()

        h.pipeline.run_index(h.config)
        granter.join(timeout=10)

        req = observed.get("request_at_version_read")
        assert req is not None, "version read ran without a request file on disk"
        assert observed["grant_at_version_read"] is not None
        assert observed["grant_at_version_read"]["run_id"] == req["run_id"]
        assert granter.granted_run_id == req["run_id"]
        assert h.qdrant.export_calls == 1
        assert h.qdrant.recorded["index_version_seq"] == 1
        # finally-cleanup: both files gone after the run
        assert not ho.request_path(h.handoff_dir).exists()
        assert not ho.granted_path(h.handoff_dir).exists()

    def test_timeout_without_api_proceeds_and_exports(self, pipeline_harness, monkeypatch):
        """Headless run (no API alive): the 15 s grant budget elapses and the
        indexer proceeds anyway (L6). Shrunk here to keep the test fast."""
        h = pipeline_harness
        monkeypatch.setattr(ho, "GRANT_WAIT_SECONDS", 0.3)

        h.pipeline.run_index(h.config)

        assert h.qdrant.export_calls == 1
        assert h.qdrant.recorded["index_version_seq"] == 1
        assert not ho.request_path(h.handoff_dir).exists()
        assert not ho.granted_path(h.handoff_dir).exists()

    def test_mismatched_grant_is_removed_and_never_authorizes(self, pipeline_harness, monkeypatch):
        """A granter echoing the WRONG run_id (stale grant semantics) must be
        ignored and deleted; the run proceeds via the timeout path."""
        h = pipeline_harness
        monkeypatch.setattr(ho, "GRANT_WAIT_SECONDS", 0.5)
        granter = _Granter(h.handoff_dir, run_id_override="not-this-run")
        granter.start()

        h.pipeline.run_index(h.config)
        granter.join(timeout=10)

        assert granter.granted_run_id == "not-this-run"
        assert h.qdrant.export_calls == 1  # proceeded after timeout
        assert not ho.granted_path(h.handoff_dir).exists(), "mismatched grant must be removed"
        assert not ho.request_path(h.handoff_dir).exists()

    def test_lock_error_bounded_retry_then_loud_failure(self, pipeline_harness, monkeypatch):
        """Lock genuinely held after the grant timeout: bounded retry, then
        ERROR + export_blocked terminal summary, NO export, NO version record
        — deferred to the next run's qdrant_stale probe, never silent."""
        h = pipeline_harness
        monkeypatch.setattr(ho, "GRANT_WAIT_SECONDS", 0.2)
        monkeypatch.setattr(ho, "LOCK_RETRY_TOTAL_SECONDS", 0.4)

        attempts = []

        def held_version_read():
            attempts.append(1)
            raise RuntimeError(
                "Storage folder is already accessed by another instance of Qdrant client."
            )

        monkeypatch.setattr(h.pipeline, "get_qdrant_export_version", held_version_read)

        h.pipeline.run_index(h.config)

        assert len(attempts) >= 2, "must retry before declaring the export blocked"
        assert h.qdrant.export_calls == 0, "export must be skipped when blocked"
        assert h.qdrant.recorded is None, "version record must be skipped when blocked"
        phases = [s.get("phase") for s in h.summaries]
        assert phases[-1] == "export_blocked", (
            f"terminal summary must be the loud export_blocked, got {phases[-1]!r}"
        )
        assert phases.count("complete") == 1, (
            "the complete payload must NOT be re-emitted after export_blocked"
        )
        # handshake files cleaned even on the blocked path
        assert not ho.request_path(h.handoff_dir).exists()
        assert not ho.granted_path(h.handoff_dir).exists()

    def test_exporter_exception_still_cleans_files_and_reemits_complete(self, pipeline_harness, monkeypatch):
        h = pipeline_harness
        monkeypatch.setattr(ho, "GRANT_WAIT_SECONDS", 0.2)

        def exploding_export(*_a, **_k):
            raise RuntimeError("exporter blew up mid-flight")

        monkeypatch.setattr(h.pipeline, "_do_qdrant_export", exploding_export)

        h.pipeline.run_index(h.config)

        assert h.qdrant.recorded is None, "failed export must not record a version"
        assert not ho.request_path(h.handoff_dir).exists()
        assert not ho.granted_path(h.handoff_dir).exists()
        phases = [s.get("phase") for s in h.summaries]
        assert phases[-1] == "complete", (
            "a caught exporter failure still ends on the re-emitted complete summary"
        )

    def test_terminal_summary_is_complete_with_full_counters(self, pipeline_harness, monkeypatch):
        """§3.4: IndexerManager keeps only the LAST summary; after a successful
        run it must be phase=complete with the counter payload — not the
        exporting marker emitted before the handshake."""
        h = pipeline_harness
        monkeypatch.setattr(ho, "GRANT_WAIT_SECONDS", 0.2)

        h.pipeline.run_index(h.config)

        phases = [s.get("phase") for s in h.summaries]
        assert "exporting" in phases
        assert phases[-1] == "complete"
        assert phases.index("exporting") < len(phases) - 1
        terminal = h.summaries[-1]
        for key in (
            "total_found", "processed_images", "faces",
            "fingerprint_hits", "files_hashed", "moves_detected",
            "superseded", "missing_marked", "tombstoned", "resurrected",
        ):
            assert key in terminal, f"terminal complete summary lost counter {key!r}"
        # the re-emit is byte-identical to the pre-export complete payload
        completes = [s for s in h.summaries if s.get("phase") == "complete"]
        assert len(completes) == 2 and completes[0] == completes[1]

    def test_nochange_run_still_probes_and_stale_catchup_fires(self, pipeline_harness, monkeypatch):
        """No-change runs keep the version-probe handshake (§3.1 P1 finding):
        the qdrant_stale recovery must still fire on a DB whose seq is ahead
        of Qdrant's record — e.g. after a prior blocked/failed export."""
        h = pipeline_harness
        monkeypatch.setattr(ho, "GRANT_WAIT_SECONDS", 0.3)

        h.pipeline.run_index(h.config)
        assert h.qdrant.export_calls == 1
        assert h.qdrant.recorded["index_version_seq"] == 1

        # Simulate a lost/failed export record: Qdrant believes seq 0.
        h.qdrant.recorded = None
        probe_seen = {}

        def observer():
            probe_seen["request_present"] = ho.request_path(h.handoff_dir).exists()

        h.qdrant.version_read_observer = observer

        h.pipeline.run_index(h.config)  # no content changes this run

        assert probe_seen.get("request_present") is True, (
            "no-change run skipped the probe handshake — qdrant_stale recovery would die"
        )
        assert h.qdrant.export_calls == 2, "stale catch-up export must fire"
        assert h.qdrant.recorded["index_version_seq"] == 1

        # And a truly current no-change run probes but does NOT export.
        probe_seen.clear()
        h.pipeline.run_index(h.config)
        assert probe_seen.get("request_present") is True
        assert h.qdrant.export_calls == 2

    def test_kill_switch_off_skips_handshake_entirely(self, pipeline_harness, monkeypatch):
        h = pipeline_harness
        monkeypatch.setenv("MSA_QDRANT_HANDOFF", "off")
        seen = {}

        def observer():
            seen["request_present"] = ho.request_path(h.handoff_dir).exists()

        h.qdrant.version_read_observer = observer

        h.pipeline.run_index(h.config)

        assert seen.get("request_present") is False, (
            "kill switch off: the pipeline must not write a request file"
        )
        assert h.qdrant.export_calls == 1

    def test_run_export_joins_the_handshake(self, pipeline_harness, monkeypatch):
        """Conductor decision: `msa index export` (run_export) uses the same
        handshake — it is the documented manual repair after a blocked export,
        so it must be able to take the lock from a live API."""
        h = pipeline_harness
        monkeypatch.setattr(ho, "GRANT_WAIT_SECONDS", 0.3)
        # Keep the test hermetic: the real summary-count helper would open
        # embedded Qdrant at the machine's configured path.
        monkeypatch.setattr(h.pipeline, "_read_qdrant_collection_counts", lambda: None)

        # Seed a real DB first.
        h.pipeline.run_index(h.config)
        assert h.qdrant.export_calls == 1

        seen = {}

        def observing_export(*_a, **_k):
            seen["request_present"] = ho.request_path(h.handoff_dir).exists()
            h.qdrant.export_calls += 1
            return True

        monkeypatch.setattr(h.pipeline, "_do_qdrant_export", observing_export)

        h.pipeline.run_export(h.config)

        assert seen.get("request_present") is True, (
            "run_export must request the lock before touching Qdrant"
        )
        assert not ho.request_path(h.handoff_dir).exists()
        assert not ho.granted_path(h.handoff_dir).exists()

    def test_export_only_summary_counts_stay_inside_window(self, pipeline_harness, monkeypatch):
        """Round-2 (PR #202): export-only mode must perform ZERO Qdrant opens
        outside the request→cleanup window. The post-export summary's
        collection counts were the leak — their client must be constructed
        while the request file is still on disk and closed explicitly before
        the window releases (a GC-released reference could outlive it and
        race the reopened API client for the embedded lock)."""
        h = pipeline_harness
        monkeypatch.setattr(ho, "GRANT_WAIT_SECONDS", 0.3)

        # Seed a DB.
        h.pipeline.run_index(h.config)

        constructions: list = []

        class _RecordingClient:
            def __init__(self, path=None, **_kwargs):
                self._entry = {
                    "request_present_at_construction": ho.request_path(h.handoff_dir).exists(),
                }
                constructions.append(self._entry)

            def get_collection(self, name):
                raise RuntimeError(f"collection {name} not found")

            def close(self):
                self._entry["request_present_at_close"] = ho.request_path(h.handoff_dir).exists()

        fake_settings = SimpleNamespace(
            qdrant_path=str(Path(h.config.sqlite_path).parent / "qdrant"),
            collections=SimpleNamespace(image="image_emb", video="video_emb", face="face_emb"),
        )
        # The helper resolves both lazily (function-level imports), so
        # patching the source modules is sufficient and test-scoped.
        monkeypatch.setattr("qdrant_client.QdrantClient", _RecordingClient)
        monkeypatch.setattr("msa_settings.load_config", lambda *_a, **_k: fake_settings)

        h.pipeline.run_export(h.config)

        assert len(constructions) == 1, (
            f"export-only summary must open Qdrant exactly once, saw {len(constructions)}"
        )
        entry = constructions[0]
        assert entry["request_present_at_construction"] is True, (
            "summary client constructed OUTSIDE the handoff window"
        )
        assert entry.get("request_present_at_close") is True, (
            "summary client must be closed explicitly BEFORE the window releases"
        )
        assert not ho.request_path(h.handoff_dir).exists()
        assert not ho.granted_path(h.handoff_dir).exists()

    def test_run_index_busy_slot_fails_loudly_and_preserves_foreign_window(self, pipeline_harness, monkeypatch):
        """Round-3: run_index behind a live foreign window that never clears
        must end on the loud export_blocked summary (§3.1 step 5) with NO
        export and NO version record — and the foreign window untouched."""
        h = pipeline_harness
        monkeypatch.setattr(ho, "SLOT_WAIT_TOTAL_SECONDS", 0.3)
        proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
        try:
            _write_foreign_request(h.handoff_dir, "busy-owner", proc.pid)

            h.pipeline.run_index(h.config)

            assert h.qdrant.export_calls == 0, "export must be skipped when the slot is busy"
            assert h.qdrant.recorded is None, "version record must be skipped when the slot is busy"
            phases = [s.get("phase") for s in h.summaries]
            assert phases[-1] == "export_blocked", (
                f"terminal summary must be the loud export_blocked, got {phases[-1]!r}"
            )
            req = ho.read_request(h.handoff_dir)
            assert req is not None and req["run_id"] == "busy-owner", (
                "the live foreign window was clobbered by run_index"
            )
        finally:
            proc.kill()
            proc.wait(timeout=5)

    def test_run_export_busy_slot_fails_loudly_and_preserves_foreign_window(self, pipeline_harness, monkeypatch):
        """Round-3: `msa index export` behind a live foreign window must log
        the loud failure and return without exporting — never clobber."""
        h = pipeline_harness
        monkeypatch.setattr(ho, "GRANT_WAIT_SECONDS", 0.3)
        # Seed a DB so run_export reaches the handshake.
        h.pipeline.run_index(h.config)
        assert h.qdrant.export_calls == 1

        monkeypatch.setattr(ho, "SLOT_WAIT_TOTAL_SECONDS", 0.3)
        proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
        try:
            _write_foreign_request(h.handoff_dir, "busy-owner", proc.pid)

            h.pipeline.run_export(h.config)

            assert h.qdrant.export_calls == 1, "run_export must not export over a busy slot"
            req = ho.read_request(h.handoff_dir)
            assert req is not None and req["run_id"] == "busy-owner", (
                "the live foreign window was clobbered by run_export"
            )
        finally:
            proc.kill()
            proc.wait(timeout=5)

    def test_run_export_lock_error_retries_in_ladder_and_succeeds_when_freed(
        self, pipeline_harness, monkeypatch
    ):
        """Round-6: export-only + no grant + lock held. The grant timeout is
        LEGITIMATE while the API watcher is inside its write-drain ceiling,
        so the first open must retry through the call_with_lock_retry ladder
        — the grant landing mid-ladder frees the lock and the next attempt
        succeeds — instead of aborting on the first API-held-lock error."""
        h = pipeline_harness
        monkeypatch.setattr(ho, "GRANT_WAIT_SECONDS", 0.2)
        monkeypatch.setattr(h.pipeline, "_read_qdrant_collection_counts", lambda: None)
        # Seed a DB (headless timeout path), then reset the version record so
        # the assertion proves run_export re-recorded it.
        h.pipeline.run_index(h.config)
        assert h.qdrant.export_calls == 1
        h.qdrant.recorded = None

        attempts: list = []

        def export_freed_on_second_attempt(*_a, **_k):
            attempts.append(1)
            if len(attempts) < 2:
                # What the mid-drain API produces: the embedded lock is held
                # until the watcher's close lands (the grant-side release).
                raise RuntimeError(
                    "Storage folder is already accessed by another instance "
                    "of Qdrant client."
                )
            h.qdrant.export_calls += 1
            return True

        monkeypatch.setattr(h.pipeline, "_do_qdrant_export", export_freed_on_second_attempt)

        h.pipeline.run_export(h.config)

        assert len(attempts) == 2, "the ladder must retry the export open"
        assert h.qdrant.export_calls == 2, "export must succeed once the lock frees"
        assert h.qdrant.recorded is not None and h.qdrant.recorded["index_version_seq"] == 1, (
            "the version record must land after a mid-ladder success"
        )
        assert not ho.request_path(h.handoff_dir).exists()
        assert not ho.granted_path(h.handoff_dir).exists()

    def test_run_export_lock_exhaustion_fails_loudly_without_version_record(
        self, pipeline_harness, monkeypatch
    ):
        """Round-6: ladder exhaustion stays the loud failure — ERROR log,
        NO export, NO version record, NO post-export Qdrant opens — and the
        handshake files are cleaned so the API watcher reopens."""
        h = pipeline_harness
        monkeypatch.setattr(ho, "GRANT_WAIT_SECONDS", 0.2)
        monkeypatch.setattr(ho, "LOCK_RETRY_TOTAL_SECONDS", 0.4)
        counts_calls: list = []
        monkeypatch.setattr(
            h.pipeline,
            "_read_qdrant_collection_counts",
            lambda: counts_calls.append(1),
        )
        h.pipeline.run_index(h.config)
        assert h.qdrant.export_calls == 1
        h.qdrant.recorded = None

        attempts: list = []

        def held_export(*_a, **_k):
            attempts.append(1)
            raise RuntimeError(
                "Storage folder is already accessed by another instance of Qdrant client."
            )

        monkeypatch.setattr(h.pipeline, "_do_qdrant_export", held_export)

        h.pipeline.run_export(h.config)  # returns loudly, must not raise

        assert len(attempts) >= 2, "must retry before declaring the export blocked"
        assert h.qdrant.export_calls == 1, "no export lands on the blocked path"
        assert h.qdrant.recorded is None, "no version record on the blocked path"
        assert counts_calls == [], (
            "no post-export Qdrant open may run after the blocked exit"
        )
        assert not ho.request_path(h.handoff_dir).exists()
        assert not ho.granted_path(h.handoff_dir).exists()

    def test_run_export_non_lock_error_propagates_without_retry(
        self, pipeline_harness, monkeypatch
    ):
        """The ladder is for lock contention only: a genuine export failure
        must propagate immediately (existing behavior), not burn the ladder."""
        h = pipeline_harness
        monkeypatch.setattr(ho, "GRANT_WAIT_SECONDS", 0.2)
        monkeypatch.setattr(h.pipeline, "_read_qdrant_collection_counts", lambda: None)
        h.pipeline.run_index(h.config)

        attempts: list = []

        def exploding_export(*_a, **_k):
            attempts.append(1)
            raise RuntimeError("exporter blew up mid-flight")

        monkeypatch.setattr(h.pipeline, "_do_qdrant_export", exploding_export)

        with pytest.raises(RuntimeError, match="blew up"):
            h.pipeline.run_export(h.config)

        assert len(attempts) == 1, "non-lock errors must not be retried"
        assert not ho.request_path(h.handoff_dir).exists(), (
            "handoff cleanup must run even when the exporter raises"
        )


# ── API-side watcher (IndexerManager) ──────────────────────────────────────────

def _wait_until(pred, timeout: float = 5.0, interval: float = 0.02) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if pred():
            return True
        time.sleep(interval)
    return bool(pred())


class _DummyQdrantClient:
    def __init__(self):
        self.closed = False

    def close(self):
        self.closed = True


@pytest.fixture
def manager_harness(monkeypatch, tmp_path):
    """IndexerManager wired to a tmp run dir + the real shared-client module
    (seeded with a dummy client), with the watcher poll shrunk for tests."""
    from msa_apps.search_api import indexer_manager as mod
    import msa_query.storage.qdrant_client as qc

    run_dir = tmp_path / "run"
    run_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(mod, "_RUN_DIR", run_dir)
    # The watcher resolves the HANDOFF dir via _handoff_run_dir() (config-
    # anchored, round-5), not _RUN_DIR — pin it through the top-precedence
    # env override so the real resolution chain is exercised.
    monkeypatch.setenv("MSA_QDRANT_HANDOFF_DIR", str(run_dir))
    monkeypatch.setattr(mod, "_INDEXER_PID_FILE", run_dir / "indexer.pid")
    monkeypatch.setattr(mod, "_INDEXER_STOP_FILE", run_dir / "indexer.stop", raising=False)
    monkeypatch.setattr(mod, "_INDEXER_STARTED_FILE", run_dir / "indexer.started", raising=False)
    monkeypatch.setattr(mod, "_HANDOFF_POLL_SECONDS", 0.02, raising=False)
    # Keep the write-drain ceiling short enough that a regression can never
    # hang a test for the production 120 s; individual tests shrink further.
    monkeypatch.setattr(mod, "_HANDOFF_WRITE_DRAIN_CEILING_SECONDS", 5.0, raising=False)

    resets: list = []
    monkeypatch.setattr("msa_apps.search_api.deps.reset_query_engine", lambda: resets.append(1))

    dummy = _DummyQdrantClient()
    qc._blocked = False
    qc._shared = dummy
    qc._inflight = 0
    qc._inflight_writes = 0

    yield SimpleNamespace(mod=mod, qc=qc, run_dir=run_dir, dummy=dummy, resets=resets)

    qc._blocked = False
    qc._shared = None
    qc._inflight = 0
    qc._inflight_writes = 0


def _running_manager(harness, monkeypatch):
    """A manager in 'running' state with the watcher live (fresh-start shape)."""
    monkeypatch.setattr(harness.mod.IndexerManager, "_restore_from_pid_file", lambda self: None)
    mgr = harness.mod.IndexerManager()
    mgr._status = "running"
    mgr._run_id = "test-run"
    mgr._start_handoff_watcher()
    return mgr


def _finish_watcher(mgr):
    """Stop a manager's watcher. The loop runs for the process lifetime in
    production (so an idle API still answers `msa index export`); tests end
    it explicitly via the stop event."""
    mgr._status = "complete"
    mgr._handoff_stop.set()
    t = mgr._handoff_thread
    if t is not None:
        t.join(timeout=5)
        assert not t.is_alive(), "handoff watcher failed to exit after stop signal"


class TestHandoffWatcher:
    def test_request_drains_closes_and_grants_with_run_id_echo(self, manager_harness, monkeypatch):
        h = manager_harness
        mgr = _running_manager(h, monkeypatch)
        try:
            ho.write_request("indexer-run-1", h.run_dir)
            assert _wait_until(lambda: ho.read_grant(h.run_dir) is not None)
            grant = ho.read_grant(h.run_dir)
            assert grant["run_id"] == "indexer-run-1", "grant must echo the request's run_id"
            assert h.qc.is_blocked() is True
            assert h.dummy.closed is True, "shared client must be closed before the grant"
            assert h.qc._shared is None
        finally:
            _finish_watcher(mgr)

    def test_watcher_grants_config_anchored_request_across_cwd(
        self, manager_harness, monkeypatch, tmp_path
    ):
        """Round-5 e2e (PR #202): with NO env overrides, the watcher polls the
        ACTIVE config's log_dir/run, and an exporter that resolves its slot
        from the same config — from a different cwd — is granted. This is the
        exact manual-repair shape the round-5 finding broke: API and
        `msa index export --config ...` in different working directories."""
        import msa_settings

        h = manager_harness
        # Drop the harness env pin: force the config-anchored resolution.
        monkeypatch.delenv("MSA_QDRANT_HANDOFF_DIR", raising=False)
        cfg_log = tmp_path / "cfg-logs"
        monkeypatch.setattr(
            msa_settings, "load_config",
            lambda *a, **k: SimpleNamespace(log_dir=cfg_log),
        )
        export_cwd = tmp_path / "export-cwd"
        export_cwd.mkdir()
        monkeypatch.chdir(export_cwd)  # exporter cwd != the API's
        mgr = _running_manager(h, monkeypatch)
        try:
            # Exporter-side resolution: the pipeline passes its --config's
            # log_dir; the cwd must play no part.
            base = ho.handoff_dir(log_dir=cfg_log)
            assert base == cfg_log / "run"
            ho.write_request("export-run", base)
            assert _wait_until(lambda: ho.read_grant(base) is not None), (
                "watcher never granted — it is polling a different dir than "
                "the exporter wrote to (cwd-dependent resolution regressed)"
            )
            assert ho.read_grant(base)["run_id"] == "export-run"
            assert h.dummy.closed is True
        finally:
            _finish_watcher(mgr)

    def test_request_disappearance_reopens_and_resets_engine(self, manager_harness, monkeypatch):
        h = manager_harness
        mgr = _running_manager(h, monkeypatch)
        try:
            ho.write_request("indexer-run-2", h.run_dir)
            assert _wait_until(lambda: ho.read_grant(h.run_dir) is not None)
            # Indexer finishes its window: finally-cleanup removes both files
            # (raw removal is what the owner's cleanup_stale performs).
            ho._clear_all(h.run_dir)
            assert _wait_until(lambda: not h.qc.is_blocked())
            assert _wait_until(lambda: len(h.resets) >= 1), (
                "query engine must be reset so the fresh client sees new collections"
            )
            assert not ho.granted_path(h.run_dir).exists()
        finally:
            _finish_watcher(mgr)

    def test_dead_pid_request_is_cleaned_without_grant(self, manager_harness, monkeypatch):
        import subprocess
        import sys

        h = manager_harness
        proc = subprocess.Popen([sys.executable, "-c", "pass"])
        proc.wait(timeout=5)
        mgr = _running_manager(h, monkeypatch)
        try:
            ho._write_json_atomic(
                ho.request_path(h.run_dir),
                {"run_id": "ghost", "pid": proc.pid, "ts": time.time()},
            )
            assert _wait_until(lambda: not ho.request_path(h.run_dir).exists())
            time.sleep(0.1)
            assert ho.read_grant(h.run_dir) is None, "dead requester must never be granted"
            assert h.qc.is_blocked() is False
        finally:
            _finish_watcher(mgr)

    def test_window_owner_death_mid_export_reopens(self, manager_harness, monkeypatch):
        """Indexer crashes hard (no finally-cleanup) while holding the window:
        the watcher's dead-pid fail-safe cleans both files and reopens."""
        import subprocess
        import sys

        h = manager_harness
        proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
        mgr = _running_manager(h, monkeypatch)
        try:
            ho._write_json_atomic(
                ho.request_path(h.run_dir),
                {"run_id": "doomed", "pid": proc.pid, "ts": time.time()},
            )
            assert _wait_until(lambda: ho.read_grant(h.run_dir) is not None)
            assert h.qc.is_blocked() is True
            proc.kill()
            proc.wait(timeout=5)
            assert _wait_until(lambda: not h.qc.is_blocked())
            assert not ho.request_path(h.run_dir).exists()
            assert not ho.granted_path(h.run_dir).exists()
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.wait(timeout=5)
            _finish_watcher(mgr)

    def test_monitor_exit_defers_reopen_to_watcher_over_live_successor_window(
        self, manager_harness, monkeypatch
    ):
        """Round-6 (P2): a standalone exporter can own the SUCCESSOR window
        (back-to-back grant) by the time the spawned indexer's exit reaches
        _monitor — the claim+grant cycle beats the indexer's teardown lag.
        _monitor's on-exit reopen must defer to the watcher: flipping
        _blocked off mid-window lets payload writes bypass their §4 503 and
        commit SQLite over a silently failed sync, and races the granted
        exporter for the embedded lock."""
        import subprocess
        import sys

        h = manager_harness
        mgr = _running_manager(h, monkeypatch)
        exporter = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
        try:
            # Managed window: request → grant (client closed + blocked).
            ho.write_request("managed-run", h.run_dir)
            assert _wait_until(lambda: ho.read_grant(h.run_dir) is not None)
            assert h.qc.is_blocked() is True
            # Managed indexer releases; a LIVE standalone exporter claims the
            # successor slot within one poll tick (back-to-back branch).
            ho._clear_all(h.run_dir)
            ho._write_json_atomic(
                ho.request_path(h.run_dir),
                {"run_id": "successor-export", "pid": exporter.pid, "ts": time.time()},
            )
            assert _wait_until(
                lambda: (ho.read_grant(h.run_dir) or {}).get("run_id")
                == "successor-export"
            ), "watcher must grant the successor window"
            assert h.qc.is_blocked() is True

            # The spawned indexer's exit lands in _monitor NOW.
            proc = subprocess.Popen([sys.executable, "-c", "pass"])
            mgr._process = proc
            mgr._monitor()

            assert h.qc.is_blocked() is True, (
                "_monitor reopened the shared client while the successor "
                "export window is live — payload writes would bypass the 503 "
                "and the API would race the granted exporter for the lock"
            )
            # The successor releases → the WATCHER (owner) reopens.
            ho._clear_all(h.run_dir)
            assert _wait_until(lambda: not h.qc.is_blocked()), (
                "watcher must reopen once the successor window releases"
            )
        finally:
            exporter.kill()
            exporter.wait(timeout=5)
            _finish_watcher(mgr)

    def test_monitor_exit_reopen_crash_net_kept_when_no_live_window(
        self, manager_harness, monkeypatch
    ):
        """The on-exit reopen stays as the crash net when NO live window
        exists — absent request (normal exit) and dead-owner leftovers
        (indexer crashed mid-export before the watcher's fail-safe tick)."""
        import subprocess
        import sys

        h = manager_harness
        mgr = _running_manager(h, monkeypatch)
        try:
            # Absent request: closed-at-export shape, window already released.
            h.qc._blocked = True
            proc = subprocess.Popen([sys.executable, "-c", "pass"])
            mgr._process = proc
            mgr._monitor()
            assert h.qc.is_blocked() is False, "crash-net reopen regressed (no window)"

            # Dead-owner leftovers: not a live window — reopen proceeds.
            h.qc._blocked = True
            _write_foreign_request(h.run_dir, "crashed-run", _dead_pid())
            proc = subprocess.Popen([sys.executable, "-c", "pass"])
            mgr._process = proc
            mgr._monitor()
            assert h.qc.is_blocked() is False, "crash-net reopen regressed (dead owner)"
        finally:
            _finish_watcher(mgr)

    def test_drain_before_grant_inflight_op_completes_and_new_op_blocked(self, manager_harness, monkeypatch):
        """§3.2 drain-before-grant: an in-flight search finishes cleanly before
        qdrant.granted is written; an op arriving after the request gets None."""
        h = manager_harness
        op_entered = threading.Event()
        op_release = threading.Event()
        op_result: dict = {}

        def inflight_op():
            with h.qc.shared_client_op() as client:
                op_result["client"] = client
                op_entered.set()
                op_release.wait(timeout=10)
            op_result["finished_at"] = time.monotonic()

        t = threading.Thread(target=inflight_op, daemon=True)
        t.start()
        assert op_entered.wait(timeout=5)
        assert op_result["client"] is h.dummy

        mgr = _running_manager(h, monkeypatch)
        try:
            ho.write_request("indexer-run-3", h.run_dir)
            # Watcher notices the request and blocks new ops...
            assert _wait_until(lambda: h.qc.is_blocked())
            with h.qc.shared_client_op() as late_client:
                assert late_client is None, "post-request op must get the blocked None path"
            # ...but must NOT grant while the op is still in flight.
            time.sleep(0.2)
            assert ho.read_grant(h.run_dir) is None, "grant written before drain completed"
            op_release.set()
            t.join(timeout=5)
            assert _wait_until(lambda: ho.read_grant(h.run_dir) is not None)
            grant_seen_at = time.monotonic()
            assert op_result["finished_at"] <= grant_seen_at
            assert h.dummy.closed is True
        finally:
            op_release.set()
            _finish_watcher(mgr)

    def test_grant_waits_for_inflight_write_past_reader_cap(self, manager_harness, monkeypatch):
        """Round-2 (PR #202): a §4 payload write in flight at request time
        must hold the grant back even beyond the bounded READER drain — the
        watcher waits behind write holds (drain_writes) because abandoning
        one commits SQLite over a silently failed Qdrant sync."""
        h = manager_harness
        # Reader cap tiny, write ceiling generous: the grant delay we observe
        # can only be the write hold.
        monkeypatch.setattr(h.mod, "_HANDOFF_READ_DRAIN_SECONDS", 0.05, raising=False)
        monkeypatch.setattr(h.mod, "_HANDOFF_WRITE_DRAIN_CEILING_SECONDS", 5.0, raising=False)

        write_entered = threading.Event()
        write_release = threading.Event()
        write_result: dict = {}

        def inflight_write():
            with h.qc.shared_client_op(write=True) as client:
                write_result["client"] = client
                write_entered.set()
                write_release.wait(timeout=10)
            write_result["finished_at"] = time.monotonic()

        t = threading.Thread(target=inflight_write, daemon=True)
        t.start()
        assert write_entered.wait(timeout=5)
        assert write_result["client"] is h.dummy

        mgr = _running_manager(h, monkeypatch)
        try:
            ho.write_request("indexer-run-w1", h.run_dir)
            assert _wait_until(lambda: h.qc.is_blocked())
            # Far past the reader cap: the write hold must still be blocking
            # the grant, and the client must still be open under the write.
            time.sleep(0.4)
            assert ho.read_grant(h.run_dir) is None, (
                "grant written while a payload write was still in flight"
            )
            assert h.dummy.closed is False, (
                "client closed under an in-flight payload write before the ceiling"
            )
            write_release.set()
            t.join(timeout=5)
            assert _wait_until(lambda: ho.read_grant(h.run_dir) is not None)
            assert write_result["finished_at"] <= time.monotonic()
            assert h.dummy.closed is True
        finally:
            write_release.set()
            _finish_watcher(mgr)

    def test_write_drain_ceiling_fires_closes_anyway(self, manager_harness, monkeypatch):
        """The generous hard ceiling: a WEDGED write cannot block the export
        window forever. Past the ceiling the watcher closes and grants anyway
        (loud path) — the write then 503s via the guard's close-generation
        check instead of committing silently-stale state."""
        h = manager_harness
        monkeypatch.setattr(h.mod, "_HANDOFF_READ_DRAIN_SECONDS", 0.05, raising=False)
        monkeypatch.setattr(h.mod, "_HANDOFF_WRITE_DRAIN_CEILING_SECONDS", 0.2, raising=False)

        write_entered = threading.Event()
        write_release = threading.Event()

        def wedged_write():
            with h.qc.shared_client_op(write=True):
                write_entered.set()
                write_release.wait(timeout=10)

        t = threading.Thread(target=wedged_write, daemon=True)
        t.start()
        assert write_entered.wait(timeout=5)

        gen_before = h.qc.close_generation()
        mgr = _running_manager(h, monkeypatch)
        try:
            ho.write_request("indexer-run-w2", h.run_dir)
            assert _wait_until(lambda: ho.read_grant(h.run_dir) is not None, timeout=5)
            # Grant despite the still-held write: closed anyway, loudly.
            assert h.dummy.closed is True
            assert h.qc.close_generation() == gen_before + 1, (
                "the mid-write close must bump the generation the guard checks"
            )
        finally:
            write_release.set()
            t.join(timeout=5)
            _finish_watcher(mgr)

    def test_stale_handshake_files_cleaned_on_init_without_live_indexer(self, manager_harness, monkeypatch):
        """API startup with no live indexer: leftover request/grant files are
        removed and the shared client is available (startup hygiene, L4)."""
        h = manager_harness
        ho.write_request("crashed-run", h.run_dir)
        ho.write_grant("crashed-run", h.run_dir)
        h.qc._blocked = True  # simulate whatever state a crash left behind

        mgr = h.mod.IndexerManager()  # real _restore_from_pid_file, no pid file
        try:
            assert mgr._status == "idle"
            assert not ho.request_path(h.run_dir).exists()
            assert not ho.granted_path(h.run_dir).exists()
            assert h.qc.is_blocked() is False
        finally:
            _finish_watcher(mgr)

    def test_idle_manager_grants_export_only_request(self, manager_harness, monkeypatch):
        """`msa index export` against an idle API (no run active) is the
        documented manual repair after a blocked export — the watcher must be
        alive and answer the request even though no run was ever started."""
        h = manager_harness
        monkeypatch.setattr(h.mod.IndexerManager, "_restore_from_pid_file", lambda self: None)
        mgr = h.mod.IndexerManager()  # never started a run: status stays idle
        try:
            assert mgr._status == "idle"
            assert mgr._handoff_thread is not None and mgr._handoff_thread.is_alive(), (
                "watcher must run for the process lifetime, not just during a run"
            )
            ho.write_request("export-only", h.run_dir)
            assert _wait_until(lambda: ho.read_grant(h.run_dir) is not None)
            assert ho.read_grant(h.run_dir)["run_id"] == "export-only"
            assert h.dummy.closed is True, "shared client must be closed before the grant"
            # Release — reopen + engine reset, identical to a spawned run.
            ho._clear_all(h.run_dir)
            assert _wait_until(lambda: not h.qc.is_blocked())
            assert _wait_until(lambda: len(h.resets) >= 1)
        finally:
            _finish_watcher(mgr)

    def test_watcher_survives_run_end_and_answers_late_request(self, manager_harness, monkeypatch):
        """The loop must NOT exit when a run finishes: an export-only request
        arriving after the run (API back to idle) still gets the window."""
        h = manager_harness
        mgr = _running_manager(h, monkeypatch)
        try:
            mgr._status = "complete"  # run ended
            time.sleep(0.1)  # several poll ticks at the shrunk interval
            assert mgr._handoff_thread is not None and mgr._handoff_thread.is_alive(), (
                "watcher must survive the end of a run"
            )
            ho.write_request("post-run-export", h.run_dir)
            assert _wait_until(lambda: ho.read_grant(h.run_dir) is not None)
            assert ho.read_grant(h.run_dir)["run_id"] == "post-run-export"
        finally:
            _finish_watcher(mgr)

    def test_start_handoff_watcher_is_idempotent(self, manager_harness, monkeypatch):
        """start()/re-attach re-call _start_handoff_watcher() on a manager whose
        __init__ already started it — exactly one watcher may run."""
        h = manager_harness
        mgr = _running_manager(h, monkeypatch)
        try:
            t1 = mgr._handoff_thread
            assert t1 is not None and t1.is_alive()
            mgr._start_handoff_watcher()
            assert mgr._handoff_thread is t1, "second start must not spawn a second watcher"
        finally:
            _finish_watcher(mgr)

    def test_g9_reattach_drives_identical_handshake(self, manager_harness, monkeypatch):
        """G9 (LONG_RUNNING_INDEXING_REQUIREMENTS §7, R1): a manager restored
        from the pid file must run the SAME watcher contract as fresh start —
        request → drain/close → grant → release → reopen."""
        import subprocess
        import sys

        h = manager_harness
        # A live process the restore path accepts as the running indexer.
        proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
        mgr = None
        try:
            (h.run_dir / "indexer.pid").write_text(str(proc.pid))
            monkeypatch.setattr(h.mod.IndexerManager, "_pid_is_indexer", lambda self, pid: True)
            monkeypatch.setattr(h.mod.IndexerManager, "_monitor_pid", lambda self, pid: None)

            mgr = h.mod.IndexerManager()  # __init__ → _restore_from_pid_file → watcher
            assert mgr._status == "running"
            assert mgr._handoff_thread is not None and mgr._handoff_thread.is_alive(), (
                "re-attach must start the handoff watcher (G9 fix)"
            )

            # The restored-run indexer requests the export window.
            ho._write_json_atomic(
                ho.request_path(h.run_dir),
                {"run_id": "restored-run", "pid": proc.pid, "ts": time.time()},
            )
            assert _wait_until(lambda: ho.read_grant(h.run_dir) is not None)
            assert ho.read_grant(h.run_dir)["run_id"] == "restored-run"
            assert h.qc.is_blocked() is True
            assert h.dummy.closed is True

            # Release: identical to fresh start (the restored-run owner's own
            # finally-cleanup — raw removal, simulated from the test process).
            ho._clear_all(h.run_dir)
            assert _wait_until(lambda: not h.qc.is_blocked())
            assert _wait_until(lambda: len(h.resets) >= 1)
        finally:
            proc.kill()
            proc.wait(timeout=5)
            if mgr is not None:
                _finish_watcher(mgr)

    def test_second_exporter_never_clobbers_granted_window_or_reopens_early(self, manager_harness, monkeypatch):
        """Round-3 regression (a): a second `msa index export` starting while
        an earlier handoff is granted must serialize or fail loudly. The
        first window stays undisturbed, and the watcher must NEVER read a
        clobbered request as a release and reopen the shared client while
        the first exporter still holds embedded Qdrant."""
        h = manager_harness
        monkeypatch.setenv("MSA_QDRANT_HANDOFF_DIR", str(h.run_dir))
        monkeypatch.setattr(ho, "SLOT_WAIT_TOTAL_SECONDS", 0.3)
        proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
        mgr = _running_manager(h, monkeypatch)
        try:
            # Exporter 1 (foreign process) requests and is granted.
            _write_foreign_request(h.run_dir, "exporter-1", proc.pid)
            assert _wait_until(lambda: ho.read_grant(h.run_dir) is not None)
            assert h.qc.is_blocked() is True

            # Exporter 2 (this process) tries to open a window: bounded wait,
            # then loud failure — never a clobber.
            with pytest.raises(ho.HandoffSlotBusyError):
                with ho.handoff_window():
                    pytest.fail("second window must not open over a granted one")

            # First window fully undisturbed; watcher never reopened early.
            time.sleep(0.2)  # several poll ticks
            req = ho.read_request(h.run_dir)
            assert req is not None and req["run_id"] == "exporter-1"
            assert ho.read_grant(h.run_dir)["run_id"] == "exporter-1"
            assert h.qc.is_blocked() is True, (
                "watcher reopened the shared client while exporter-1 held the window"
            )
            assert h.resets == [], "query engine reset before the window released"

            # Exporter 1 dies → the dead-pid fail-safe still cleans up (crash
            # guarantees preserved) and reopens.
            proc.kill()
            proc.wait(timeout=5)
            assert _wait_until(lambda: not h.qc.is_blocked())
            assert not ho.request_path(h.run_dir).exists()
            assert not ho.granted_path(h.run_dir).exists()
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.wait(timeout=5)
            _finish_watcher(mgr)

    def test_api_startup_preserves_live_standalone_export_handshake(self, manager_harness, monkeypatch):
        """Round-3 regression (b): API startup during a live standalone
        `msa index export` (request on disk, no indexer.pid) must preserve
        the handshake and serve it like a fresh request arrival — no
        deletion, no grant-timeout contention."""
        h = manager_harness
        proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
        mgr = None
        try:
            _write_foreign_request(h.run_dir, "standalone-export", proc.pid)

            mgr = h.mod.IndexerManager()  # real _restore_from_pid_file, no pid file
            assert mgr._status == "idle"
            # Preserved — NOT classified stale despite the missing indexer.pid.
            req = ho.read_request(h.run_dir)
            assert req is not None and req["run_id"] == "standalone-export", (
                "startup deleted a live standalone-export request"
            )
            # Treated like a fresh arrival: blocked immediately, then granted.
            assert h.qc.is_blocked() is True, (
                "startup must not leave the shared client constructible over "
                "a live export window"
            )
            assert _wait_until(lambda: ho.read_grant(h.run_dir) is not None)
            assert ho.read_grant(h.run_dir)["run_id"] == "standalone-export"

            # Exporter releases → reopen + engine reset, business as usual.
            ho._clear_all(h.run_dir)
            assert _wait_until(lambda: not h.qc.is_blocked())
            assert _wait_until(lambda: len(h.resets) >= 1)
        finally:
            proc.kill()
            proc.wait(timeout=5)
            if mgr is not None:
                _finish_watcher(mgr)

    def test_api_startup_cleans_dead_foreign_owner_files(self, manager_harness, monkeypatch):
        """Round-3 regression (c): genuinely stale files — a DEAD foreign
        owner — are still cleaned on startup (L4 mitigation preserved)."""
        h = manager_harness
        _write_foreign_request(h.run_dir, "crashed-foreign", _dead_pid())
        ho.write_grant("crashed-foreign", h.run_dir)
        h.qc._blocked = True  # whatever state a crash left behind

        mgr = h.mod.IndexerManager()  # real _restore_from_pid_file, no pid file
        try:
            assert mgr._status == "idle"
            assert not ho.request_path(h.run_dir).exists()
            assert not ho.granted_path(h.run_dir).exists()
            assert h.qc.is_blocked() is False
        finally:
            _finish_watcher(mgr)

    def test_watcher_exit_preserves_live_foreign_window(self, manager_harness, monkeypatch):
        """The watcher's exit cleanup is owner-aware too: stopping while a
        LIVE foreign exporter holds the window must leave the files (and the
        blocked client) alone — reopening under the exporter is the exact
        lock race the handshake prevents. Dead-owner exit cleanup keeps
        working (covered by the same cleanup_stale routing)."""
        h = manager_harness
        proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
        mgr = _running_manager(h, monkeypatch)
        try:
            _write_foreign_request(h.run_dir, "live-owner", proc.pid)
            assert _wait_until(lambda: ho.read_grant(h.run_dir) is not None)

            mgr._handoff_stop.set()
            t = mgr._handoff_thread
            t.join(timeout=5)
            assert not t.is_alive()

            req = ho.read_request(h.run_dir)
            assert req is not None and req["run_id"] == "live-owner", (
                "watcher exit clobbered a live foreign window"
            )
            assert ho.read_grant(h.run_dir)["run_id"] == "live-owner"
            assert h.qc.is_blocked() is True, (
                "watcher exit reopened the shared client under a live export window"
            )
        finally:
            proc.kill()
            proc.wait(timeout=5)

    def test_kill_switch_off_watcher_not_started(self, manager_harness, monkeypatch):
        h = manager_harness
        monkeypatch.setenv("MSA_QDRANT_HANDOFF", "off")
        mgr = _running_manager(h, monkeypatch)
        try:
            assert mgr._handoff_thread is None, "kill switch off: no watcher thread"
            ho.write_request("legacy-run", h.run_dir)
            time.sleep(0.2)
            assert ho.read_grant(h.run_dir) is None, (
                "kill switch off: a request file must never be granted"
            )
        finally:
            _finish_watcher(mgr)


# ── §4 write rejection during the handoff window ───────────────────────────────

@pytest.fixture
def api_client(tmp_path, monkeypatch):
    """TestClient over a fresh DB (idiom from tests/test_people_api.py)."""
    from fastapi.testclient import TestClient
    from msa_indexer.db.sqlite_store import SQLiteStore

    db_path = tmp_path / "test.db"
    schema_path = Path(__file__).parents[1] / "src" / "msa_indexer" / "db" / "schema.sql"
    store = SQLiteStore(db_path)
    store.init_schema(schema_path)
    store.conn.execute(
        "INSERT INTO media(media_id, path, mime) VALUES(?, ?, ?)",
        ("m1", "/tmp/photo1.jpg", "image/jpeg"),
    )
    store.conn.execute(
        "INSERT INTO face(face_id, media_id, x, y, w, h, confidence) VALUES(?, ?, 0.1, 0.1, 0.2, 0.2, 0.9)",
        ("f1", "m1"),
    )
    store.conn.execute(
        "INSERT INTO face(face_id, media_id, x, y, w, h, confidence) VALUES(?, ?, 0.3, 0.3, 0.2, 0.2, 0.8)",
        ("f2", "m1"),
    )
    store.commit()
    store.close()

    test_config = SimpleNamespace(
        sqlite_path=str(db_path),
        server=SimpleNamespace(qdrant_url="http://localhost:6333", qdrant_api_key=None),
        collections=SimpleNamespace(face="face_emb"),
        thumb_dir=tmp_path / "thumbnails",
        face_thumb_dir=tmp_path / "face_thumbnails",
        log_dir=tmp_path / "logs",
        log_level="DEBUG",
    )
    for d in (test_config.thumb_dir, test_config.face_thumb_dir, test_config.log_dir):
        d.mkdir(exist_ok=True)

    monkeypatch.setattr(
        "msa_indexer.db.sqlite_store.load_global_config", lambda *_a, **_k: test_config
    )
    monkeypatch.setattr("msa_indexer.db.qdrant_sync._get_qdrant_client", lambda: None)

    from msa_apps.search_api.app import create_app
    app = create_app(config_override=test_config, reset_dependencies=True)

    import msa_query.storage.qdrant_client as qc
    qc._blocked = False
    qc._shared = None
    qc._inflight = 0
    qc._inflight_writes = 0
    yield SimpleNamespace(client=TestClient(app), db_path=db_path, qc=qc)
    qc._blocked = False
    qc._shared = None
    qc._inflight = 0
    qc._inflight_writes = 0


def _person_id(api, name: str) -> str:
    resp = api.client.post("/people", json={"name": name})
    assert resp.status_code == 200
    return resp.json()["person_id"]


def _face_person(db_path: Path, face_id: str):
    import sqlite3
    conn = sqlite3.connect(str(db_path))
    try:
        row = conn.execute(
            "SELECT person_id FROM face WHERE face_id=?", (face_id,)
        ).fetchone()
        return row[0] if row else None
    finally:
        conn.close()


def _person_name(db_path: Path, person_id: str):
    import sqlite3
    conn = sqlite3.connect(str(db_path))
    try:
        row = conn.execute(
            "SELECT name FROM person WHERE person_id=?", (person_id,)
        ).fetchone()
        return row[0] if row else None
    finally:
        conn.close()


class TestWriteRejectionDuringHandoff:
    def _activate_window(self, api):
        """Simulate the watcher having blocked for the export window."""
        api.qc.block_shared_client()

    def test_all_six_endpoints_reject_with_503_while_window_active(self, api_client):
        api = api_client
        pid_a = _person_id(api, "Alice")
        pid_b = _person_id(api, "Bob")
        self._activate_window(api)

        calls = [
            ("POST", "/faces/f1/label", {"person_id": pid_a}),
            ("POST", "/faces/label-batch", {"face_ids": ["f1", "f2"], "person_id": pid_a}),
            ("DELETE", "/faces/f1/label", None),
            ("POST", "/faces/bulk_label", {"face_ids": ["f1", "f2"], "person_id": pid_a}),
            ("PATCH", f"/people/{pid_a}", {"name": "Alicia"}),
            ("POST", f"/people/{pid_a}/merge", {"source_id": pid_b}),
        ]
        for method, url, body in calls:
            resp = api.client.request(method, url, json=body)
            assert resp.status_code == 503, f"{method} {url} returned {resp.status_code}"
            assert "Finalizing index" in resp.json()["detail"], f"{method} {url}"
            assert resp.headers.get("Retry-After"), f"{method} {url} missing Retry-After"

    def test_rejection_happens_before_sqlite_commit(self, api_client):
        """No committed-but-unsynced state may exist after a 503 (§4)."""
        api = api_client
        pid_a = _person_id(api, "Alice")
        self._activate_window(api)

        resp = api.client.post("/faces/f1/label", json={"person_id": pid_a})
        assert resp.status_code == 503
        assert _face_person(api.db_path, "f1") is None, (
            "face row mutated despite the 503 — rejection must precede the SQLite write"
        )

        resp = api.client.patch(f"/people/{pid_a}", json={"name": "Alicia"})
        assert resp.status_code == 503
        assert _person_name(api.db_path, pid_a) == "Alice", (
            "person renamed despite the 503"
        )

    def test_person_create_untouched_by_rejection(self, api_client):
        """POST /people has no Qdrant payload — it stays available (§4)."""
        api = api_client
        self._activate_window(api)
        resp = api.client.post("/people", json={"name": "Carol"})
        assert resp.status_code == 200

    def test_normal_path_unaffected_when_window_inactive(self, api_client):
        api = api_client
        pid_a = _person_id(api, "Alice")
        resp = api.client.post("/faces/f1/label", json={"person_id": pid_a})
        assert resp.status_code == 200
        assert _face_person(api.db_path, "f1") == pid_a
        resp = api.client.patch(f"/people/{pid_a}", json={"name": "Alicia"})
        assert resp.status_code == 200

    def test_kill_switch_off_does_not_reject(self, api_client, monkeypatch):
        """With MSA_QDRANT_HANDOFF=off, _blocked spans the whole run and the
        pre-S-2 behavior — commit + silently skipped Qdrant sync — remains."""
        api = api_client
        pid_a = _person_id(api, "Alice")
        monkeypatch.setenv("MSA_QDRANT_HANDOFF", "off")
        self._activate_window(api)

        resp = api.client.post("/faces/f1/label", json={"person_id": pid_a})
        assert resp.status_code == 200
        assert _face_person(api.db_path, "f1") == pid_a

    def test_window_opening_mid_sqlite_write_syncs_on_guarded_client(self, api_client, monkeypatch):
        """TOCTOU half of §4: the window opening AFTER the guard's entry check
        but DURING the SQLite mutation must not produce a committed label whose
        payload sync silently no-ops. The whole write runs under the in-flight
        guard, so the sync still receives the pre-close client — and because
        the refcount is held, the watcher's drain() waits behind this write
        instead of closing the client mid-request."""
        api = api_client

        class _Dummy:
            def close(self):
                pass

        dummy = _Dummy()
        api.qc._shared = dummy
        pid_a = _person_id(api, "Alice")

        from msa_indexer.db.sqlite_store import SQLiteStore
        real_update = SQLiteStore.update_face_person
        seen: dict = {}

        def racing_update(self, face_id, person_id):
            seen["inflight_during_sqlite"] = api.qc._inflight
            api.qc.block_shared_client()  # watcher reacting to a request mid-write
            return real_update(self, face_id, person_id)

        monkeypatch.setattr(SQLiteStore, "update_face_person", racing_update)

        def capture_sync(face_id, person_id, person_name, collection="face_emb", client=None):
            seen["sync_client"] = client
            return True

        # app.py imports the helper inside the handler body, so patching the
        # source module is sufficient.
        monkeypatch.setattr("msa_indexer.db.qdrant_sync.update_face_payload", capture_sync)

        resp = api.client.post("/faces/f1/label", json={"person_id": pid_a})
        assert resp.status_code == 200
        assert seen["inflight_during_sqlite"] >= 1, (
            "the write must hold the in-flight refcount across the SQLite "
            "mutation — otherwise drain() cannot wait behind it"
        )
        assert seen["sync_client"] is dummy, (
            "payload sync must run on the guarded pre-close client, not fall "
            "back to None (silent-skip)"
        )
        assert _face_person(api.db_path, "f1") == pid_a

    def test_slow_write_during_handoff_completes_before_close(self, api_client, monkeypatch):
        """Round-2 (PR #202), happy half: a payload write in flight when the
        handoff request arrives finishes — SQLite commit AND Qdrant sync on
        the still-open client — before the watcher's drain sequence closes.
        drain_writes() queues the close behind the write hold."""
        api = api_client

        class _Recording:
            def __init__(self):
                self.closed = False
                self.payloads = []

            def set_payload(self, **kwargs):
                self.payloads.append(kwargs)

            def close(self):
                self.closed = True

        dummy = _Recording()
        api.qc._shared = dummy
        pid_a = _person_id(api, "Alice")

        from msa_indexer.db.sqlite_store import SQLiteStore
        real_update = SQLiteStore.update_face_person
        in_sqlite = threading.Event()
        release = threading.Event()

        def slow_update(self, face_id, person_id):
            in_sqlite.set()
            release.wait(timeout=10)
            return real_update(self, face_id, person_id)

        monkeypatch.setattr(SQLiteStore, "update_face_person", slow_update)

        result: dict = {}

        def call_endpoint():
            result["resp"] = api.client.post("/faces/f1/label", json={"person_id": pid_a})

        et = threading.Thread(target=call_endpoint, daemon=True)
        et.start()
        assert in_sqlite.wait(timeout=5)

        # The watcher's drain sequence arriving mid-write.
        drained: dict = {}

        def watcher_close():
            api.qc.block_shared_client()
            drained["writes"] = api.qc.drain_writes(5.0)
            drained["reads"] = api.qc.drain(1.0)
            api.qc.close_shared_client()

        wt = threading.Thread(target=watcher_close, daemon=True)
        wt.start()
        time.sleep(0.2)
        assert "writes" not in drained, "drain_writes must wait behind the in-flight write"
        assert dummy.closed is False, "client closed while the write was still in flight"

        release.set()
        et.join(timeout=10)
        wt.join(timeout=10)
        assert drained["writes"] is True
        assert result["resp"].status_code == 200
        assert dummy.payloads, "payload sync must have run against the still-open client"
        assert _face_person(api.db_path, "f1") == pid_a
        assert dummy.closed is True

    def test_ceiling_close_mid_write_returns_503_not_silent_200(self, api_client, monkeypatch):
        """Round-2 (PR #202), residual half: if the drain_writes hard ceiling
        fired and the client was closed under an in-flight write, the request
        must fail with the retryable 503 — never 200 with the qdrant_sync
        failure swallowed. A retry after the window converges both stores."""
        api = api_client

        class _Dummy:  # no set_payload: the real sync helper swallows the failure
            def close(self):
                pass

        api.qc._shared = _Dummy()
        pid_a = _person_id(api, "Alice")

        from msa_indexer.db.sqlite_store import SQLiteStore
        real_update = SQLiteStore.update_face_person

        def update_with_ceiling_close(self, face_id, person_id):
            # Simulate: request arrived mid-write, ceiling elapsed, watcher
            # closed anyway — all before this write finished its body.
            api.qc.block_shared_client()
            api.qc.close_shared_client()
            return real_update(self, face_id, person_id)

        monkeypatch.setattr(SQLiteStore, "update_face_person", update_with_ceiling_close)

        resp = api.client.post("/faces/f1/label", json={"person_id": pid_a})
        assert resp.status_code == 503, (
            f"a write outliving the closed client must fail loudly, got {resp.status_code}"
        )
        assert "Finalizing index" in resp.json()["detail"]
        assert resp.headers.get("Retry-After")
        assert _face_person(api.db_path, "f1") is None, (
            "round-4: the SQLite mutation must be ROLLED BACK on the ceiling "
            "503 — state unchanged, so the retry replays cleanly"
        )

        # Window over: the documented retry converges SQLite AND Qdrant.
        monkeypatch.setattr(SQLiteStore, "update_face_person", real_update)
        api.qc.reopen_shared_client()
        api.qc._shared = _Dummy()  # pre-seed: no lazy real-client construction in tests
        synced: dict = {}

        def capture_sync(face_id, person_id, person_name, collection="face_emb", client=None):
            synced["client"] = client
            return True

        monkeypatch.setattr("msa_indexer.db.qdrant_sync.update_face_payload", capture_sync)
        resp = api.client.post("/faces/f1/label", json={"person_id": pid_a})
        assert resp.status_code == 200
        assert _face_person(api.db_path, "f1") == pid_a
        assert "client" in synced, "retry must re-sync the payload"

    def test_merge_ceiling_close_mid_sync_rolls_back_and_retry_succeeds(self, api_client, monkeypatch):
        """Round-4 (PR #202): the close-generation check firing while a merge
        is SYNCING must roll back the already-executed SQLite mutation. The
        old commit-then-503 shape had already deleted the source person, so
        the advertised retry failed the src-existence check forever and the
        unpatched Qdrant face payloads stayed under a deleted person until a
        full export. After the 503: source person still exists, faces
        unchanged; the SAME request replays cleanly after the window."""
        api = api_client

        class _Dummy:
            def close(self):
                pass

        api.qc._shared = _Dummy()
        pid_a = _person_id(api, "Alice")
        pid_b = _person_id(api, "Bob")
        # Put a face under the source person so the rollback is observable.
        resp = api.client.post("/faces/f1/label", json={"person_id": pid_b})
        assert resp.status_code == 200

        def sync_with_ceiling_close(
            source_id, target_id, target_name, collection="face_emb", client=None
        ):
            # Ceiling fired mid-sync: the watcher closed the shared client
            # under us; the real helper swallows the failure and returns 0.
            api.qc.block_shared_client()
            api.qc.close_shared_client()
            return 0

        monkeypatch.setattr(
            "msa_indexer.db.qdrant_sync.sync_person_merge", sync_with_ceiling_close
        )
        resp = api.client.post(f"/people/{pid_a}/merge", json={"source_id": pid_b})
        assert resp.status_code == 503
        assert "Finalizing index" in resp.json()["detail"]
        assert _person_name(api.db_path, pid_b) == "Bob", (
            "the source person must survive the 503 (SQLite rolled back) — "
            "otherwise the retry 404s on the src-existence check"
        )
        assert _face_person(api.db_path, "f1") == pid_b, (
            "the face reassignment must be rolled back with the merge"
        )

        # Window over: the SAME merge request replays cleanly.
        api.qc.reopen_shared_client()
        api.qc._shared = _Dummy()
        synced: dict = {}

        def capture_sync(
            source_id, target_id, target_name, collection="face_emb", client=None
        ):
            synced["args"] = (source_id, target_id, target_name)
            return 1

        monkeypatch.setattr(
            "msa_indexer.db.qdrant_sync.sync_person_merge", capture_sync
        )
        resp = api.client.post(f"/people/{pid_a}/merge", json={"source_id": pid_b})
        assert resp.status_code == 200
        assert resp.json()["reassigned"] == 1
        assert _person_name(api.db_path, pid_b) is None, (
            "the retry must complete the merge (source person deleted)"
        )
        assert _face_person(api.db_path, "f1") == pid_a
        assert synced["args"][:2] == (pid_b, pid_a), (
            "the retry must re-run the Qdrant sync for the same merge"
        )

    def test_label_batch_ceiling_close_mid_sync_rolls_back(self, api_client, monkeypatch):
        """Round-4 companion: the same rollback semantics on the batch-label
        path — after the ceiling 503, no face keeps the half-applied label."""
        api = api_client

        class _Dummy:
            def close(self):
                pass

        api.qc._shared = _Dummy()
        pid_a = _person_id(api, "Alice")

        def batch_sync_with_ceiling_close(
            face_ids, person_id, person_name,
            collection="face_emb", chunk_size=1000, client=None,
        ):
            api.qc.block_shared_client()
            api.qc.close_shared_client()
            return 0

        monkeypatch.setattr(
            "msa_indexer.db.qdrant_sync.set_face_person_batch",
            batch_sync_with_ceiling_close,
        )
        resp = api.client.post(
            "/faces/label-batch", json={"face_ids": ["f1", "f2"], "person_id": pid_a}
        )
        assert resp.status_code == 503
        assert "Finalizing index" in resp.json()["detail"]
        assert _face_person(api.db_path, "f1") is None, "batch label must roll back"
        assert _face_person(api.db_path, "f2") is None, "batch label must roll back"

    def test_window_already_open_at_entry_rejects_before_any_sqlite_touch(self, api_client, monkeypatch):
        """Companion bound for the TOCTOU test: when the window is already
        active at guard entry, the 503 fires before SQLiteStore is even
        constructed (no partial state, no person auto-create)."""
        api = api_client
        self._activate_window(api)
        opened = []
        from msa_indexer.db.sqlite_store import SQLiteStore
        real_init = SQLiteStore.__init__

        def counting_init(self, *a, **k):
            opened.append(1)
            return real_init(self, *a, **k)

        monkeypatch.setattr(SQLiteStore, "__init__", counting_init)

        resp = api.client.post("/faces/f1/label", json={"name": "Newly Created"})
        assert resp.status_code == 503
        assert opened == [], "503 must precede any SQLite open"
