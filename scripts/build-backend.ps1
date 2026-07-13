# Stage the Python backend for the bundled-uv adapter (design §3a) — WINDOWS-NATIVE port of
# scripts/build-backend.sh, for hosts without Git Bash. Same two outputs as the .sh:
#   1. the uv binary           -> src-tauri/bin/uv.exe   (the provisioning tool)
#   2. the backend Python src  -> src-tauri/backend/app/ (run as `python -m app`)
# The standalone CPython + venv are NOT built here — the shell provisions them with uv on
# first run, into %LOCALAPPDATA%\<id>\ (so they're app-owned for a clean Tier-1 uninstall).
#
# WHY a .ps1 exists (cross-platform delta): the resource *contents* are identical on both
# OSes; only the staging shell forks (bash vs PowerShell) and the uv binary name (uv vs
# uv.exe). The tauri.conf resources glob "bin/uv*" matches either, so no config fork.
$ErrorActionPreference = "Stop"
Set-Location (Join-Path $PSScriptRoot "..")

# --- 1. Stage the uv binary -------------------------------------------------------------
# Spike: copy the host's uv. A real template/CI step downloads a pinned uv release per
# target arch instead (FRICTION_LOG §F7 — host-arch-locked + unpinned otherwise).
$uv = (Get-Command uv -ErrorAction SilentlyContinue).Source
if (-not $uv) { Write-Error "[backend] ERROR: uv not found on PATH"; exit 1 }
New-Item -ItemType Directory -Force "src-tauri/bin" | Out-Null
Copy-Item $uv "src-tauri/bin/uv.exe" -Force
Write-Output "[backend] staged uv ($(& $uv --version)) -> src-tauri/bin/uv.exe"

# --- 2. Stage the backend Python source -------------------------------------------------
if (Test-Path "src-tauri/backend") { Remove-Item -Recurse -Force "src-tauri/backend" }
New-Item -ItemType Directory -Force "src-tauri/backend" | Out-Null
Copy-Item -Recurse "backend/app" "src-tauri/backend/app"
# Don't ship compiled bytecode into the bundle.
Get-ChildItem -Recurse -Directory "src-tauri/backend" -Filter "__pycache__" -ErrorAction SilentlyContinue |
  Remove-Item -Recurse -Force -ErrorAction SilentlyContinue
Write-Output "[backend] staged backend source -> src-tauri/backend/app/ (spawned as: python -m app)"
Write-Output "[backend] done - bundle resources ready (bin/uv.exe + backend/app)"
