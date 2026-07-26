"""Unit tests for the Tauri desktop-shell sidecar entry (M-7/S-1 spec §1.3).

Covers the pure-glue contract logic without spawning uvicorn or a real supervisor:
  - SIDECAR_PORT / SUPERVISOR_PID parsing + validation
  - the recycle-proof parent-watchdog (getppid drift; pid-reuse can't fool it)
  - the non-deadlocking SIGTERM handler
  - PATH prepend for the bundled exiftool/mediainfo
  - a CORS preflight regression: tauri://localhost must be allowed (guards against
    a later tightening reintroducing the CORS regression)
  - the diagnostics api_url derives from the bound SIDECAR_PORT in shell mode
"""

import logging
import os

import pytest

from msa_apps.search_api import sidecar


@pytest.fixture
def clean_root_logger():
    """Snapshot/restore root-logger handlers + level so a test that installs the unified-log
    handler doesn't leak it (and its open file) into the rest of the run."""
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


# ── SIDECAR_PORT / SUPERVISOR_PID parsing ────────────────────────────────────


def test_sidecar_port_reads_env():
    assert sidecar.sidecar_port({"SIDECAR_PORT": "54321"}) == 54321


def test_sidecar_port_missing_raises():
    with pytest.raises(RuntimeError, match="SIDECAR_PORT is not set"):
        sidecar.sidecar_port({})


def test_sidecar_port_non_integer_raises():
    with pytest.raises(RuntimeError, match="not an integer"):
        sidecar.sidecar_port({"SIDECAR_PORT": "abc"})


def test_sidecar_port_out_of_range_raises():
    with pytest.raises(RuntimeError, match="out of range"):
        sidecar.sidecar_port({"SIDECAR_PORT": "70000"})


def test_supervisor_pid_parses_and_defaults():
    assert sidecar.supervisor_pid({"SUPERVISOR_PID": "4242"}) == 4242
    assert sidecar.supervisor_pid({}) == 0
    assert sidecar.supervisor_pid({"SUPERVISOR_PID": "  "}) == 0
    assert sidecar.supervisor_pid({"SUPERVISOR_PID": "notapid"}) == 0


# ── parent-watchdog liveness ─────────────────────────────────────────────────


def test_parent_alive_true_for_current_process():
    assert sidecar.parent_alive(os.getpid()) is True


def test_parent_alive_nonpositive_is_alive():
    assert sidecar.parent_alive(0) is True
    assert sidecar.parent_alive(-1) is True


@pytest.mark.skipif(os.name == "nt", reason="POSIX reparent semantics")
def test_parent_reparented_detects_drift():
    # We started as pid 100's child (expected_ppid == supervisor_pid); getppid now returns 1
    # (init reparented us) → the supervisor is gone.
    assert sidecar.parent_reparented(100, expected_ppid=100, getppid=lambda: 1) is True
    # getppid still returns the supervisor pid → alive.
    assert sidecar.parent_reparented(100, expected_ppid=100, getppid=lambda: 100) is False


@pytest.mark.skipif(os.name == "nt", reason="POSIX reparent semantics")
def test_parent_reparented_ignores_non_child_topology():
    # We were NOT the supervisor's direct child at start → drift isn't a meaningful signal.
    assert sidecar.parent_reparented(100, expected_ppid=999, getppid=lambda: 1) is False


def test_watchdog_tick_fires_on_dead():
    fired = []
    dead = sidecar._watchdog_tick(5, alive=lambda _p: False, on_dead=lambda: fired.append(True))
    assert dead is True and fired == [True]


def test_watchdog_tick_noop_when_alive():
    fired = []
    dead = sidecar._watchdog_tick(5, alive=lambda _p: True, on_dead=lambda: fired.append(True))
    assert dead is False and fired == []


def test_start_parent_watchdog_exits_when_supervisor_gone():
    exits: list[int] = []
    calls = {"n": 0}

    def _alive(_pid):
        calls["n"] += 1
        return calls["n"] < 2  # alive once, then gone

    sleeps: list[float] = []
    t = sidecar.start_parent_watchdog(
        1234,
        interval=0,
        alive=_alive,
        exit_fn=lambda code: exits.append(code),
        sleep=lambda s: sleeps.append(s),
    )
    t.join(timeout=2.0)
    assert exits == [0]  # os._exit(0)-equivalent stub fired exactly once


