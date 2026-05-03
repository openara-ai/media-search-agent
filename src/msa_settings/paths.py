"""
Platform-aware path conversion utilities.

Storage vs display (ADR-007):
- config.yaml stores user-native paths (e.g. D:\\Photos on WSL2/Windows).
- SQLite and Qdrant store OS-accessible POSIX paths (e.g. /mnt/d/Photos on WSL2),
  because resolve_for_access() is called at index time before any file I/O.
- display_path() converts stored POSIX paths back to user-native format at API
  response boundaries. Call it when returning paths to the UI or CLI.
- resolve_for_access() converts user-native paths to OS-accessible paths before
  any file I/O. Call it immediately before open/stat/pathlib ops.

No other module should perform path conversion.
"""
import functools as _functools
import re as _re

_WIN_PATH_RE = _re.compile(r'^([A-Za-z]):[/\\](.*)', _re.DOTALL)


@_functools.lru_cache(maxsize=None)
def _is_wsl2() -> bool:
    """Return True when the server process is running inside WSL2."""
    try:
        with open("/proc/version") as _f:
            return "microsoft" in _f.read().lower()
    except OSError:
        return False


# Public alias — import this when platform detection is needed outside this module.
is_wsl2 = _is_wsl2


def _win_to_wsl(path: str) -> str:
    """Convert a Windows path to its WSL2 /mnt/ equivalent.
    Returns the path unchanged if it is not a Windows path."""
    m = _WIN_PATH_RE.match(path)
    if not m:
        return path
    drive = m.group(1).lower()
    rest = m.group(2).replace("\\", "/").strip("/")
    return f"/mnt/{drive}/{rest}" if rest else f"/mnt/{drive}"


def _wsl_to_win(path: str) -> str:
    """Convert a WSL2 /mnt/<drive>/… path to a Windows path.
    Returns the path unchanged if it does not start with /mnt/."""
    if not path.startswith("/mnt/"):
        return path
    parts = path[5:].split("/", 1)  # strip "/mnt/"
    if not parts or len(parts[0]) != 1:
        return path  # not a single-letter drive mount
    drive = parts[0].upper()
    rest = parts[1].replace("/", "\\") if len(parts) > 1 and parts[1] else ""
    return f"{drive}:\\{rest}" if rest else f"{drive}:\\"


def resolve_for_access(path: str) -> str:
    """Convert a stored user-native path to an OS-accessible path.

    Must be called immediately before any file I/O (open, os.stat, pathlib.Path ops)
    on a path that came from config.yaml, SQLite, or Qdrant.
    """
    if _is_wsl2():
        return _win_to_wsl(path)
    # Windows native, macOS, Linux: stored path is already OS-native.
    return path


def display_path(path: str) -> str:
    """Convert a stored path to the user-facing display format.

    Must be called when returning paths to the UI or CLI, and when writing
    file paths into SQLite or Qdrant (store in user-native format).
    """
    if _is_wsl2():
        return _wsl_to_win(path)
    return path
