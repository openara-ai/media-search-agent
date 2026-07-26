"""Sidecar runtime entry — honor the desktop-app-template sidecar contract (M-7 spec §1.3).

The vendored Tauri supervisor (``src-tauri/src/main.rs``) spawns the MSA backend as a
child process, handing it an ephemeral port via ``SIDECAR_PORT`` and its own pid via
``SUPERVISOR_PID``, then polls ``GET /health`` until it returns 200. This module is the
Python half of that contract — pure glue (no engine logic), so it stays small and obvious:

  #1 take a port    — bind ``127.0.0.1`` on ``SIDECAR_PORT`` (never a fixed port, never 0.0.0.0)
  #2 signal ready   — the existing ``/health`` (the supervisor's ``ready_path``); the app's
                      lifespan sets ``app.state._ready`` and ``/health`` returns
                      ``{"status": "ready"|"starting"}`` (``app.py`` ~L376), unchanged.
  #3 exit cleanly   — a **non-deadlocking** SIGTERM handler → ``os._exit(0)`` (uvicorn's own
                      graceful drain only fires on the main thread and risks a deadlock here),
                      plus a **parent-watchdog**: if the supervisor dies without SIGTERM (a hard
                      quit), reap ourselves so no orphaned uvicorn keeps the port. On POSIX the
                      primary signal is ``os.getppid()`` drifting off the supervisor's pid — we
                      are its *direct* child, so its death reparents us (recycle-proof, unlike
                      ``os.kill(pid, 0)`` which another process reusing the pid can fool). On
                      Windows the venv ``python.exe`` is a two-process launcher tree and the
                      supervisor's ``TerminateProcess`` reaches only the stub, so the watchdog is
                      load-bearing; it uses ``OpenProcess``/``GetExitCodeProcess`` (never
                      ``os.kill`` — on Windows that would *kill* the supervisor).
  #4 answer CORS    — handled in ``app.py`` (``allow_origins=["*"], allow_credentials=True``;
                      Starlette echoes the request Origin, so ``tauri://localhost`` /
                      ``http(s)://tauri.localhost`` preflights succeed — see the regression test).
  #5 bind 127.0.0.1 — see #1; loopback only.

The plain ``msa api start`` / ``scripts/start.sh`` path is untouched — it keeps its own
uvicorn on ``config.yaml`` ``api.port`` (browser mode, same origin). This module is only the
bundled-sidecar entry, invoked by the provisioning shim (``python -m app`` →
``src-tauri/backend/app/__main__.py``) after first-run provisioning completes.
"""

from __future__ import annotations

import functools
import logging
import os
import shutil
import signal
import socket
import sys
import threading
import time
from collections.abc import Callable
from pathlib import Path

HOST = "127.0.0.1"  # contract #5 — loopback only, never 0.0.0.0
_WATCHDOG_INTERVAL_S = 2.0
_APP_PATH = "msa_apps.search_api.app:app"

# Root-propagating logger → the unified msa-desktop.log (once configure_unified_log runs). A
# startup/bind failure MUST surface here, not only on the child's invisible stderr (the shim's
# stderr isn't teed to the log), or a field first-launch failure is undiagnosable.
_log = logging.getLogger(__name__)

# Unified rotating desktop log — MIRROR of src-tauri/backend/app/applog.py (spec §S-2.5). The
# shim (a bundle resource) and this module (the library) can't import each other across the
# package boundary and run in the SAME shim→uvicorn process, so both target one file with the
# same sentinel mark: whichever configures first wins, the other no-ops (LOG-001 single-owner).
_UNIFIED_LOG_NAME = "msa-desktop.log"
_UNIFIED_MARK = "_msa_desktop_unified"
_UNIFIED_FORMAT = "%(asctime)s %(levelname)s %(name)s: %(message)s"

# Published-port file (issue #172): the authoritative, machine-readable source of the ephemeral
# port this sidecar bound, written next to the unified log so external tools (notably the desktop
# BVT validators) can discover it deterministically instead of scraping uvicorn's log banner.
_SIDECAR_PORT_FILE = "sidecar-port"