class _BrokenStderr:
    """A stderr whose pipe reader (the supervisor) is dead: every write/flush raises EPIPE.
    Exactly the stream state _on_dead runs against — the supervisor held the read end."""

    def write(self, _text):
        raise BrokenPipeError(32, "Broken pipe")

    def flush(self):
        raise BrokenPipeError(32, "Broken pipe")


def test_watchdog_reaps_even_when_stderr_pipe_is_broken(monkeypatch, caplog):
    """THE field-orphan regression: the supervisor dies → its end of the sidecar's stderr pipe
    closes → the watchdog's farewell stderr write raises BrokenPipeError. That exception used to
    kill the watchdog thread BEFORE exit_fn(0) ran, leaving a live backend orphaned on the port
    and holding the instance lock (so every later app launch died on 'already running'). The
    reap must fire regardless of stderr's state."""
    import sys as _sys

    monkeypatch.setattr(_sys, "stderr", _BrokenStderr())
    exits: list[int] = []
    with caplog.at_level(logging.INFO, logger="msa_apps.search_api.sidecar"):
        t = sidecar.start_parent_watchdog(
            1234,
            interval=0,
            alive=lambda _pid: False,  # supervisor gone on the first tick
            exit_fn=lambda code: exits.append(code),
            sleep=lambda s: None,
        )
        t.join(timeout=2.0)
    assert exits == [0], "watchdog must reap even when its stderr pipe is broken"
    # The reap is also visible in the unified msa-desktop.log — the only place a user can
    # actually see it (stderr goes to the dead supervisor).
    assert any("is gone" in r.getMessage() for r in caplog.records)


def test_watchdog_thread_survives_probe_exception(caplog):
    """A liveness probe that raises (odd OS/ctypes state) must not silently kill the watchdog
    thread — it keeps polling and still reaps when the probe later reports the supervisor gone."""
    calls = {"n": 0}

    def _alive(_pid):
        calls["n"] += 1
        if calls["n"] == 1:
            raise OSError("transient probe failure")
        return False  # then: supervisor gone

    exits: list[int] = []
    with caplog.at_level(logging.WARNING, logger="msa_apps.search_api.sidecar"):
        t = sidecar.start_parent_watchdog(
            1234,
            interval=0,
            alive=_alive,
            exit_fn=lambda code: exits.append(code),
            sleep=lambda s: None,
        )
        t.join(timeout=2.0)
    assert exits == [0]
    assert any("probe failed" in r.getMessage() for r in caplog.records)


def test_safe_stderr_never_raises(monkeypatch):
    import sys as _sys

    monkeypatch.setattr(_sys, "stderr", _BrokenStderr())
    sidecar._safe_stderr("must not raise\n")  # the assertion is: no exception


# ── SIGTERM handler ──────────────────────────────────────────────────────────


def test_install_sigterm_exits_hard(monkeypatch):
    import signal as _signal

    handlers = {}
    monkeypatch.setattr(_signal, "signal", lambda sig, fn: handlers.__setitem__(sig, fn))
    exits: list[int] = []
    sidecar.install_sigterm(exit_fn=lambda code: exits.append(code))
    handlers[_signal.SIGTERM](_signal.SIGTERM, None)
    assert exits == [0]


# ── PATH prepend for bundled tools ───────────────────────────────────────────


def test_prepend_tools_noop_when_unset():
    env = {"PATH": "/usr/bin"}
    assert sidecar.prepend_tools_to_path(env) is None
    assert env["PATH"] == "/usr/bin"


def test_prepend_tools_prepends_dir(tmp_path):
    tools = tmp_path / "bin"
    tools.mkdir()
    env = {"MSA_TOOLS_DIR": str(tools), "PATH": "/usr/bin"}
    assert sidecar.prepend_tools_to_path(env) == str(tools)
    assert env["PATH"].split(os.pathsep)[0] == str(tools)
    assert "/usr/bin" in env["PATH"].split(os.pathsep)


