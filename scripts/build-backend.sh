#!/usr/bin/env bash
# Stage the Python backend for the bundled-uv adapter (design §3a). NO PyInstaller.
#
# Unlike the PyInstaller path (compile a per-arch binary, rename to the Tauri target
# triple), the uv path ships TWO things into the bundle as plain resources:
#   1. the `uv` binary           -> src-tauri/bin/uv         (the provisioning tool)
#   2. the backend Python source -> src-tauri/backend/app/   (run as `python -m app`)
# The standalone CPython + venv are NOT built here — the shell provisions them with `uv`
# on first run, into the app-private data dir (so they're app-owned for clean uninstall).
#
# This is the work the template's Python build ADAPTER should own; it is identical for
# every Python-backend project in the fleet (only the source dir differs). No target
# triple, no Hardened-Runtime entitlement, no codesign-of-a-frozen-binary.
set -euo pipefail
cd "$(dirname "$0")/.."

# --- 1. Stage the uv binary -------------------------------------------------------------
# Spike: copy the host's uv (pinned 0.5.21, matching the media-search-agent reference).
# A real template/CI step downloads a pinned uv release per target arch instead.
# Cross-platform: this script also runs under Git Bash on Windows, where uv is `uv.exe`.
# (tauri.conf resources glob "bin/uv*" so it picks up either name. A native PowerShell
#  build-backend.ps1 is the Windows agent's to add if Git Bash isn't available.)
UV_BIN="${UV_BIN:-$(command -v uv)}"
[ -n "$UV_BIN" ] || { echo "[backend] ERROR: uv not found on PATH"; exit 1; }
EXT=""; case "$(uname -s)" in MINGW*|MSYS*|CYGWIN*) EXT=".exe";; esac
mkdir -p src-tauri/bin
cp "$UV_BIN" "src-tauri/bin/uv${EXT}"
chmod +x "src-tauri/bin/uv${EXT}" 2>/dev/null || true
echo "[backend] staged uv ($("$UV_BIN" --version)) -> src-tauri/bin/uv${EXT}"

# --- 2. Stage the backend Python source -------------------------------------------------
rm -rf src-tauri/backend
mkdir -p src-tauri/backend
cp -R backend/app src-tauri/backend/app
# Don't ship compiled bytecode or stray files into the (signed, sealed) bundle.
find src-tauri/backend -name '__pycache__' -type d -prune -exec rm -rf {} + 2>/dev/null || true
echo "[backend] staged backend source -> src-tauri/backend/app/ (spawned as: python -m app)"
echo "[backend] done — bundle resources ready (bin/uv + backend/app)"