def _unified_log_dir(env: dict[str, str] | None = None) -> Path | None:
    """The ADR-009 LOG_DIR the shim exported (``MSA_LOG_DIR``); ``None`` in dev/browser mode
    where ``msa api start`` — not this entry — owns logging."""
    environ = os.environ if env is None else env
    raw = (environ.get("MSA_LOG_DIR") or "").strip()
    return Path(raw) if raw else None


def publish_port(port: int, *, env: dict[str, str] | None = None) -> Path | None:
    """Write the bound sidecar port to ``MSA_LOG_DIR/sidecar-port`` (plain text, ``<port>\\n``) so
    external tools can discover the app's ephemeral port deterministically — the authoritative
    alternative to scraping uvicorn's log banner (issue #172; the desktop BVT validators read this).

    Best-effort: atomic (temp + ``os.replace``) and never raises — a publish failure must not block
    the backend. Returns the path written, or ``None`` when no LOG_DIR is known (dev/browser mode)
    or the write failed. NOT removed on exit (the sidecar exits via ``os._exit``), so a reader must
    still confirm liveness — e.g. a ``/health`` poll, which the BVT already does."""
    log_dir = _unified_log_dir(env)
    if log_dir is None:
        return None
    path = log_dir / _SIDECAR_PORT_FILE
    tmp = log_dir / f"{_SIDECAR_PORT_FILE}.{os.getpid()}.tmp"
    try:
        log_dir.mkdir(parents=True, exist_ok=True)
        tmp.write_text(f"{port}\n", encoding="utf-8")
        os.replace(tmp, path)
    except OSError:
        try:
            tmp.unlink()
        except OSError:
            pass
        return None
    return path


def configure_unified_log(
    log_dir: Path | str | None = None,
    *,
    env: dict[str, str] | None = None,
    max_bytes: int = 5 * 1024 * 1024,
    backups: int = 3,
) -> Path | None:
    """Attach the shared rotating ``msa-desktop.log`` handler to the root logger so uvicorn's
    output lands in the same user-visible file as the shim's (spec §S-2.5). Idempotent (detects
    the shim's already-installed handler by its sentinel mark) and never raises. Returns the
    installed log path, or ``None`` when NO handler is in place — either no LOG_DIR is known
    (dev/browser — not this entry) OR the handler couldn't be created. The return value is the
    signal ``run()`` uses to decide whether to suppress uvicorn's default logging, so it must be
    truthy ONLY when a real handler exists (else uvicorn's output would vanish)."""
    from logging.handlers import RotatingFileHandler

    ldir = Path(log_dir) if log_dir is not None else _unified_log_dir(env)
    if ldir is None:
        return None
    path = ldir / _UNIFIED_LOG_NAME
    root = logging.getLogger()
    for handler in root.handlers:
        if getattr(handler, _UNIFIED_MARK, False):
            return path  # already configured (e.g. by the shim) — one rotating handle per file
    try:
        ldir.mkdir(parents=True, exist_ok=True)
        handler = RotatingFileHandler(path, maxBytes=max_bytes, backupCount=backups, encoding="utf-8")
    except OSError:
        # Handler creation failed (permission/IO). Return None (NOT the path): no handler was
        # installed, so run() must keep uvicorn's default logging — returning the path would set
        # log_config=None and drop every log line into a non-existent file handler. Degrade to
        # stderr, never to silence. Logging setup must not block the backend.
        return None
    handler.setFormatter(logging.Formatter(_UNIFIED_FORMAT))
    setattr(handler, _UNIFIED_MARK, True)
    root.addHandler(handler)
    if root.level == logging.NOTSET or root.level > logging.INFO:
        root.setLevel(logging.INFO)
    return path


def sidecar_port(env: dict[str, str] | None = None) -> int:
    """The port the supervisor assigned, from ``SIDECAR_PORT``. Raises a clear error if unset
    or out of range — ``python -m app`` is *only* a supervisor-spawned entry (use
    ``msa api start`` in dev), so a missing port is a misuse, not a runtime condition to paper
    over."""
    environ = os.environ if env is None else env
    raw = environ.get("SIDECAR_PORT")
    if not raw:
        raise RuntimeError(
            "SIDECAR_PORT is not set — the sidecar entry is spawned by the Tauri supervisor; "
            "for local development run `msa api start` instead."
        )
    try:
        port = int(raw)
    except ValueError as e:
        raise RuntimeError(f"SIDECAR_PORT={raw!r} is not an integer") from e
    if not (1 <= port <= 65535):
        raise RuntimeError(f"SIDECAR_PORT={port} is out of range (1..65535)")
    return port