def test_prepend_tools_idempotent(tmp_path):
    tools = tmp_path / "bin"
    tools.mkdir()
    env = {"MSA_TOOLS_DIR": str(tools), "PATH": f"{tools}{os.pathsep}/usr/bin"}
    sidecar.prepend_tools_to_path(env)
    # Not duplicated.
    assert env["PATH"].count(str(tools)) == 1


def test_prepend_tools_missing_dir_is_noop(tmp_path):
    env = {"MSA_TOOLS_DIR": str(tmp_path / "does-not-exist"), "PATH": "/usr/bin"}
    assert sidecar.prepend_tools_to_path(env) is None
    assert env["PATH"] == "/usr/bin"


# ── CORS regression: the webview origin must preflight OK ────────────


def test_cors_preflight_from_tauri_origin_allowed():
    from fastapi.testclient import TestClient
    from msa_apps.search_api.app import app

    client = TestClient(app)
    resp = client.options(
        "/search",
        headers={
            "Origin": "tauri://localhost",
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "content-type",
        },
    )
    assert resp.status_code in (200, 204)
    # Starlette echoes the request Origin when allow_origins=["*"] + allow_credentials=True.
    assert resp.headers.get("access-control-allow-origin") == "tauri://localhost"


def test_cors_preflight_from_tauri_localhost_https_allowed():
    from fastapi.testclient import TestClient
    from msa_apps.search_api.app import app

    client = TestClient(app)
    resp = client.options(
        "/search",
        headers={
            "Origin": "https://tauri.localhost",
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "content-type",
        },
    )
    assert resp.status_code in (200, 204)
    assert resp.headers.get("access-control-allow-origin") == "https://tauri.localhost"


# ── unified rotating desktop log (spec §S-2.5) ───────────────────────────────


def test_configure_unified_log_none_without_log_dir(clean_root_logger):
    assert sidecar.configure_unified_log(env={}) is None
    # No handler installed when there's no LOG_DIR (dev/browser mode).
    assert not any(getattr(h, "_msa_desktop_unified", False) for h in clean_root_logger.handlers)


def test_configure_unified_log_writes_and_is_idempotent(tmp_path, clean_root_logger):
    path = sidecar.configure_unified_log(tmp_path)
    assert path == tmp_path / "msa-desktop.log"
    marked = [h for h in clean_root_logger.handlers if getattr(h, "_msa_desktop_unified", False)]
    assert len(marked) == 1

    logging.getLogger("uvicorn.error").info("backend online")
    for h in marked:
        h.flush()
    assert "backend online" in path.read_text(encoding="utf-8")

    # Second call must NOT add a second rotating handle on the same file (LOG-001).
    sidecar.configure_unified_log(tmp_path)
    marked2 = [h for h in clean_root_logger.handlers if getattr(h, "_msa_desktop_unified", False)]
    assert len(marked2) == 1


def test_configure_unified_log_reads_msa_log_dir(tmp_path, clean_root_logger):
    assert sidecar.configure_unified_log(env={"MSA_LOG_DIR": str(tmp_path)}) == tmp_path / "msa-desktop.log"


def test_configure_unified_log_returns_none_when_handler_cannot_be_created(
    tmp_path, clean_root_logger, monkeypatch
):
    """If the rotating file handler can't be created (permission/IO error), configure_unified_log
    must return None — NOT the path — and install no handler. run() uses the return value to
    decide whether to suppress uvicorn's default logging: returning the path would set
    log_config=None with no file handler in place, dropping every log line. None keeps uvicorn's
    default (stderr) logging so logs never silently disappear."""
    import logging.handlers as _handlers

    def _boom(*_a, **_k):
        raise OSError("cannot open log file")

    monkeypatch.setattr(_handlers, "RotatingFileHandler", _boom)
    result = sidecar.configure_unified_log(tmp_path)
    assert result is None  # falsy → run() falls back to uvicorn.config.LOGGING_CONFIG
    assert not any(
        getattr(h, "_msa_desktop_unified", False) for h in clean_root_logger.handlers
    )


