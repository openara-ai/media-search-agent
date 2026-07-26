"""
Indexer process manager — singleton that owns the indexer subprocess lifecycle.

Only one indexer run is allowed at a time.

State is persisted to run/indexer.pid so that:
- stop.sh can kill the indexer even if the API was restarted
- The API can detect a running indexer after its own restart
"""
import os
import signal
import sys
import threading
import uuid
import json
import re
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from loguru import logger

from msa_query.storage.db import connect_readonly

# Matches any ANSI/VT escape sequence (color codes, cursor movement, etc.)
_ANSI_ESCAPE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")

# PID file lives in the log directory (MSA_LOG_DIR) so it stays in app-internal
# space alongside uvicorn.log rather than polluting the user data directory.
# Falls back to CWD/run if the env var is not set (dev mode).
#
# NOTE (M-8/S-2, round-5): _RUN_DIR holds only the indexer pid/stop/started
# sentinels. The qdrant.request/qdrant.granted handshake files use the
# config-anchored _handoff_run_dir() below instead — their dir must be
# cwd-independent because a standalone `msa index export --config ...` in a
# different working directory has to land on the same slot. The pid/stop
# files stay on this legacy derivation: their external consumers (stop.sh,
# `msa index stop/status`) resolve the same MSA_LOG_DIR/cwd rule
# independently, so moving them is not a drop-in change.
_LOG_DIR = os.getenv("MSA_LOG_DIR")
_RUN_DIR = Path(_LOG_DIR) / "run" if _LOG_DIR else Path(os.getcwd()) / "run"
_INDEXER_PID_FILE = _RUN_DIR / "indexer.pid"
# Cooperative-stop sentinel. Created by stop() and watched by the indexer
# subprocess so we never have to rely on CTRL_BREAK_EVENT on Windows, where
# Intel Fortran runtime (linked via NumPy/SciPy/sklearn) installs its own
# console-control handler that aborts the process before Python's signal
# handler can run. Removed by _monitor() after the process exits.
_INDEXER_STOP_FILE = _RUN_DIR / "indexer.stop"
# Real wall-clock start time of the detached indexer, persisted alongside the PID so a
# re-attach after an app restart restores a CONTINUOUS elapsed timer (#169). Without it,
# _restore_from_pid_file could only approximate started_at as "now", so get_status()
# reported elapsed_seconds counting from the restart, not the true run start.
_INDEXER_STARTED_FILE = _RUN_DIR / "indexer.started"
# Poll interval of the M-8/S-2 Qdrant handoff watcher (a stat of two sentinel
# files in app-private space — negligible). Module-level so tests can shrink it.
_HANDOFF_POLL_SECONDS = 0.5
# Reader-drain budget before the watcher closes anyway: reads are safely
# abandonable — they error harmlessly on a closed client — so a wedged READER
# must not block the export window for long.
_HANDOFF_READ_DRAIN_SECONDS = 10.0
# Hard ceiling on waiting for in-flight §4 payload WRITES before the export
# window is granted. Writes are bounded operations (bulk label, person
# merge/rename), so the grant simply queues behind them WITHOUT the reader
# cap — abandoning a write commits SQLite while its Qdrant sync silently
# fails against the closed client. The ceiling exists only so a WEDGED write
# cannot block the export forever; when it fires, the close is loud and the
# write surfaces a retryable 503 (close-generation check in
# _qdrant_payload_write_guard) instead of a 200 over stale payloads.
_HANDOFF_WRITE_DRAIN_CEILING_SECONDS = 120.0


def _write_pid(pid: int, started_at: datetime) -> None:
    _RUN_DIR.mkdir(parents=True, exist_ok=True)
    _INDEXER_PID_FILE.write_text(str(pid))
    # Best-effort: a missing/corrupt started file only degrades the timer back to the
    # old approximate-now behaviour on re-attach; it never blocks a run.
    try:
        _INDEXER_STARTED_FILE.write_text(started_at.isoformat())
    except Exception:
        pass


def _read_started_at() -> Optional[datetime]:
    """Real start time persisted by _write_pid, or None if absent/unparseable."""
    try:
        return datetime.fromisoformat(_INDEXER_STARTED_FILE.read_text().strip())
    except Exception:
        return None


def _clear_pid() -> None:
    for f in (_INDEXER_PID_FILE, _INDEXER_STARTED_FILE):
        try:
            f.unlink(missing_ok=True)
        except Exception:
            pass


