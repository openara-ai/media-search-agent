#!/usr/bin/env bash
# Full build: backend sidecar -> Tauri shell -> installer (.dmg/.app on macOS).
#
# Updater artifacts (.app.tar.gz + .sig) are produced when a signing key is present:
#   export TAURI_SIGNING_PRIVATE_KEY="$(cat private/media-search-agent-updater.key)"
#   export TAURI_SIGNING_PRIVATE_KEY_PASSWORD=""
set -euo pipefail
cd "$(dirname "$0")/.."

./scripts/build-backend.sh

echo "[app] tauri build"
npx tauri build

echo "[app] done. Artifacts under src-tauri/target/release/bundle/"