# ── diagnostics api_url derives from the bound sidecar port ──────────────────


def test_effective_api_url_prefers_sidecar_port(monkeypatch):
    from types import SimpleNamespace
    from msa_apps.search_api.app import _effective_api_url

    monkeypatch.setenv("SIDECAR_PORT", "54321")
    cfg = SimpleNamespace(api=SimpleNamespace(port=8000))
    assert _effective_api_url(cfg) == "http://127.0.0.1:54321"


def test_effective_api_url_falls_back_to_config_port(monkeypatch):
    from types import SimpleNamespace
    from msa_apps.search_api.app import _effective_api_url

    monkeypatch.delenv("SIDECAR_PORT", raising=False)
    cfg = SimpleNamespace(api=SimpleNamespace(port=8123))
    assert _effective_api_url(cfg) == "http://localhost:8123"


# ── _serve: bind retry, crash capture, non-zero early-exit (spec §1.3) ───────
#
# These drive _serve with fake sockets/"servers" whose .run() returns immediately — modelling
# uvicorn coming up (started True), early-exiting (started False), or crashing (raises). No
# real uvicorn, no real sockets.


class _FakeSocket:
    """Stand-in for the SO_REUSEADDR listening socket _serve hands to uvicorn — records close()."""

    def __init__(self):
        self.closed = False

    def close(self):
        self.closed = True


def _ok_bind(host, port):
    """A ``bind`` that always succeeds — the port was takeable (the SO_REUSEADDR fix working)."""
    return _FakeSocket()


def _flaky_bind(fail_first: int):
    """A ``bind`` that raises OSError for the first ``fail_first`` calls (port not yet takeable),
    then succeeds — models the real Windows bind race now surfaced as a real OSError."""
    n = {"c": 0}

    def bind(host, port):
        n["c"] += 1
        if n["c"] <= fail_first:
            raise OSError(48, "address already in use")
        return _FakeSocket()

    return bind, n


class _FakeServer:
    """Stand-in for uvicorn.Server: .run(sockets=...) returns at once (the worker thread then
    dies), setting ``started`` to model whether uvicorn came up (True) or early-exited (False). If
    ``raises`` is given, .run() raises it instead — a deterministic startup crash (bad app import)."""

    def __init__(self, *, ready: bool = False, raises: BaseException | None = None):
        self.started = False
        self._ready = ready
        self._raises = raises

    def run(self, sockets=None):
        if self._raises is not None:
            raise self._raises
        self.started = self._ready


def _factory(readiness):
    """A server_factory yielding a _FakeServer per attempt, ready per the ``readiness`` list;
    also records every server it built so a test can count attempts."""
    built = []
    seq = iter(readiness)

    def make():
        srv = _FakeServer(ready=next(seq))
        built.append(srv)
        return srv

    return make, built


def test_serve_returns_without_retry_when_first_bind_wins():
    make, built = _factory([True])
    exits: list[int] = []
    sleeps: list[float] = []
    sidecar._serve(
        make, port=5000, attempts=5, backoff=0.01, bind=_ok_bind,
        exit_fn=lambda c: exits.append(c), sleep=lambda d: sleeps.append(d),
    )
    assert len(built) == 1  # bound + ready on the first try
    assert exits == []      # never signalled failure
    assert sleeps == []     # no backoff needed


def test_serve_recovers_when_bind_fails_then_succeeds():
    """The real fix's happy-recovery path: the port isn't takeable for the first two tries (OSError),
    then the SO_REUSEADDR bind wins — no failure signalled, backoff between the failed binds."""
    bind, calls = _flaky_bind(fail_first=2)
    make, built = _factory([True])  # once bind succeeds, uvicorn comes up first try
    exits: list[int] = []
    sleeps: list[float] = []
    sidecar._serve(
        make, port=5000, attempts=5, backoff=0.5, max_backoff=4.0, bind=bind,
        exit_fn=lambda c: exits.append(c), sleep=lambda d: sleeps.append(d),
    )
    assert calls["c"] == 3       # bind tried 3× (2 fail, 1 win)
    assert len(built) == 1       # the server is only built once the port is taken
    assert exits == []           # a bind that eventually wins is NOT a failure
    assert sleeps == [0.5, 1.0]  # backoff before bind retries 2 and 3


