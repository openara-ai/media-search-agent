# ADR-007: Unified Path Experience Across Platforms

Status: Accepted

## Context

The app runs on three distinct runtime environments:

| Environment | Example source path (user sees) | File access layer |
|---|---|---|
| Windows + WSL2 | `D:\Photos\2024` | Linux kernel via `/mnt/d/Photos/2024` |
| macOS (native) | `/Users/alice/Photos/2024` | macOS VFS, no translation |
| Linux (native) | `/home/alice/Photos/2024` | Linux VFS, no translation |
| Windows native (planned) | `D:\Photos\2024` | Win32 API, no translation |

Before this ADR, the app stored WSL2 mount paths (`/mnt/d/Photos`) in `config.yaml`
and SQLite. Windows users were forced to learn and type WSL2 path syntax everywhere:
in settings, in search results, in diagnostics. This is confusing and error-prone for
users who think in Windows paths.

## Decision

### 1. Store paths in user-native format at config and display boundaries

`config.yaml` stores source paths in user-native format:

- Windows / WSL2 users → `D:\Photos`
- macOS / Linux users → `/Users/alice/Photos`

**SQLite and Qdrant store POSIX paths internally** (e.g. `/mnt/d/Photos/photo.jpg`
on WSL2). Python's pathlib treats backslash as a plain character on Linux, so
Windows paths stored in SQLite would be mangled when code calls `Path(...).resolve()`.
Converting at the API response boundary is safer and keeps internal code simple.

API responses convert POSIX paths to user-native format via `display_path()` before
returning them to the UI.

### 2. Single conversion layer in `msa_settings`

A module-level utility in `src/msa_settings/paths.py` (or equivalent) owns all
path translation. No other module performs path conversion independently.

```python
def resolve_for_access(path: str) -> str:
    """Convert a stored user-native path to an OS-accessible path.

    Called immediately before any file I/O (open, os.stat, pathlib.Path ops).
    Must NOT be called on paths that are already OS-native for the current runtime.
    """
    if _is_wsl2():
        return _win_to_wsl(path)   # D:\Photos → /mnt/d/Photos (no-op if already /mnt/)
    # Windows native, macOS, Linux: stored path is already OS-native
    return path


def display_path(path: str) -> str:
    """Convert a stored path to the user-facing display format.

    Called when returning paths to the UI or CLI. On WSL2, converts legacy
    /mnt/ paths that pre-date this ADR to Windows format automatically.
    """
    if _is_wsl2():
        return _wsl_to_win(path)   # /mnt/d/Photos → D:\Photos (no-op if already D:\)
    return path
```

### 3. Platform detection

Detection is done once, lazily, and cached:

| Platform | Detection method |
|---|---|
| WSL2 | `/proc/version` contains `"microsoft"` (case-insensitive) |
| Windows native | `sys.platform == "win32"` |
| macOS | `sys.platform == "darwin"` |
| Linux | fallback |

WSL2 detection takes priority over Linux because WSL2 reports `sys.platform == "linux"`
but requires path translation.

### 4. Conversion rules

**Windows ↔ WSL2:**

```
D:\Photos\2024\photo.jpg   →   /mnt/d/Photos/2024/photo.jpg
                           ←
```

Rules:
- Drive letter lowercased and prepended with `/mnt/`
- Backslashes replaced with forward slashes
- Forward slashes in Windows paths (`D:/Photos`) also accepted as input
- Single-letter drive only; UNC paths (`\\server\share`) not supported (deferred)

**macOS / Linux / Windows native:** stored path equals access path — `resolve_for_access`
is a no-op.

**Re-index required:** Existing index data (SQLite, Qdrant) stores `/mnt/` paths and
is not forward-compatible. A full re-index is required after upgrading to this version.
No migration code is provided — re-indexing is the upgrade path.

### 5. File picker is platform-aware

The directory picker UI component calls `GET /browse` on the server. The server returns
entries with both the stored format (`display_path`) and the access path (`wsl_path`)
so the UI never needs to perform conversion itself.

On WSL2, the picker opens at `/mnt` which lists Windows drive letters (`C:\`, `D:\`, …).
On macOS/Linux, it opens at the home directory.

The path written to `config.yaml` when the user confirms a selection is always the
`display_path` (user-native format).

### 6. Enforcement

**Rule:** Any code that opens, stats, or otherwise accesses a file from a stored path
must call `resolve_for_access(path)` first. Any code that returns a path to the UI
or CLI must call `display_path(path)`.

**Where this is enforced:**

| Site | Direction | Call |
|---|---|---|
| Indexer scanner (`scanner.py`) — `iter_media()` | storage → OS | `resolve_for_access` |
| Indexer SQLite store — `insert_file()` | OS path → storage | stored as POSIX (no conversion) |
| API media serving — thumbnail and file endpoints | storage → OS | `resolve_for_access` |
| API `GET /config/sources` | storage → UI | `display_path` per source |
| API `POST /config/sources` | UI → storage | path stored as-is (UI sends native format) |
| API `GET /browse` | storage → UI | both formats returned |
| CLI output | storage → terminal | `display_path` |

**Where it is NOT called:**
- Index-to-index operations (FAISS, Qdrant internal paths) — these are not user paths
- Log messages may show either format; consistency is preferred but not enforced

**Code review gate:** Any PR that introduces a new file access site must show the
`resolve_for_access` call. Any PR that introduces a new UI/CLI path display site must
show the `display_path` call. Missing calls are bugs.

## Consequences

- A re-index is required after upgrading. Existing index data is not forward-compatible.
- `config.yaml` becomes human-readable for Windows users — opening it in Notepad shows
  familiar `D:\Photos` paths.
- Adding a new platform (e.g. Android, NAS) requires adding a detection branch in
  `paths.py` and a conversion pair. All other code remains unchanged.
- UNC paths (`\\server\share`) and WSL2 network mounts (`/mnt/wsl/`) are not handled
  by this ADR. They can be added to `paths.py` later without changing this policy.
- `display_path` is idempotent: calling it on an already-converted path is safe.
  `resolve_for_access` is also idempotent for the same reason.
