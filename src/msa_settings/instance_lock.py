"""
Single-instance enforcement via a PID lock file.

acquire_instance_lock(path, name):
  - If the lock file exists and the PID inside is alive → log error and raise SystemExit.
  - If the lock file exists but the PID is dead → remove stale lock and continue.
  - Otherwise → write our PID and return.

release_instance_lock(path):
  - Remove the lock file if it contains our PID.
"""
import os
import sys
import logging
from pathlib import Path

logger = logging.getLogger(__name__)


def _pid_alive(pid: int) -> bool:
    """Return True if the process with the given PID is alive.

    Uses os.kill(pid, 0) on POSIX (safe — signal 0 never kills).
    On Windows, uses OpenProcess / GetExitCodeProcess via ctypes to avoid
    the documented risk of os.kill taking the terminate-process path.
    """
    if sys.platform == "win32":
        import ctypes
        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        handle = ctypes.windll.kernel32.OpenProcess(
            PROCESS_QUERY_LIMITED_INFORMATION, False, pid
        )
        if not handle:
            return False
        exit_code = ctypes.c_ulong(0)
        ctypes.windll.kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code))
        ctypes.windll.kernel32.CloseHandle(handle)
        STILL_ACTIVE = 259
        return exit_code.value == STILL_ACTIVE
    else:
        try:
            os.kill(pid, 0)
            return True
        except ProcessLookupError:
            return False
        except PermissionError:
            return True  # Exists but owned by another user


def acquire_instance_lock(lock_path: Path, name: str = "Media Search Agent") -> None:
    """Acquire a PID lock file. Raises SystemExit if another instance is running."""
    lock_path = Path(lock_path)

    if lock_path.exists():
        try:
            existing_pid = int(lock_path.read_text().strip())
        except (ValueError, OSError):
            existing_pid = None

        if existing_pid is not None:
            if _pid_alive(existing_pid):
                raise SystemExit(
                    f"{name} is already running (PID {existing_pid}). "
                    f"Stop the existing instance first, or remove {lock_path} if it is stale."
                )
            else:
                # PID does not exist — stale lock
                logger.warning(
                    "Removing stale instance lock for %s (PID %d no longer exists)",
                    name, existing_pid,
                )
                lock_path.unlink(missing_ok=True)

    lock_path.parent.mkdir(parents=True, exist_ok=True)
    lock_path.write_text(str(os.getpid()))
    logger.info("Instance lock acquired: %s (PID %d)", lock_path, os.getpid())


def release_instance_lock(lock_path: Path) -> None:
    """Release the PID lock file if it contains our PID."""
    lock_path = Path(lock_path)
    try:
        if lock_path.exists():
            stored_pid = int(lock_path.read_text().strip())
            if stored_pid == os.getpid():
                lock_path.unlink()
                logger.info("Instance lock released: %s", lock_path)
            else:
                logger.warning(
                    "Not releasing %s — it belongs to PID %d, not us (%d)",
                    lock_path, stored_pid, os.getpid(),
                )
    except Exception as exc:
        logger.warning("Failed to release instance lock %s: %s", lock_path, exc)