def test_serve_exits_when_bind_fails_every_attempt(caplog):
    """Bind never succeeds → exit(1), and the ACTUAL OSError is logged (this is what was invisible
    on the real hardware — uvicorn's Windows bind losing the just-freed responder port)."""
    bind, calls = _flaky_bind(fail_first=99)  # always fails
    made = {"n": 0}

    def make():
        made["n"] += 1
        return _FakeServer(ready=True)

    exits: list[int] = []
    sleeps: list[float] = []
    with caplog.at_level(logging.WARNING, logger="msa_apps.search_api.sidecar"):
        sidecar._serve(
            make, port=63241, attempts=3, backoff=0.5, max_backoff=4.0, bind=bind,
            exit_fn=lambda c: exits.append(c), sleep=lambda d: sleeps.append(d),
        )
    assert calls["c"] == 3        # tried the full bind budget
    assert made["n"] == 0         # never even built a server — bind never yielded a socket
    assert exits == [1]
    assert sleeps == [0.5, 1.0]   # backoff between attempts, none after the last
    blob = " ".join(r.message for r in caplog.records)
    assert "could not bind" in blob and "address already in use" in blob


def test_serve_retries_when_uvicorn_early_exits_then_succeeds():
    make, built = _factory([False, False, True])  # bind ok each time; uvicorn early-exits twice
    exits: list[int] = []
    sleeps: list[float] = []
    sidecar._serve(
        make, port=5000, attempts=5, backoff=0.5, max_backoff=4.0, bind=_ok_bind,
        exit_fn=lambda c: exits.append(c), sleep=lambda d: sleeps.append(d),
    )
    assert len(built) == 3   # stopped retrying as soon as uvicorn came up
    assert exits == []       # a retry that eventually wins is NOT a failure
    assert sleeps == [0.5, 1.0]  # exponential backoff before attempts 2 and 3


def test_serve_exits_nonzero_after_exhausting_attempts():
    make, built = _factory([False] * 3)  # bind ok, but uvicorn early-exits every attempt
    exits: list[int] = []
    sleeps: list[float] = []
    sidecar._serve(
        make, port=5000, attempts=3, backoff=0.5, max_backoff=4.0, bind=_ok_bind,
        exit_fn=lambda c: exits.append(c), sleep=lambda d: sleeps.append(d),
    )
    assert len(built) == 3       # tried the full budget
    assert exits == [1]          # then exited non-zero so the supervisor sees a failure
    assert sleeps == [0.5, 1.0]  # backoff between attempts, none after the last


def test_serve_backoff_is_capped_at_max_backoff():
    make, _ = _factory([False] * 6)
    sleeps: list[float] = []
    sidecar._serve(
        make, port=5000, attempts=6, backoff=0.5, max_backoff=2.0, bind=_ok_bind,
        exit_fn=lambda c: None, sleep=lambda d: sleeps.append(d),
    )
    assert sleeps == [0.5, 1.0, 2.0, 2.0, 2.0]  # 0.5,1,2,4,8 → capped at 2.0


def test_serve_logs_failure_through_root_logger(caplog):
    """Every early-exit is visible in the unified log (root-propagating), not just stderr."""
    make, _ = _factory([False, False])
    with caplog.at_level(logging.WARNING, logger="msa_apps.search_api.sidecar"):
        sidecar._serve(
            make, port=54321, attempts=2, backoff=0.0, max_backoff=0.0, bind=_ok_bind,
            exit_fn=lambda c: None, sleep=lambda d: None,
        )
    blob = " ".join(r.message for r in caplog.records)
    assert "54321" in blob                    # the port is named for diagnosis
    assert "retrying in" in blob              # the intermediate warning
    assert "before becoming ready" in blob    # the final give-up error


