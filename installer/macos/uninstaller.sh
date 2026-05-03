#!/usr/bin/env bash
# Media Search Agent — Uninstaller
#
# Run directly by the user (via the Uninstall app bundled in the DMG, or from a
# copy they've kept).  Compatible with the Phase 2F single-bundle layout where
# all app code lives inside MediaSearchAgent.app/Contents/Resources/.
#
# Removal tiers (per ADR-005):
#   Always:   app bundle, LaunchAgent plist
#   Always:   Python venv (~500 MB)
#   Prompt:   persistent app data (index/data/qdrant/config)
#   Never:    ML model caches (~2 GB), user media

set -euo pipefail

# App lives inside the .app bundle folder.
MSA_ROOT="/Applications/MediaSearchAgent.app/Contents/Resources"

# user data and others (model cache, logslive elsewhere
VENV_DIR="$HOME/Library/Application Support/MediaSearchAgent/.venv"
DATA_DIR="$HOME/Library/Application Support/MediaSearchAgent"
INDEX_DIR="$DATA_DIR/index"
APP_DATA_SUBDIR="$DATA_DIR/data"
QDRANT_DIR="$DATA_DIR/qdrant"
CONFIG_PATH="$DATA_DIR/config.yaml"
CACHE_DIR="$HOME/Library/Caches/MediaSearchAgent"
LAUNCH_AGENT_PLIST="$HOME/Library/LaunchAgents/com.mediasearchagent.plist"
LOG_DIR="$HOME/Library/Logs/MediaSearchAgent"

log()  { echo "$*"; }
ask()  {
  local prompt="$1"
  local default="${2:-n}"
  local answer
  if [[ "$default" == "y" ]]; then
    read -r -p "$prompt [Y/n] " answer
    answer="${answer:-y}"
  else
    read -r -p "$prompt [y/N] " answer
    answer="${answer:-n}"
  fi
  [[ "$(echo "$answer" | tr '[:upper:]' '[:lower:]')" == "y" ]]
}

log "Media Search Agent — Uninstaller"
log "=================================="
log ""

if ! ask "This will remove Media Search Agent. Are you sure?"; then
  log "Uninstall cancelled."
  exit 0
fi

log ""

# ── Stop running services ─────────────────────────────────────────────────────
# stop.sh handles both the indexer (SIGTERM, 15 s grace) and uvicorn (10 s
# grace) in the right order — indexer first so SQLite (and any pending WAL
# checkpoint) is flushed cleanly before the API exits.  The pkill lines
# below are a belt-and-suspenders safety
# net in case any process slipped through (e.g. a stale PID file was absent).

log "Stopping services..."
bash "$MSA_ROOT/scripts/stop.sh" 2>/dev/null || true
pkill -TERM -f "msa_indexer" 2>/dev/null || true
pkill -TERM -f "uvicorn.*msa" 2>/dev/null || true
log "Services stopped."

# ── Remove LaunchAgent (login item) ──────────────────────────────────────────

if [[ -f "$LAUNCH_AGENT_PLIST" ]]; then
  launchctl unload "$LAUNCH_AGENT_PLIST" 2>/dev/null || true
  rm -f "$LAUNCH_AGENT_PLIST"
  log "LaunchAgent removed."
fi

# ── Remove app files ──────────────────────────────────────────────────────────
# /Applications/ is owned by root; sudo is required when not running as root.

_sudo_rm() {
  local target="$1"
  local owner
  owner=$(stat -f "%Su" "$target" 2>/dev/null || echo "root")
  if [[ "$owner" == "$(whoami)" ]]; then
    rm -rf "$target"
  else
    log "Admin password required to remove $target"
    sudo rm -rf "$target"
  fi
}

if [[ -d "/Applications/MediaSearchAgent.app" ]]; then
  _sudo_rm "/Applications/MediaSearchAgent.app"
  log "Application bundle removed: /Applications/MediaSearchAgent.app"
fi

# ── Always remove Python venv ────────────────────────────────────────────────

if [[ -d "$VENV_DIR" ]]; then
  rm -rf "$VENV_DIR"
  log "Python environment removed: $VENV_DIR"
fi

# ── Optional: remove persistent app data ─────────────────────────────────────
# Keep by default. The venv lives alongside this data under Application Support,
# so delete only the specific persistent-data paths rather than DATA_DIR itself.

PERSISTENT_PATHS=()
for path in "$INDEX_DIR" "$APP_DATA_SUBDIR" "$QDRANT_DIR" "$CONFIG_PATH"; do
  if [[ -e "$path" ]]; then
    PERSISTENT_PATHS+=("$path")
  fi
done

if [[ ${#PERSISTENT_PATHS[@]} -gt 0 ]]; then
  TOTAL_SIZE=$(du -sh "${PERSISTENT_PATHS[@]}" 2>/dev/null | tail -1 | cut -f1 || echo "unknown")
  log ""
  log "Persistent app data found ($TOTAL_SIZE):"
  [[ -e "$INDEX_DIR" ]] && log "  - $INDEX_DIR"
  [[ -e "$APP_DATA_SUBDIR" ]] && log "  - $APP_DATA_SUBDIR"
  [[ -e "$QDRANT_DIR" ]] && log "  - $QDRANT_DIR"
  [[ -e "$CONFIG_PATH" ]] && log "  - $CONFIG_PATH"
  if ask "Remove persistent app data (kept by default)?"; then
    for path in "${PERSISTENT_PATHS[@]}"; do
      rm -rf "$path"
      log "Removed: $path"
    done
  else
    log "Keeping persistent app data."
  fi
fi

# ── Remove install/launch/stop logs ──────────────────────────────────────────
# LOG_DIR also contains run/ (PID files written by start.sh and indexer_manager).

if [[ -d "$LOG_DIR" ]]; then
  if ask "Remove install/run logs ($LOG_DIR)?"; then
    rm -rf "$LOG_DIR"
    log "Logs removed."
  fi
fi

# ── Not removed (user data) ───────────────────────────────────────────────────

log ""
log "The following were NOT removed (your data):"
log "  Search DB: $INDEX_DIR"
log "  Vector DB: $QDRANT_DIR"
log "  Thumbnails: $APP_DATA_SUBDIR"
log "  Config: $CONFIG_PATH"
log "  ML model cache: $CACHE_DIR/models/"
log "  Your media library (photos/videos) — never modified by this app"
log ""
log "To remove ML model caches (~2 GB):"
log "  rm -rf $CACHE_DIR/models"
log ""
log "Media Search Agent has been uninstalled."
