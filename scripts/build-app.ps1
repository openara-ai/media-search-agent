# Full desktop build (Windows): backend sidecar -> Tauri shell -> NSIS installer.
# WINDOWS-NATIVE port of scripts/build-app.sh, for hosts without Git Bash. Run from a clean
# checkout on a Windows host — the one runtime path a macOS gate can't build locally.
#
# Updater artifacts (.nsis.zip + .sig) are produced when a signing key is present:
#   $env:TAURI_SIGNING_PRIVATE_KEY = Get-Content private/media-search-agent-updater.key -Raw
#   $env:TAURI_SIGNING_PRIVATE_KEY_PASSWORD = ""
$ErrorActionPreference = "Stop"
Set-Location (Join-Path $PSScriptRoot "..")

& "$PSScriptRoot/build-backend.ps1"

Write-Output "[app] tauri build"
npx tauri build

Write-Output "[app] done. Artifacts under src-tauri/target/release/bundle/"