def _write_stop_sentinel(run_id: Optional[str]) -> bool:
    """Write the stop sentinel. Returns True on success, False on any failure.

    On Windows the sentinel is the *only* stop mechanism (CTRL_BREAK is
    hijacked by Intel Fortran runtime — see WIN-006). A silent write failure
    would leave the user with a stop request the indexer never observes, so
    the caller must check the return and surface failures.
    """
    try:
        _RUN_DIR.mkdir(parents=True, exist_ok=True)
        _INDEXER_STOP_FILE.write_text(run_id or "")
        return True
    except Exception as exc:
        logger.error("Could not write stop sentinel: {}", exc)
        return False


def _clear_stop_sentinel() -> None:
    try:
        _INDEXER_STOP_FILE.unlink(missing_ok=True)
    except Exception:
        pass


# Liveness check for pids we track (indexer subprocess, handoff requesters).
# The platform-correct implementation — including the Windows
# GetExitCodeProcess idiom (os.kill(pid, 0) is NOT an existence check on
# Windows; see the win32 note in qdrant_handoff) — lives in
# msa_indexer.db.qdrant_handoff so the owner/liveness-aware handshake-file
# helper and this manager share exactly ONE implementation.
from msa_indexer.db.qdrant_handoff import pid_alive as _pid_alive


def _handoff_run_dir() -> Path:
    """Directory holding the M-8/S-2 qdrant.request/qdrant.granted sentinels.

    Delegates to qdrant_handoff.handoff_dir(): env overrides first
    (MSA_QDRANT_HANDOFF_DIR, MSA_LOG_DIR/run), else the ACTIVE CONFIG's
    log_dir/run — cwd-independent, so this API and a standalone
    `msa index export --config ...` launched from different working
    directories agree on ONE slot (round-5 review finding, P2). In the API
    process load_config() is already cached by app startup, so this is a
    cheap lookup. See the _RUN_DIR note above for why the pid/stop
    sentinels do NOT move with it.
    """
    from msa_indexer.db.qdrant_handoff import handoff_dir
    return handoff_dir()


