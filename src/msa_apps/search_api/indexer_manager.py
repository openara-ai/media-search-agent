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
_LOG_DIR = os.getenv("MSA_LOG_DIR")
_RUN_DIR = Path(_LOG_DIR) / "run" if _LOG_DIR else Path(os.getcwd()) / "run"
_INDEXER_PID_FILE = _RUN_DIR / "indexer.pid"
# Cooperative-stop sentinel. Created by stop() and watched by the indexer
# subprocess so we never have to rely on CTRL_BREAK_EVENT on Windows, where
# Intel Fortran runtime (linked via NumPy/SciPy/sklearn) installs its own
# console-control handler that aborts the process before Python's signal
# handler can run. Removed by _monitor() after the process exits.
_INDEXER_STOP_FILE = _RUN_DIR / "indexer.stop"


def _write_pid(pid: int) -> None:
    _RUN_DIR.mkdir(parents=True, exist_ok=True)
    _INDEXER_PID_FILE.write_text(str(pid))


def _clear_pid() -> None:
    try:
        _INDEXER_PID_FILE.unlink(missing_ok=True)
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


if sys.platform == "win32":
    # Windows liveness check. os.kill(pid, 0) is not a POSIX-style existence
    # check on Windows — it calls TerminateProcess via OpenProcess, and as
    # long as any handle to the (already-exited) process is still open (e.g.,
    # the subprocess.Popen that spawned it), OpenProcess succeeds and reports
    # the PID as alive. Use GetExitCodeProcess on a query-only handle and
    # treat anything other than STILL_ACTIVE as dead.
    #
    # ctypes defaults function restype to c_int, but a Windows HANDLE is
    # pointer-sized — on 64-bit Python the handle gets truncated/sign-
    # mangled before we hand it to GetExitCodeProcess/CloseHandle. Declare
    # argtypes/restype explicitly. Use a private WinDLL instance instead of
    # the cached windll.kernel32 proxy so we don't change signatures other
    # code in the process might rely on.
    import ctypes
    from ctypes import wintypes

    _PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    _STILL_ACTIVE = 259
    _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    _kernel32.OpenProcess.restype = wintypes.HANDLE
    _kernel32.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
    _kernel32.GetExitCodeProcess.restype = wintypes.BOOL
    _kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    _kernel32.CloseHandle.restype = wintypes.BOOL


def _pid_alive(pid: int) -> bool:
    if sys.platform == "win32":
        handle = _kernel32.OpenProcess(_PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not handle:
            return False
        try:
            exit_code = wintypes.DWORD()
            if not _kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code)):
                return False
            return exit_code.value == _STILL_ACTIVE
        finally:
            _kernel32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
        return True
    except (ProcessLookupError, PermissionError):
        return False


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

        # Restore state if an indexer was running before this API instance started
        self._restore_from_pid_file()

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

            # Clear any leftover sentinel from a previous run and reset the
            # stop-requested flag so this run starts clean.
            _clear_stop_sentinel()
            self._stop_requested = False

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
            _write_pid(self._process.pid)

            self._status = "running"
            self._run_id = run_id
            self._log_path = log_path
            self._started_at = datetime.now()
            self._finished_at = None
            self._return_code = None
            self._summary = {"phase": "counting"}

        t = threading.Thread(target=self._monitor, daemon=True)
        t.start()

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
            from msa_query.storage.qdrant_client import reopen_shared_client
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
            self._started_at = datetime.now()  # approximate; real start time unknown
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
        else:
            _clear_pid()
            _clear_stop_sentinel()

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