def test_serve_logs_listening_line_after_bind(caplog, monkeypatch):
    """Handing uvicorn a pre-bound socket suppresses its own 'Uvicorn running on …' banner, so
    _serve logs an honest listening line for humans reading msa-desktop.log. (Machine port
    discovery uses the sidecar-port file — see the publish_port tests — not this line.)"""
    monkeypatch.delenv("MSA_LOG_DIR", raising=False)  # publish_port no-ops; only the log matters
    make, _ = _factory([True])
    with caplog.at_level(logging.INFO, logger="msa_apps.search_api.sidecar"):
        sidecar._serve(
            make, host="127.0.0.1", port=49237, attempts=1, bind=_ok_bind,
            exit_fn=lambda c: None, sleep=lambda d: None,
        )
    blob = "\n".join(r.getMessage() for r in caplog.records)
    assert "listening on http://127.0.0.1:49237" in blob, blob


# ── publish_port: the sidecar-port file (issue #172) ─────────────────────────
#
# The authoritative, machine-readable port-discovery source: written atomically next to the
# unified log right after the bind; the desktop BVT validators read it (and still gate on
# /health "ready", so a stale file can never green-light a dead backend).


def test_publish_port_writes_file_under_msa_log_dir(tmp_path):
    path = sidecar.publish_port(54321, env={"MSA_LOG_DIR": str(tmp_path)})
    assert path == tmp_path / "sidecar-port"
    assert path.read_text(encoding="utf-8") == "54321\n"


def test_publish_port_noop_without_log_dir():
    assert sidecar.publish_port(54321, env={}) is None  # dev/browser mode — nothing to publish


def test_publish_port_overwrites_previous_launch(tmp_path):
    sidecar.publish_port(1111, env={"MSA_LOG_DIR": str(tmp_path)})
    sidecar.publish_port(2222, env={"MSA_LOG_DIR": str(tmp_path)})
    assert (tmp_path / "sidecar-port").read_text(encoding="utf-8") == "2222\n"
    assert not list(tmp_path.glob("*.tmp"))  # atomic replace leaves no droppings


def test_serve_closes_socket_between_attempts():
    """A socket handed to uvicorn that early-exits must be closed before the next bind, or the retry
    leaks the fd (and could itself hold the port)."""
    socks: list[_FakeSocket] = []

    def bind(host, port):
        s = _FakeSocket()
        socks.append(s)
        return s

    make, _ = _factory([False, True])  # early-exit once, then ready
    sidecar._serve(
        make, port=5000, attempts=5, backoff=0.0, max_backoff=0.0, bind=bind,
        exit_fn=lambda c: None, sleep=lambda d: None,
    )
    assert socks[0].closed is True   # the early-exit attempt's socket was released
    assert socks[-1].closed is False  # the winning socket is left open for uvicorn


def test_serve_fails_fast_with_traceback_when_worker_crashes(caplog):
    """A deterministic startup crash (raised in the worker, e.g. a torch DLL that won't load) must
    NOT be retried: capture it, log the full traceback, and exit(1) at once."""
    built: list[_FakeServer] = []
    exits: list[int] = []
    sleeps: list[float] = []
    boom = RuntimeError("DLL load failed while importing torch")

    def make():
        srv = _FakeServer(raises=boom)
        built.append(srv)
        return srv

    with caplog.at_level(logging.ERROR, logger="msa_apps.search_api.sidecar"):
        sidecar._serve(
            make, port=63241, attempts=5, backoff=0.5, max_backoff=4.0, bind=_ok_bind,
            exit_fn=lambda c: exits.append(c), sleep=lambda d: sleeps.append(d),
        )

    assert len(built) == 1   # a deterministic crash is NOT retried
    assert exits == [1]      # exited non-zero so the supervisor sees a failure
    assert sleeps == []      # no backoff budget burned
    crash_records = [r for r in caplog.records if "crashed during startup" in r.message]
    assert crash_records, "expected a crash log record on the sidecar logger"
    assert crash_records[0].exc_info is not None  # full traceback captured, not just a one-liner