def supervisor_pid(env: dict[str, str] | None = None) -> int:
    """The supervisor's pid from ``SUPERVISOR_PID`` (0 when unset/blank — no watchdog)."""
    environ = os.environ if env is None else env
    raw = (environ.get("SUPERVISOR_PID") or "").strip()
    try:
        return int(raw) if raw else 0
    except ValueError:
        return 0


def parent_alive(pid: int) -> bool:
    """Is process ``pid`` still alive? Cross-platform liveness for the parent-watchdog:
    ``OpenProcess`` + ``GetExitCodeProcess`` on Windows (``os.kill(pid, 0)`` is unreliable
    there and the venv sidecar is a two-process tree), ``os.kill(pid, 0)`` on POSIX. ``pid <=
    0`` ⇒ no parent declared ⇒ treated as alive (the watchdog never starts in that case)."""
    if pid <= 0:
        return True
    if os.name == "nt":  # Windows — never os.kill (it would terminate the supervisor)
        import ctypes
        from ctypes import wintypes

        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        STILL_ACTIVE = 259
        kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
        handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not handle:
            return False  # cannot open ⇒ gone (or never existed)
        try:
            code = wintypes.DWORD()
            if not kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
                return False
            return code.value == STILL_ACTIVE
        finally:
            kernel32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False  # reaped — the supervisor is gone
    except PermissionError:
        return True  # exists but not ours to signal — still alive
    return True


def parent_reparented(
    supervisor_pid: int, *, expected_ppid: int, getppid: Callable[[], int] = os.getppid
) -> bool:
    """Recycle-proof orphan signal (POSIX). We are spawned as the supervisor's **direct
    child**, so when it dies the kernel reparents us (to init/pid 1, or a subreaper) and
    ``os.getppid()`` stops returning its pid. That drift is an *unfakeable* "the supervisor is
    gone": ``getppid()`` reads the kernel's live parent link, so unlike ``os.kill(pid, 0)`` it
    can never be fooled by another process later **reusing** the supervisor's pid.

    Guarded two ways to avoid a *false* reap: returns False on Windows (no reparent-on-death
    semantics — the Windows path stays on ``parent_alive``), and only when we actually started
    as the supervisor's child (``expected_ppid == supervisor_pid``)."""
    if supervisor_pid <= 0 or os.name == "nt":
        return False
    if expected_ppid != supervisor_pid:
        return False  # we weren't its direct child at start → the drift signal isn't meaningful
    return getppid() != supervisor_pid


def _supervisor_present(
    pid: int, expected_ppid: int, *, getppid: Callable[[], int] = os.getppid
) -> bool:
    """The watchdog's default liveness: the supervisor is present only if it has **not**
    reparented us away (recycle-proof, POSIX) AND its pid still exists (belt-and-suspenders,
    and the sole check on Windows). Either failing ⇒ reap ourselves."""
    if pid <= 0:
        return True
    if parent_reparented(pid, expected_ppid=expected_ppid, getppid=getppid):
        return False
    return parent_alive(pid)


def _safe_stderr(text: str) -> None:
    """Write+flush to stderr without ever raising. The sidecar's stderr is a pipe whose read end
    the supervisor holds, so the moment the supervisor dies every stderr write raises
    ``BrokenPipeError`` — and most of this module's stderr writes happen exactly when the
    supervisor may be gone. A diagnostic write must never abort the code path that exits or
    reaps the process (the field orphan: the watchdog's stderr write killed the watchdog thread
    before ``exit_fn(0)`` ran, leaving a live backend holding the port + instance lock)."""
    try:
        sys.stderr.write(text)
        sys.stderr.flush()
    except Exception:
        pass


def _watchdog_tick(pid: int, *, alive: Callable[[int], bool], on_dead: Callable[[], None]) -> bool:
    """One liveness check: if ``pid`` is gone, fire ``on_dead`` and report dead. Factored out
    so the decision is unit-testable without spawning a thread or exiting the interpreter."""
    if alive(pid):
        return False
    on_dead()
    return True


