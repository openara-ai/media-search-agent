# ADR-003: Python Management — Bundle uv, App-Local Venv

Status: Accepted

## Context

The installer must provide Python 3.11+ on target machines without conflicting with
existing Python environments (system Python, Homebrew Python, conda, pyenv, etc.).
Three options were evaluated:

| Option | Description |
|---|---|
| System Python | Use whatever Python is already installed on the machine |
| System-wide install | Install Python via apt, brew, or python.org |
| Bundled manager | Ship a Python version manager with the installer |

## Decision

**Bundle the `uv` binary** in the installer payload. Use `uv python install 3.12` to
obtain Python and `uv venv` to create an **app-local virtual environment**:

- WSL2/Linux: `~/media-search-agent/.venv/`
- macOS: `~/Library/Application Support/MediaSearchAgent/.venv/` (user-writable;
  the app bundle under `/Applications` remains read-only after install)

Pin the exact Python version at release time (e.g. `UV_PYTHON_VERSION="3.12.9"`)
so all installations use the same interpreter.

## Rationale

### Why not system Python

- macOS system Python varies by OS version (3.9 on older macOS; Apple explicitly
  discourages relying on it and may remove it in future OS updates).
- WSL2 Ubuntu 22.04 ships Python 3.10, which is below the `requires-python = ">=3.11"`
  floor in `pyproject.toml`. An apt install of 3.12 would still be needed.
- System Python can silently change with OS updates, breaking the app.

### Why not a system-wide Python install

- Installing Python globally via Homebrew or python.org modifies the user's environment
  without their explicit request, can conflict with existing Python versions, and is
  difficult to reverse cleanly on uninstall.
- On WSL2, `apt install python3.12` is clean — but an app-local venv is still required
  on top of it for isolation. Using uv achieves both steps cleanly.

### Why uv

- Single Rust binary (~12 MB) with zero runtime dependencies — easy to bundle in the
  installer payload.
- `uv python install 3.12` searches in order:
  1. Python 3.12 already in PATH → reuses it (no download)
  2. uv-managed 3.12 already present → reuses it
  3. Nothing found → downloads python-build-standalone CPython 3.12 (~55 MB)
- Python is installed to `~/.local/share/uv/python/` — user-local, not system-wide.
- `uv pip install` is 10–100x faster than pip for the ML dependency set.
- Clean uninstall: deleting `.venv/` removes the entire dependency tree. The uv-managed
  Python at `~/.local/share/uv/python/` is user-local and not touched by the uninstaller
  (the user may use uv for their own projects).

## Version Pinning

```bash
# In setup.sh and install.sh — pinned at release time
UV_PYTHON_VERSION="3.12.9"
uv python install "$UV_PYTHON_VERSION"
uv venv .venv --python "$UV_PYTHON_VERSION"
uv pip install -r requirements-api.txt
```

Pinning the patch version prevents ML package ABI sensitivity (NumPy, ONNX Runtime)
from causing subtle regressions between installations on different machines or at
different points in time.

## onnxruntime Platform Split

`requirements-api.txt` specifies `onnxruntime-gpu` for CUDA support on WSL2/Linux.
On macOS, `onnxruntime-gpu` does not exist. The platform split is handled in setup
scripts, not in the requirements file:

- `install.sh` (Linux): installs `onnxruntime-gpu`
- `setup.sh` (macOS): installs `onnxruntime` (CPU) or `onnxruntime-silicon` (CoreML
  on Apple Silicon, if beneficial)

## Consequences

- `uv` binary must be bundled for both macOS (ARM64+x86_64 universal2) and Linux x86_64.
- Node.js (for the React UI build) is NOT managed by uv — use system Node or nvm;
  it is a build-time dependency only.
- The venv path is deterministic and documented; straightforward to inspect or debug.
- Reinstalls run `uv pip install --upgrade -r requirements-api.txt` against the existing
  venv — much faster than a full fresh install on upgrades.