class IndexerManager:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._process: Optional[Any] = None  # subprocess.Popen
        self._stderr_fh: Optional[Any] = None  # log file handle for subprocess stderr
        self._status: str = "idle"
        self._run_id: Optional[str] = None
        self._log_path: Optional[Path] = None
        self._started_at: Optional[datetime] = None
        self._finished_at: Optional[datetime] = None
        self._return_code: Optional[int] = None
        self._summary: Optional[Dict[str, Any]] = None
        self._log_position: int = 0
        self._log_start_position: int = 0
        # Set True when stop() is called so _monitor classifies the resulting
        # exit as "stopped" rather than "error", regardless of the subprocess
        # return code (Windows ML libraries can abort with rc!=0 even after a
        # cooperative shutdown request).
        self._stop_requested: bool = False
        # M-8/S-2 Qdrant handoff watcher thread. Must exist before
        # _restore_from_pid_file below, which may start it (the G9 fix).
        self._handoff_thread: Optional[threading.Thread] = None
        # Signals the watcher loop to exit. Only tests (and a future explicit
        # shutdown path) set it — in production the watcher lives as long as
        # the process, so an export-only `msa index export` is answered even
        # when no run is active.
        self._handoff_stop = threading.Event()

        # Restore state if an indexer was running before this API instance started
        self._restore_from_pid_file()
        # Process-lifetime watcher: an idle API still holds the shared client
        # (and with it the embedded-Qdrant lock), so the handshake must be
        # answerable at ALL times — the documented manual repair
        # `msa index export` runs against an idle API. No-op if the restore
        # path already started it, or when the kill switch is off.
        self._start_handoff_watcher()

    # ── Public API ────────────────────────────────────────────────────────────

    def start(self, config_path: str, venv_bin: str) -> Dict[str, Any]:
        import subprocess

        with self._lock:
            if self._status == "running":
                raise RuntimeError("Indexer is already running")

            run_id = uuid.uuid4().hex[:8]
            try:
                from msa_settings import load_config as _lc
                log_path = Path(_lc(config_path).log_dir) / "msa.log"
            except Exception:
                log_path = Path(config_path).parent / "logs" / "msa.log"

            script_name = "msa.exe" if sys.platform == "win32" else "msa"
            cmd = [
                str(Path(venv_bin) / script_name),
                "index", "run",
                "--config", config_path,
                "--no-console-log",
            ]
            logger.info(f"Starting indexer run {run_id}: {' '.join(cmd)}")

            # M-8/S-2: with the sentinel-file handoff enabled, the API KEEPS
            # its shared Qdrant client through the run — search serves the
            # pre-run index — and releases the lock only for the export
            # window, when the watcher (started below) sees the indexer's
            # qdrant.request. MSA_QDRANT_HANDOFF=off restores the legacy
            # close-at-start behavior on both sides (subprocess inherits it).
            from msa_indexer.db.qdrant_handoff import handoff_enabled
            if handoff_enabled():
                logger.info(
                    "Qdrant handoff enabled — shared client stays open; "
                    "watcher will release the lock for the export window"
                )
            else:
                # Release the embedded Qdrant lock so the indexer subprocess can acquire it
                try:
                    from msa_query.storage.qdrant_client import close_shared_client
                    close_shared_client()
                    logger.info("Qdrant client closed — indexer subprocess will take exclusive access")
                except Exception as exc:
                    logger.warning("Could not close shared Qdrant client: {}", exc)

            # Redirect subprocess stderr to msa.log so crash tracebacks are visible.
            # stdout stays DEVNULL — the indexer uses loguru (not print) for all output.
            stderr_dest: Any = subprocess.DEVNULL
            stderr_fh = None
            try:
                log_path.parent.mkdir(parents=True, exist_ok=True)
                stderr_fh = open(log_path, "a", encoding="utf-8", errors="replace")
                stderr_dest = stderr_fh
            except Exception as exc:
                logger.warning("Could not open log file for indexer stderr: {}", exc)

            # Force UTF-8 for the subprocess's stdio streams so that non-ASCII
            # characters (emojis, accented filenames) are encoded correctly when
            # written to stderr. Without this, Windows defaults to the system
            # codepage (cp1252) and uses backslashreplace, turning 📂 into \U0001f4c2.
            sub_env = os.environ.copy()
            sub_env["PYTHONIOENCODING"] = "utf-8"
            # Tell the indexer where to watch for a cooperative-stop sentinel.
            sub_env["MSA_INDEXER_STOP_FILE"] = str(_INDEXER_STOP_FILE)
            # Point the indexer's Qdrant handshake at OUR resolved handoff
            # dir (config-anchored, round-5) so both sides agree even if the
            # subprocess derives cwd/MSA_LOG_DIR differently.
            handoff_run_dir = _handoff_run_dir()
            sub_env["MSA_QDRANT_HANDOFF_DIR"] = str(handoff_run_dir)

            # Clear any leftover sentinel from a previous run and reset the
            # stop-requested flag so this run starts clean.
            _clear_stop_sentinel()
            self._stop_requested = False
            # Same hygiene for leftover handshake files: a stale qdrant.request
            # would make the fresh watcher grant a window nobody asked for.
            # Owner-aware (round-3, P2): only dead-owner leftovers are swept —
            # a LIVE foreign request (a standalone `msa index export` mid-
            # handshake) is preserved; the watcher keeps serving that window,
            # and the spawned run's own request hours later serializes behind
            # it via the indexer-side slot wait.
            try:
                from msa_indexer.db import qdrant_handoff as _qdrant_handoff
                _qdrant_handoff.cleanup_stale(handoff_run_dir, pid_alive_fn=_pid_alive)
            except Exception:
                pass

            popen_kwargs: Dict[str, Any] = {
                "stdout": subprocess.DEVNULL,
                "stderr": stderr_dest,
                "env": sub_env,
            }
            if sys.platform == "win32":
                # CREATE_NEW_PROCESS_GROUP isolates the indexer from console-
                # control events targeted at the API. We no longer send
                # CTRL_BREAK_EVENT to stop it — the sentinel file is the stop
                # path (see stop() and WIN-006 in BUGS_AND_GOTCHAS.md). The
                # flag still matters in dev mode: without it, Ctrl-C in the
                # API's terminal broadcasts CTRL_C_EVENT to every process in
                # the console group, including the indexer, where Intel
                # Fortran's console-control handler would abort the process
                # with "forrtl: error (200)" — the exact failure the sentinel
                # path was designed to avoid.
                popen_kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
            else:
                popen_kwargs["start_new_session"] = True

            # Record the log file offset BEFORE Popen so that any immediate
            # import errors / dyld noise written to stderr by the subprocess are
            # within this run's window and visible in get_log_lines().
            try:
                if log_path.exists():
                    with open(log_path, "r", errors="replace") as _f:
                        _f.seek(0, 2)
                        self._log_start_position = _f.tell()
                else:
                    self._log_start_position = 0
            except Exception:
                self._log_start_position = 0
            self._log_position = self._log_start_position

            self._process = subprocess.Popen(cmd, **popen_kwargs)
            self._stderr_fh = stderr_fh
            self._started_at = datetime.now()
            _write_pid(self._process.pid, self._started_at)

            self._status = "running"
            self._run_id = run_id
            self._log_path = log_path
            self._finished_at = None
            self._return_code = None
            self._summary = {"phase": "counting"}

        t = threading.Thread(target=self._monitor, daemon=True)
        t.start()
        self._start_handoff_watcher()

        return {"run_id": run_id, "status": "running", "log_path": str(log_path)}

    def stop(self) -> Dict[str, Any]:
        with self._lock:
            if self._status != "running":
                return {"status": self._status, "message": "Indexer is not running"}

            # If the subprocess has already exited but _monitor hasn't observed
            # it yet, do NOT flip _stop_requested. Otherwise a late stop arriving
            # in the race window between exit and classification would overwrite
            # a real crash (rc!=0) with a user-stop label, hiding the failure.
            # Let _monitor classify based on the actual return code.
            pid = self._process.pid if self._process else self._read_pid_file()
            if pid and not _pid_alive(pid):
                logger.info(
                    "stop() called but indexer PID {} is already dead — "
                    "deferring to _monitor's rc-based classification",
                    pid,
                )
                return {"status": self._status, "message": "Indexer already exited"}

            # Write the cooperative-stop sentinel BEFORE flipping intent, so a
            # write failure leaves the system in a consistent state (no flag set,
            # no sentinel, no claim of "stopping").
            #
            # On Windows this is the only stop path — if it fails the indexer
            # will never observe the stop request, so surface the failure to
            # the caller. On POSIX the SIGTERM below would still work, but it's
            # simpler to fail loudly than to half-stop.
            if not _write_stop_sentinel(self._run_id):
                return {
                    "status": "error",
                    "detail": (
                        "Could not write stop sentinel. The indexer was not "
                        "asked to stop. Check disk space and permissions on "
                        f"{_RUN_DIR}, then try again."
                    ),
                }
            self._stop_requested = True
            logger.info(f"Wrote stop sentinel for indexer run {self._run_id}")

            # On POSIX, send SIGTERM as well for fast shutdown — the indexer's
            # SIGTERM handler does the same thing as the sentinel watcher.
            #
            # On Windows we deliberately do NOT send CTRL_BREAK_EVENT: when the
            # indexer has Intel Fortran runtime loaded (NumPy/SciPy/sklearn via
            # PySceneDetect, CLIP, FaceNet, etc.), the runtime installs its own
            # console-control handler that aborts the process before Python's
            # signal handler can run. The sentinel above is the Windows path.
            if pid and sys.platform != "win32":
                try:
                    os.killpg(os.getpgid(pid), signal.SIGTERM)
                    logger.info(f"Sent SIGTERM to indexer PID {pid} (run {self._run_id})")
                except (ProcessLookupError, OSError):
                    pass
            # Do NOT clear the PID file here — _monitor/_monitor_pid does it
            # once the process has actually exited. Clearing early would prevent
            # API restart from restoring state during graceful shutdown, and
            # would allow a second indexer to start before the first has stopped.
            return {"status": "stopping", "run_id": self._run_id}

    def get_status(self) -> Dict[str, Any]:
        with self._lock:
            # If we think it's running, verify the process is still alive
            if self._status == "running":
                self._refresh_summary_from_log_locked()
                pid = self._process.pid if self._process else self._read_pid_file()
                if pid and not _pid_alive(pid):
                    # Process died without our monitor catching it (e.g. after
                    # API restart, or status polled in the window between exit
                    # and _monitor_pid running). Respect the stop-requested
                    # contract here so a user stop never reports as "complete".
                    self._status = "stopped" if self._stop_requested else "complete"
                    self._finished_at = datetime.now()
                    _clear_pid()
                    _clear_stop_sentinel()

            elapsed: Optional[int] = None
            if self._started_at:
                end = self._finished_at or datetime.now()
                elapsed = int((end - self._started_at).total_seconds())

            return {
                "status": self._status,
                "run_id": self._run_id,
                "log_path": str(self._log_path) if self._log_path else None,
                "started_at": self._started_at.isoformat() if self._started_at else None,
                "finished_at": self._finished_at.isoformat() if self._finished_at else None,
                "elapsed_seconds": elapsed,
                "return_code": self._return_code,
                "summary": self._summary,
            }

    def get_log_lines(self, tail: int = 100) -> List[str]:
        """Return the last `tail` lines of msa.log written since this run started."""
        with self._lock:
            log_path = self._log_path
            start_pos = self._log_start_position
        if not log_path or not log_path.exists():
            return []
        try:
            with open(log_path, "r", errors="replace") as f:
                if start_pos > 0:
                    try:
                        f.seek(start_pos)
                    except Exception:
                        pass
                lines = f.readlines()
            return [_ANSI_ESCAPE.sub("", l).rstrip() for l in lines[-tail:]]
        except Exception:
            return []

    # ── Internal ──────────────────────────────────────────────────────────────

    def _monitor(self) -> None:
        proc = self._process
        if proc is None:
            return
        rc = proc.wait()
        _clear_pid()
        _clear_stop_sentinel()
        # Close the stderr file handle now that the subprocess has exited.
        fh = self._stderr_fh
        self._stderr_fh = None
        if fh is not None:
            try:
                fh.close()
            except Exception:
                pass
        with self._lock:
            self._refresh_summary_from_log_locked()
            self._return_code = rc
            self._finished_at = datetime.now()
            # If the user pressed Stop, call it "stopped" even on non-zero
            # exit — they asked for it, it's not a crash from their POV.
            if self._stop_requested:
                self._status = "stopped"
            elif rc == 0:
                self._status = "complete"
            else:
                self._status = "error"
            logger.info(
                f"Indexer run {self._run_id} finished — "
                f"status={self._status} rc={rc}"
            )
        try:
            from msa_indexer.db import qdrant_handoff as _ho
            from msa_query.storage.qdrant_client import reopen_shared_client
            if _ho.handoff_enabled() and _ho.window_active(
                _handoff_run_dir(), pid_alive_fn=_pid_alive
            ):
                # Round-6 (P2): a standalone exporter can already own the
                # SUCCESSOR window (back-to-back grant) by the time the
                # spawned indexer's exit lands here — its slot claim beats
                # this thread whenever the indexer's teardown lag exceeds
                # one claim+grant cycle. Reopening now would flip _blocked
                # off mid-window: payload writes would bypass their §4 503
                # and commit SQLite over a silently failed sync, and the
                # API would race the granted exporter for the embedded
                # lock. The watcher owns reopening — it reopens when the
                # live window's request disappears or its owner dies. This
                # on-exit reopen remains only as the crash net for the
                # no-live-window case (window_active is False for absent
                # requests and dead owners).
                logger.info(
                    "Skipping Qdrant reopen after indexer exit — a live "
                    "handoff window is active (the watcher reopens on its release)"
                )
            else:
                reopen_shared_client()
                logger.info("Qdrant client reopened after indexer subprocess exit")
        except Exception as exc:
            logger.warning("Could not reopen shared Qdrant client: {}", exc)
        if rc == 0:
            try:
                from msa_apps.search_api.deps import reset_query_engine
                reset_query_engine()
                logger.info("Query engine reset after indexer run (rc=0)")
            except Exception as exc:
                logger.warning("Could not reset query engine: {}", exc)

    # ── M-8/S-2: Qdrant handoff watcher ──────────────────────────────────────

    def _start_handoff_watcher(self) -> None:
        """Start the sentinel-file handoff watcher (process lifetime).

        Called from __init__, start(), and _restore_from_pid_file() —
        idempotent, so exactly one watcher runs per manager. The start()/
        re-attach call sites are defensive restarts (the G9 fix keeps fresh
        start and re-attach on the same lock-window contract; __init__ covers
        the idle case, where `msa index export` must still be granted the
        window). No-op when the kill switch is off (legacy close-at-start
        already released the lock for the whole run).
        """
        from msa_indexer.db.qdrant_handoff import handoff_enabled
        if not handoff_enabled():
            return
        if self._handoff_thread is not None and self._handoff_thread.is_alive():
            return
        t = threading.Thread(target=self._handoff_watcher_loop, daemon=True)
        self._handoff_thread = t
        t.start()

    def _handoff_watcher_loop(self) -> None:
        """Poll the run dir for a qdrant.request for the process lifetime.

        Covers requests from a spawned run, a re-attached run, AND an
        export-only `msa index export` while the API is idle — the idle API
        still holds the shared client, so a request arriving between runs
        must be granted too (the manual-repair path the pipeline's own
        export_blocked message recommends).

        On request: block new shared-client ops, wait for in-flight payload
        WRITES (no reader cap, generous hard ceiling — abandoning a write
        is never silent-safe), drain in-flight reads (bounded), close the
        client, write qdrant.granted echoing the request's run_id. On request disappearance: reopen + reset the query
        engine so the fresh client sees the new collections. Fail-safes: a
        request/window whose recorded pid is dead is cleaned up and the
        client reopened (indexer crash mid-export), and loop exit while a
        window is open reopens too. _monitor's on-exit reopen stays as the
        outer crash net for spawned runs.
        """
        from msa_indexer.db import qdrant_handoff as ho
        from msa_query.storage.qdrant_client import (
            block_shared_client,
            close_shared_client,
            drain,
            drain_writes,
            reopen_shared_client,
        )

        def _reopen_and_reset() -> None:
            reopen_shared_client()
            try:
                from msa_apps.search_api.deps import reset_query_engine
                reset_query_engine()
            except Exception as exc:
                logger.warning("Could not reset query engine after Qdrant handoff: {}", exc)

        def _request_pid(req: Dict[str, Any]) -> Optional[int]:
            try:
                return int(req.get("pid"))
            except (TypeError, ValueError):
                return None

        # Snapshot the handoff dir (config-anchored, round-5): a watcher must
        # keep answering the dir it was born with even if env/config were
        # later re-pointed (tests do this per-case; production never does).
        run_dir = _handoff_run_dir()
        granted_run_id: Optional[str] = None
        try:
            while not self._handoff_stop.is_set():
                req = ho.read_request(run_dir)
                if granted_run_id is None:
                    if req is not None:
                        req_pid = _request_pid(req)
                        if req_pid is not None and not _pid_alive(req_pid):
                            # Fail-safe: the requester died before we granted.
                            # cleanup_stale re-verifies ownership before any
                            # file is removed (round-3 lifecycle rule).
                            logger.warning(
                                "Qdrant handoff request from dead PID {} — cleaning up",
                                req_pid,
                            )
                            ho.cleanup_stale(run_dir, pid_alive_fn=_pid_alive)
                            reopen_shared_client()
                        else:
                            run_id = str(req.get("run_id"))
                            logger.info(
                                "Qdrant handoff: request received (run_id={}) — "
                                "draining in-flight operations and closing shared client",
                                run_id,
                            )
                            block_shared_client()
                            # Payload WRITES first, without the reader cap:
                            # abandoning one commits SQLite over a silently
                            # failed Qdrant sync. The generous ceiling only
                            # fires on a wedged write — then the close is
                            # loud and the write 503s via the guard's
                            # close-generation check instead of 200ing.
                            if not drain_writes(_HANDOFF_WRITE_DRAIN_CEILING_SECONDS):
                                logger.error(
                                    "Qdrant handoff: a payload write is still in "
                                    "flight after the {}s ceiling — closing anyway; "
                                    "that write will fail with a retryable 503 "
                                    "rather than commit silently-stale Qdrant state",
                                    _HANDOFF_WRITE_DRAIN_CEILING_SECONDS,
                                )
                            if not drain(_HANDOFF_READ_DRAIN_SECONDS):
                                logger.warning(
                                    "Qdrant handoff: reader drain timed out with "
                                    "operations in flight — closing anyway "
                                    "(abandoned reads error harmlessly; the "
                                    "indexer's lock retries absorb the residual race)"
                                )
                            close_shared_client()
                            try:
                                ho.write_grant(run_id, run_dir)
                                granted_run_id = run_id
                                logger.info("Qdrant handoff: granted (run_id={})", run_id)
                            except Exception as exc:
                                logger.warning(
                                    "Qdrant handoff: could not write grant ({}) — reopening",
                                    exc,
                                )
                                _reopen_and_reset()
                else:
                    if req is None:
                        # Indexer released the window (finally-cleanup).
                        # cleanup_stale sweeps the now-orphaned grant (grants
                        # are run_id-bound; without a request none is valid).
                        ho.cleanup_stale(run_dir, pid_alive_fn=_pid_alive)
                        _reopen_and_reset()
                        granted_run_id = None
                        logger.info(
                            "Qdrant handoff: released — shared client reopened, "
                            "query engine reset"
                        )
                    else:
                        req_pid = _request_pid(req)
                        if req_pid is not None and not _pid_alive(req_pid):
                            # Fail-safe: indexer crashed mid-export without
                            # running its finally-cleanup.
                            logger.warning(
                                "Qdrant handoff window owner PID {} died — "
                                "cleaning up and reopening",
                                req_pid,
                            )
                            ho.cleanup_stale(run_dir, pid_alive_fn=_pid_alive)
                            _reopen_and_reset()
                            granted_run_id = None
                        elif str(req.get("run_id")) != granted_run_id:
                            # Back-to-back windows: the granted requester
                            # released and a NEW live requester took the slot
                            # within one poll tick. The client is already
                            # closed/blocked, so grant the new window
                            # directly; the single reopen+reset after ITS
                            # release covers both exports.
                            new_run_id = str(req.get("run_id"))
                            logger.info(
                                "Qdrant handoff: window {} replaced by new "
                                "request {} — granting successor",
                                granted_run_id, new_run_id,
                            )
                            try:
                                ho.write_grant(new_run_id, run_dir)
                                granted_run_id = new_run_id
                            except Exception as exc:
                                logger.warning(
                                    "Qdrant handoff: could not grant successor "
                                    "window ({}) — cleaning up and reopening",
                                    exc,
                                )
                                ho.cleanup_stale(run_dir, pid_alive_fn=_pid_alive)
                                _reopen_and_reset()
                                granted_run_id = None
                self._handoff_stop.wait(_HANDOFF_POLL_SECONDS)
        finally:
            if granted_run_id is not None:
                # Watcher stopping (or crashing) while a window is open: clean
                # up so the API is never wedged behind a DEAD window — but
                # never clobber a LIVE one (round-3, P2): cleanup_stale only
                # removes the files when their owner is dead (or ourselves),
                # and the shared client is reopened only in that case. A live
                # foreign exporter keeps its window; reopening under it would
                # be exactly the lock race the handshake exists to prevent.
                if ho.cleanup_stale(run_dir, pid_alive_fn=_pid_alive):
                    _reopen_and_reset()
                    logger.info(
                        "Qdrant handoff: watcher exiting with open window — "
                        "cleaned up and reopened shared client"
                    )
                else:
                    logger.error(
                        "Qdrant handoff: watcher exiting while a LIVE export "
                        "window is open — handshake files preserved for their "
                        "owner; shared client stays blocked"
                    )

    @staticmethod
    def _pid_is_indexer(pid: int) -> bool:
        """Return True only if the process cmdline looks like the MSA indexer.

        Uses /proc/<pid>/cmdline on Linux. On macOS (no /proc) falls back to
        `ps -o args=` so the check stays cross-platform. Returns True if the
        cmdline cannot be determined (fail-open: trust the PID file on platforms
        where we have no way to verify ownership).
        """
        import platform
        try:
            if platform.system() == "Linux":
                with open(f"/proc/{pid}/cmdline", "rb") as f:
                    cmdline = f.read().replace(b"\x00", b" ").decode(errors="replace")
            elif platform.system() == "Windows":
                import subprocess
                result = subprocess.run(
                    [
                        "powershell", "-NoProfile", "-NonInteractive", "-Command",
                        f"(Get-CimInstance Win32_Process -Filter 'ProcessId={pid}').CommandLine",
                    ],
                    capture_output=True, text=True, timeout=5,
                )
                cmdline = result.stdout
            else:
                import subprocess
                result = subprocess.run(
                    ["ps", "-p", str(pid), "-o", "args="],
                    capture_output=True, text=True, timeout=2,
                )
                cmdline = result.stdout
            # Match installed console-script form: ".../msa index run ..."
            # and legacy/dev forms: python -m msa_indexer.cli, msa-index, msa_cli
            is_msa_index_run = (
                ("msa" in cmdline and "index" in cmdline and "run" in cmdline)
                or "msa_indexer" in cmdline
                or "msa-index" in cmdline
                or "msa_cli" in cmdline
            )
            return is_msa_index_run
        except FileNotFoundError:
            # /proc not available and ps not found — fail open
            return True
        except Exception:
            return True

    def _restore_from_pid_file(self) -> None:
        """On startup, check if an indexer process is still running from a prior session."""
        pid = self._read_pid_file()
        if pid and _pid_alive(pid) and self._pid_is_indexer(pid):
            logger.info(f"Detected running indexer from previous session (PID {pid})")
            self._status = "running"
            self._run_id = "restored"
            self._summary = {"phase": "counting"}
            # If a stop sentinel survived an API restart, preserve the intent
            # so we classify the eventual exit as "stopped" rather than "complete".
            if _INDEXER_STOP_FILE.exists():
                self._stop_requested = True
            try:
                from msa_settings import load_config as _lc
                self._log_path = Path(_lc().log_dir) / "msa.log"
            except Exception:
                self._log_path = Path(os.getcwd()) / "logs" / "msa.log"
            # Restore the REAL start time persisted at launch so the elapsed timer stays
            # continuous across the app restart (#169); fall back to now only if the
            # started file is missing/corrupt (older runs, or a write that failed).
            self._started_at = _read_started_at() or datetime.now()
            # Seek to current file end so get_log_lines() shows only content
            # written after this API restart — avoids historical noise from prior
            # runs. Pre-restart indexer output is still readable in msa.log directly.
            try:
                log_path = self._log_path
                if log_path and log_path.exists():
                    with open(log_path, "r", errors="replace") as _f:
                        _f.seek(0, 2)
                        self._log_start_position = _f.tell()
                else:
                    self._log_start_position = 0
            except Exception:
                self._log_start_position = 0
            self._log_position = self._log_start_position
            # Attach a monitor thread so we notice when it finishes
            t = threading.Thread(target=self._monitor_pid, args=(pid,), daemon=True)
            t.start()
            # M-8/S-2 G9 fix: re-attach starts the SAME handoff watcher as a
            # fresh start, so the restored run's export window is granted and
            # a search can no longer steal the embedded lock mid-run.
            self._start_handoff_watcher()
        else:
            _clear_pid()
            _clear_stop_sentinel()
            # M-8/S-2 startup hygiene: no live indexer pid, so DEAD-owner
            # handshake leftovers from a crashed run must not wedge anything
            # (L4). Owner-aware (round-3, P2): "no indexer.pid" does NOT mean
            # "no handshake owner" — a standalone `msa index export` writes a
            # qdrant.request with no pid file. cleanup_stale preserves a
            # request whose recorded owner is alive; we then treat it exactly
            # like a fresh qdrant.request arrival: block the shared client
            # now (no search may construct one over the exporter's window)
            # and let the process-lifetime watcher started right after this
            # restore drain/grant it and reopen on release.
            slot_free = True
            try:
                from msa_indexer.db import qdrant_handoff as _qdrant_handoff
                slot_free = _qdrant_handoff.cleanup_stale(
                    _handoff_run_dir(), pid_alive_fn=_pid_alive
                )
            except Exception:
                pass
            try:
                from msa_indexer.db.qdrant_handoff import handoff_enabled
                from msa_query.storage.qdrant_client import (
                    block_shared_client,
                    reopen_shared_client,
                )
                if not slot_free and handoff_enabled():
                    logger.info(
                        "Live Qdrant handoff request found at startup "
                        "(standalone export in flight) — preserving it; the "
                        "watcher will grant the window and reopen on release"
                    )
                    block_shared_client()
                else:
                    # Slot free (or kill switch off, where no watcher would
                    # ever serve the window): the shared client must be
                    # available.
                    reopen_shared_client()
            except Exception:
                pass

    def _monitor_pid(self, pid: int) -> None:
        """Monitor a PID we don't own a Popen handle for (restored from pid file)."""
        import time
        while _pid_alive(pid):
            with self._lock:
                self._refresh_summary_from_log_locked()
            time.sleep(2)
        _clear_pid()
        _clear_stop_sentinel()
        with self._lock:
            self._refresh_summary_from_log_locked()
            self._finished_at = datetime.now()
            # Without a Popen handle we can't read the real exit code on POSIX
            # (waitpid only works for direct children). If stop was requested
            # via this API instance, record that intent; otherwise assume the
            # restored run completed on its own.
            self._status = "stopped" if self._stop_requested else "complete"
            logger.info(f"Restored indexer PID {pid} has finished — status={self._status}")

    @staticmethod
    def _read_pid_file() -> Optional[int]:
        try:
            return int(_INDEXER_PID_FILE.read_text().strip())
        except Exception:
            return None

    def _refresh_summary_from_log_locked(self) -> None:
        log_path = self._log_path
        if not log_path or not log_path.exists():
            return
        try:
            with open(log_path, "r", errors="replace") as fh:
                fh.seek(self._log_position)
                for raw_line in fh:
                    line = _ANSI_ESCAPE.sub("", raw_line)
                    marker = line.find("INDEXER_SUMMARY ")
                    if marker == -1:
                        continue
                    payload_text = line[marker + len("INDEXER_SUMMARY "):].strip()
                    try:
                        payload = json.loads(payload_text)
                    except json.JSONDecodeError:
                        continue
                    if isinstance(payload, dict):
                        self._summary = payload
                self._log_position = fh.tell()
        except Exception:
            pass