def start_parent_watchdog(
    pid: int,
    *,
    interval: float = _WATCHDOG_INTERVAL_S,
    alive: Callable[[int], bool] | None = None,
    exit_fn: Callable[[int], None] = os._exit,
    sleep: Callable[[float], None] = time.sleep,
    getppid: Callable[[], int] = os.getppid,
) -> threading.Thread:
    """Poll the supervisor's liveness on a daemon thread; ``os._exit(0)`` the moment it's gone,
    so a hard-killed supervisor (no SIGTERM delivered) can't leave the backend orphaned on the
    port. The default check is recycle-proof: it samples our parent pid **once** here (we start
    as the supervisor's direct child, so it equals ``pid``) and treats a later ``getppid()``
    drift as the supervisor's death. ``getppid`` is injectable for tests."""
    _alive = alive or functools.partial(
        _supervisor_present, expected_ppid=getppid(), getppid=getppid
    )

    def _on_dead() -> None:
        # The reap must be unstoppable. By the time this fires the supervisor is dead, so
        # stderr (a pipe it held) raises EPIPE on write — which used to kill this thread
        # before exit_fn(0) ran, leaving a live orphan holding the port + instance lock.
        # Diagnostics go to the unified log (a file handler — safe) and a never-raising
        # stderr write; the finally guarantees the exit regardless.
        try:
            _log.info("supervisor pid %d is gone — exiting", pid)
            _safe_stderr(f"[sidecar] supervisor pid {pid} is gone - exiting\n")
        finally:
            exit_fn(0)

    def _loop() -> None:
        warned = False
        while True:
            try:
                if _watchdog_tick(pid, alive=_alive, on_dead=_on_dead):
                    return  # _on_dead already exited; only reached if exit_fn is a test stub
            except Exception:
                # A probe failure must not kill the watchdog thread — a silently dead
                # watchdog is exactly how orphans happen. Warn once, keep polling.
                if not warned:
                    warned = True
                    _log.warning("parent-watchdog probe failed; still polling", exc_info=True)
            sleep(interval)

    thread = threading.Thread(target=_loop, name="parent-watchdog", daemon=True)
    thread.start()
    return thread


def install_sigterm(exit_fn: Callable[[int], None] = os._exit) -> None:
    """Contract #3: a **non-deadlocking** SIGTERM handler. The supervisor sends SIGTERM on
    quit; we ``os._exit(0)`` immediately rather than draining (a graceful
    ``Server.shutdown()`` from the handler risks a deadlock). SIGINT gets the same hard
    exit."""

    def _handler(_signum: int, _frame: object) -> None:
        exit_fn(0)

    signal.signal(signal.SIGTERM, _handler)
    # SIGINT may be unavailable in odd embeddings; never let registration failure block startup.
    try:
        signal.signal(signal.SIGINT, _handler)
    except (ValueError, OSError):  # pragma: no cover
        pass


def prepend_tools_to_path(env: dict[str, str] | None = None) -> str | None:
    """Prepend the bundled exiftool/mediainfo directory to ``PATH`` so the indexer's
    ``shutil.which("exiftool")`` / ``pymediainfo`` resolve the app-owned binaries first.

    The directory is published by the provisioning shim as ``MSA_TOOLS_DIR`` (it resolves the
    bundle's ``<Resources>/bin`` from its own location). No-op for dev/browser mode (the var is
    unset → the system-installed tools on PATH are used, exactly as today). Idempotent: skips
    if the dir is already the first PATH entry. Returns the dir it prepended, or ``None``."""
    environ = os.environ if env is None else env
    tools_dir = environ.get("MSA_TOOLS_DIR")
    if not tools_dir or not os.path.isdir(tools_dir):
        return None
    current = environ.get("PATH", "")
    parts = current.split(os.pathsep) if current else []
    if parts and os.path.normpath(parts[0]) == os.path.normpath(tools_dir):
        return tools_dir  # already first
    environ["PATH"] = os.pathsep.join([tools_dir, *[p for p in parts if p != tools_dir]])
    return tools_dir


