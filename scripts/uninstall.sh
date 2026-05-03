#!/usr/bin/env bash
# Media Search Agent — macOS Uninstaller
# Removes the application. Prompts before deleting user data.
# Never touches: WSL2 distro, ML model caches, uv, system Python.
#
# Usage: bash scripts/uninstall.sh

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
MSA_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
source "$SCRIPT_DIR/lib/common.sh"

setup_log "uninstall"

# ── Helpers ───────────────────────────────────────────────────────────────────

confirm() {
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
  [[ "${answer,,}" == "y" ]]
}

dir_size() {
  du -sh "$1" 2>/dev/null | awk '{print $1}' || echo "unknown"
}

# ── Guard ─────────────────────────────────────────────────────────────────────

log_bold "Media Search Agent — Uninstaller"
log_warn "This will remove the application from $MSA_ROOT"
echo ""

if ! confirm "Are you sure you want to uninstall Media Search Agent?"; then
  log_info "Uninstall cancelled."
  exit 0
fi

# ── Stop running services ─────────────────────────────────────────────────────

log_bold "Stopping services..."
if [[ -f "$SCRIPT_DIR/stop.sh" ]]; then
  bash "$SCRIPT_DIR/stop.sh" || log_warn "stop.sh returned non-zero — services may not have been running"
else
  log_skip "stop.sh not found — skipping"
fi

# ── Tier 2: prompt for user data ──────────────────────────────────────────────

echo ""
log_bold "User data (default: keep)"
echo ""

REMOVE_INDEX=false
REMOVE_THUMBNAILS=false

INDEX_DIR="$MSA_ROOT/index"
DATA_DIR="$MSA_ROOT/data"

if [[ -d "$INDEX_DIR" ]]; then
  INDEX_SIZE=$(dir_size "$INDEX_DIR")
  echo "  Media index (SQLite + FAISS): $INDEX_SIZE"
  echo "  Location: $INDEX_DIR"
  if confirm "  Delete media index? This cannot be recovered."; then
    REMOVE_INDEX=true
  fi
  echo ""
fi

if [[ -d "$DATA_DIR" ]]; then
  DATA_SIZE=$(dir_size "$DATA_DIR")
  echo "  Thumbnails and face crops: $DATA_SIZE"
  echo "  Location: $DATA_DIR"
  if confirm "  Delete thumbnails and face crops? This cannot be recovered."; then
    REMOVE_THUMBNAILS=true
  fi
  echo ""
fi

# ── Tier 1: always remove ─────────────────────────────────────────────────────

log_bold "Removing application files..."

# Stop and remove Qdrant Docker container
if command -v docker &>/dev/null && docker ps -a --filter "name=^msa-qdrant$" --format '{{.Names}}' 2>/dev/null | grep -q msa-qdrant; then
  docker stop msa-qdrant > /dev/null 2>&1 || true
  docker rm msa-qdrant > /dev/null 2>&1 || true
  log_ok "Removed: msa-qdrant Docker container"
fi

# Source code
if [[ -d "$MSA_ROOT/src" ]]; then
  rm -rf "$MSA_ROOT/src"
  log_ok "Removed: src/"
fi

# Virtual environment
if [[ -d "$MSA_ROOT/.venv" ]]; then
  rm -rf "$MSA_ROOT/.venv"
  log_ok "Removed: .venv/"
fi

# Bundled binaries (exiftool, mediainfo placed by setup.sh on macOS)
if [[ -d "$MSA_ROOT/bin" ]]; then
  rm -rf "$MSA_ROOT/bin"
  log_ok "Removed: bin/"
fi

# Qdrant storage (not user media data)
if [[ -d "$MSA_ROOT/qdrant" ]]; then
  rm -rf "$MSA_ROOT/qdrant"
  log_ok "Removed: qdrant/ (vector store)"
fi

# Runtime artefacts — PID files now live in logs/run/ (RUN_DIR=$MSA_LOG_DIR/run)
rm -rf "$MSA_ROOT/logs/run"
rm -f "$MSA_ROOT/logs"/launch-*.log "$MSA_ROOT/logs"/stop-*.log \
      "$MSA_ROOT/logs"/qdrant.log "$MSA_ROOT/logs"/uvicorn.log

# macOS app bundle (if present from Phase 1C installer)
if [[ -d "/Applications/MediaSearchAgent.app" ]]; then
  rm -rf "/Applications/MediaSearchAgent.app"
  log_ok "Removed: /Applications/MediaSearchAgent.app"
fi

# launchd plist (Start on Login)
PLIST="$HOME/Library/LaunchAgents/com.mediasearchagent.plist"
if [[ -f "$PLIST" ]]; then
  launchctl unload "$PLIST" 2>/dev/null || true
  rm -f "$PLIST"
  log_ok "Removed: launchd plist (Start on Login)"
fi

# ── Tier 2: conditional removal ───────────────────────────────────────────────

if [[ "$REMOVE_INDEX" == true ]] && [[ -d "$INDEX_DIR" ]]; then
  rm -rf "$INDEX_DIR"
  log_ok "Removed: index/ (SQLite + FAISS)"
fi

if [[ "$REMOVE_THUMBNAILS" == true ]] && [[ -d "$DATA_DIR" ]]; then
  rm -rf "$DATA_DIR"
  log_ok "Removed: data/ (thumbnails + face crops)"
fi

# ── Tier 3: never touched ─────────────────────────────────────────────────────
# ~/.cache/open_clip         — CLIP model cache (shared with other ML tools)
# ~/.cache/huggingface/      — RT-DETR / HuggingFace models (shared)
# ~/.insightface/models      — InsightFace models (shared)
# ~/.local/share/uv          — uv-managed Python (user's own tool)
# config.yaml            — preserved if user kept their index, for potential reinstall

if [[ -d "$INDEX_DIR" ]] || [[ -d "$DATA_DIR" ]]; then
  log_info "config.yaml preserved (you kept data — useful for reinstalling)"
fi

# ── Done ──────────────────────────────────────────────────────────────────────

echo ""
log_ok "──────────────────────────────────────────────"
log_ok "Media Search Agent has been uninstalled."
echo ""
log_info "Not removed (shared resources):"
log_info "  ~/.cache/open_clip       — CLIP model cache"
log_info "  ~/.cache/huggingface/    — RT-DETR / HuggingFace models"
log_info "  ~/.insightface/models    — InsightFace models"
log_info "  ~/.local/share/uv        — uv Python manager"
log_info "  qdrant/qdrant:latest    — Docker image (not removed; shared with other apps)"
log_info ""
log_info "To reinstall: bash scripts/setup.sh"
log_ok "──────────────────────────────────────────────"