# ── Stats helper ──────────────────────────────────────────────────────────────

def get_index_stats(sqlite_path: str) -> Dict[str, int]:
    """Query SQLite for current index totals. Returns zeros on any error.

    The connection uses URI mode=ro so a missing or misconfigured DB path
    raises ``sqlite3.OperationalError`` rather than silently creating an
    empty file — the surrounding try/except converts that to the zeros
    response below, which is the right behavior for a stats endpoint.
    """
    conn = None
    try:
        conn = connect_readonly(sqlite_path)
        cur = conn.cursor()
        images = cur.execute(
            "SELECT COUNT(*) FROM media WHERE mime LIKE 'image/%' AND (deleted IS NULL OR deleted = 0)"
        ).fetchone()[0]
        videos = cur.execute(
            "SELECT COUNT(*) FROM media WHERE mime LIKE 'video/%' AND (deleted IS NULL OR deleted = 0)"
        ).fetchone()[0]
        total_video_duration = cur.execute(
            "SELECT COALESCE(SUM(duration), 0) FROM media WHERE mime LIKE 'video/%' AND (deleted IS NULL OR deleted = 0)"
        ).fetchone()[0]
        faces = cur.execute("SELECT COUNT(*) FROM face").fetchone()[0]
        people = cur.execute(
            "SELECT COUNT(*) FROM person WHERE is_labeled = 1"
        ).fetchone()[0]
        last_indexed_at = cur.execute(
            "SELECT MAX(added_at) FROM media"
        ).fetchone()[0]
        return {
            "images": images,
            "videos": videos,
            "total_video_duration": total_video_duration,
            "faces": faces,
            "people": people,
            "last_indexed_at": last_indexed_at,
        }
    except Exception as e:
        logger.warning(f"Could not read index stats: {e}")
        return {"images": 0, "videos": 0, "total_video_duration": 0, "faces": 0, "people": 0, "last_indexed_at": None}
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass


# ── Module-level singleton ────────────────────────────────────────────────────

indexer_manager = IndexerManager()