def run(app_path: str = _APP_PATH) -> None:
    """Bring up the bundled sidecar: prepend bundled tools to PATH, install the hard-exit
    handlers, start the parent-watchdog, and serve the FastAPI app on
    ``127.0.0.1:SIDECAR_PORT``.

    Signal handling is the subtle part. uvicorn captures SIGTERM/SIGINT for a *graceful*
    shutdown, but **only from the main thread**. So we run uvicorn on a worker thread — where it
    installs no handlers — and keep our own hard, non-deadlocking handler on the main thread:
    SIGTERM → ``os._exit(0)`` (contract #3)."""
    port = sidecar_port()
    pid = supervisor_pid()

    # Runtime port authority: the diagnostics endpoint self-reports the API url. In sidecar mode
    # the bound port is the supervisor-assigned ephemeral SIDECAR_PORT, not config.yaml api.port
    # — app.py's diagnostics derives from SIDECAR_PORT when present (spec §1.3 "runtime port
    # authority"), so no config mutation is needed here beyond leaving SIDECAR_PORT in the env.
    # Unified troubleshooting log (spec §S-2.5): usually a no-op here — the shim already installed
    # the handler in this same process — but configure it so uvicorn's output is captured even if
    # this entry is ever reached without the shim. log_config=None disables uvicorn's own handlers
    # so its loggers propagate to the root handler (the file) instead of only the invisible stderr.
    log_path = configure_unified_log()
    prepend_tools_to_path()
    install_sigterm()  # main thread owns SIGTERM/SIGINT → os._exit(0)
    if pid:
        start_parent_watchdog(pid)

    import uvicorn

    # Fresh Server per attempt — a uvicorn.Server can't be re-run once its serve loop has
    # exited, and _serve may retry (bind failure or early exit), so it rebuilds a Server each
    # time. The Config is cheap to rebuild and closes over the fixed supervisor-assigned port.
    log_config = None if log_path is not None else uvicorn.config.LOGGING_CONFIG

    def make_server():
        config = uvicorn.Config(app_path, host=HOST, port=port, log_level="info", log_config=log_config)
        return uvicorn.Server(config)

    print(f"[sidecar] MSA backend on http://{HOST}:{port} (supervisor pid {pid or 'none'})", flush=True)
    _serve(make_server, host=HOST, port=port)


def _bind_reuse_socket(host: str, port: int) -> socket.socket:
    """Create the sidecar's listening socket, bind it, and (via ``_serve``) hand it to uvicorn
    with ``Server.run(sockets=[...])``.

    Belt-and-braces, NOT the first-launch fix: uvicorn's own bind path also sets
    ``SO_REUSEADDR`` (``Config.bind_socket``), and the 2026-07-10 field failure turned out to be
    an app-import crash, not a lost bind — the import now happens in the shim BEFORE the
    responder stops (conductor ledger 2026-07-11; architecture doc §10 risk #6). Owning the bind
    still buys three things: (a) a bind failure surfaces HERE as a real ``OSError`` (WinError
    included), logged to the unified log and retryable, instead of dying invisibly inside
    uvicorn's serve loop; (b) port problems and app problems are cleanly separated, each with
    its own handling in ``_serve``; (c) a well-defined moment to publish the bound port
    (``publish_port``) for external discovery. ``SO_REUSEADDR`` matches uvicorn's own behavior.
    Loopback-only (127.0.0.1), so the Windows "SO_REUSEADDR permits address hijacking" caveat
    doesn't apply."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        sock.bind((host, port))
    except OSError:
        sock.close()
        raise
    return sock


def _serve(
    server_factory: Callable[[], object],
    *,
    host: str = HOST,
    port: int | None = None,
    attempts: int = 5,
    backoff: float = 0.5,
    max_backoff: float = 4.0,
    exit_fn: Callable[[int], None] = os._exit,
    sleep: Callable[[float], None] = time.sleep,
    bind: Callable[[str, int], object] = _bind_reuse_socket,
) -> None:
    """Take the port with an ``SO_REUSEADDR`` socket (see ``_bind_reuse_socket``), hand it to uvicorn
    on a worker thread, and park the main thread on the join loop so it stays responsive to signals
    (our SIGTERM handler runs between join wake-ups; ``os._exit`` tears the process down at once).

    Three failure modes, each handled distinctly:

    1. **bind fails** — the just-freed responder port isn't takeable yet. With ``SO_REUSEADDR`` this
       is now rare, but if it happens we log the *actual* OSError (WinError included) and **retry
       with exponential backoff** — this is the true bind race, finally surfaced instead of hidden
       inside uvicorn on the invisible child stderr.

    2. **the worker thread RAISED** — a genuine startup crash (lifespan/config failure; in the
       desktop flow the app module is pre-loaded by the shim BEFORE the responder stops, so an
       import failure normally surfaces there, in the responder's pollable error state).
       Deterministic — retrying only burns the budget, and the traceback would otherwise escape
       to the invisible child stderr. So we **capture the exception, log the full traceback**
       to the root logger (→ the unified msa-desktop.log) and **fail fast without retrying**.

    3. **uvicorn exited before ready without raising** — a rarer internal early-exit; retry.

    Only after a genuine failure do we exit **non-zero**, so the supervisor sees a real failure, not
    a clean (0) shutdown it would misread as a graceful quit. A normal SIGTERM exits in the handler
    (``os._exit(0)``) and never reaches here; once uvicorn is ready the ``while worker.is_alive()``
    loop parks here for the process's life."""
    where = f" on {host}:{port}" if port is not None else ""
    for attempt in range(1, attempts + 1):
        # (1) Take the port ourselves with SO_REUSEADDR, then hand the bound socket to uvicorn — so
        #     uvicorn never does its own (Windows-exclusive) bind on the just-freed responder port.
        try:
            sock = bind(host, port)
        except OSError as exc:
            if attempt < attempts:
                delay = min(backoff * 2 ** (attempt - 1), max_backoff)
                _log.warning(
                    "could not bind%s (attempt %d/%d): %r — retrying in %.1fs",
                    where, attempt, attempts, exc, delay,
                )
                sleep(delay)
                continue
            _log.error("could not bind%s after %d attempts: %r — startup failure", where, attempts, exc)
            _safe_stderr(f"[sidecar] could not bind {host}:{port}: {exc!r}\n")
            exit_fn(1)
            return

        if port is not None:
            # Publish the bound port to MSA_LOG_DIR/sidecar-port — the authoritative,
            # machine-readable source for external port discovery (issue #172; the desktop BVT
            # validators read this file). uvicorn skips its own "Uvicorn running on …" banner on
            # the sockets= path, so also log an honest listening line for humans reading
            # msa-desktop.log.
            publish_port(port)
            _log.info("sidecar backend listening on http://%s:%d", host, port)

        server = server_factory()
        crash: dict[str, BaseException] = {}

        def _run(srv=server, s=sock, box=crash) -> None:
            # Capture whatever the worker raises — else an app-import/startup crash escapes to the
            # child's invisible stderr and _serve can only report the generic "exited before ready".
            try:
                srv.run(sockets=[s])
            except BaseException as exc:  # noqa: BLE001 — re-surfaced below via the root logger
                box["exc"] = exc

        worker = threading.Thread(target=_run, name="uvicorn", daemon=True)
        worker.start()
        while worker.is_alive():
            worker.join(timeout=1.0)
        if getattr(server, "started", False):
            return  # became ready (and only later exited) — a genuine shutdown, nothing to retry

        try:
            sock.close()  # release before another attempt (uvicorn may not have on early-exit)
        except OSError:
            pass

        exc = crash.get("exc")
        if exc is not None:
            # (2) Deterministic startup crash — retrying can't help; surface the traceback and die.
            _log.error(
                "uvicorn worker crashed during startup%s — not a bind race; not retrying",
                where, exc_info=exc,
            )
            _safe_stderr(f"[sidecar] uvicorn worker crashed during startup: {exc!r}\n")
            exit_fn(1)
            return

        # (3) uvicorn early-exited without raising and without becoming ready → retry.
        if attempt < attempts:
            delay = min(backoff * 2 ** (attempt - 1), max_backoff)
            _log.warning(
                "uvicorn exited before ready%s (attempt %d/%d) — retrying in %.1fs",
                where, attempt, attempts, delay,
            )
            sleep(delay)
            continue
        _log.error(
            "uvicorn exited before becoming ready%s after %d attempts — startup failure",
            where, attempts,
        )
        _safe_stderr("[sidecar] uvicorn exited before becoming ready - startup failure\n")
        exit_fn(1)
