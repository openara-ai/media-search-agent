#!/usr/bin/env bash
# Media Search Agent — Shell Installer Uninstaller (macOS + Linux)
#
# Removal tiers (ADR-005):
#   Tier 1 — Always (no prompt):
#     App code items (src/, scripts/, bin/, pyproject.toml, requirements-api.txt,
#     LICENSE, NOTICE, uninstall.sh), Python venv, launcher (~/.local/bin/msa),
#     auto-start (LaunchAgent on macOS, systemd user unit on Linux)
#
#   Tier 2 — Prompt, default KEEP:
#     Index + thumbnails + Qdrant vector DB (can represent hours of indexing)
#     Config file (media source paths)
#     Logs
#     App-private ML model cache (re-downloadable but large)
#
#   Tier 3 — Never removed:
#     User media library — never modified by this app
#
#   Note: uv is stored app-private in APP_CODE_DIR/bin/uv and is removed with
#   the rest of bin/ as part of Tier 1. It is not shared with other tools.
#
# Usage:
#   msa uninstall          (via the launcher)
#   bash ~/.local/share/MediaSearchAgent/uninstall.sh

set -euo pipefail

# ── Detect OS + resolve paths (must match install.sh) ─────────────────────────

case "$(uname -s)" in
  Darwin) OS="macos" ;;
  Linux)  OS="linux" ;;
  *) echo "Unsupported OS: $(uname -s)"; exit 1 ;;
esac

# Default paths — match install.sh ADR-009 layout.
# On macOS the entire install lives inside ~/Applications/MediaSearchAgent.app;
# Tier 1 removal is a single rm -rf on the bundle.
# The launcher sets MSA_ROOT (= Contents/Resources on macOS) so APP_CODE_DIR is
# resolved correctly when invoked via `msa uninstall`.

if [[ "$OS" == "macos" ]]; then
  APP_BUNDLE="$HOME/Applications/MediaSearchAgent.app"
  APP_CODE_DIR="${MSA_ROOT:-$APP_BUNDLE/Contents/Resources}"
  APP_SUPPORT_DIR="$HOME/Library/Application Support/MediaSearchAgent"
  CACHE_DIR="$HOME/Library/Caches/MediaSearchAgent"
  LOG_DIR="$HOME/Library/Logs/MediaSearchAgent"
  CONFIG_PATH="$APP_SUPPORT_DIR/config.yaml"
else
  APP_BUNDLE=""
  APP_CODE_DIR="${MSA_ROOT:-${MSA_DIR:-$HOME/.local/share/MediaSearchAgent}}"
  APP_SUPPORT_DIR="$HOME/.local/share/MediaSearchAgent"
  CACHE_DIR="$HOME/.cache/MediaSearchAgent"
  LOG_DIR="$HOME/.local/share/MediaSearchAgent/logs"
  CONFIG_PATH="$HOME/.config/MediaSearchAgent/config.yaml"
fi

VENV_DIR="${MSA_VENV_DIR:-$APP_CODE_DIR/.venv}"
LAUNCHER="$HOME/.local/bin/msa"
LAUNCH_AGENT_PLIST="$HOME/Library/LaunchAgents/ai.openara.mediasearchagent.plist"
SYSTEMD_UNIT="$HOME/.config/systemd/user/mediasearchagent.service"

# ── Colours ───────────────────────────────────────────────────────────────────

BOLD=""; GREEN=""; YELLOW=""; RED=""; DIM=""; NC=""
if [[ -t 1 ]] && [[ "${NO_COLOR:-}" != "1" ]]; then
  BOLD="\033[1m"; GREEN="\033[32m"; YELLOW="\033[33m"
  RED="\033[31m"; DIM="\033[2m"; NC="\033[0m"
fi

log()       { printf "  %s\n" "$*"; }
log_ok()    { printf "  ${GREEN}✓${NC} %s\n" "$*"; }
log_warn()  { printf "  ${YELLOW}!${NC} %s\n" "$*"; }
log_skip()  { printf "  ${DIM}–${NC} %s\n" "$*"; }
log_bold()  { printf "\n${BOLD}%s${NC}\n" "$*"; }

setup_uninstall_logging() {
  local timestamp temp_dir
  timestamp="$(date '+%Y-%m-%d_%H%M%S')"
  temp_dir="${TMPDIR:-/tmp}"
  UNINSTALL_LOG_BASENAME="uninstall-${timestamp}.log"
  UNINSTALL_LOG_FILE="$(mktemp "${temp_dir%/}/msa-${UNINSTALL_LOG_BASENAME}.XXXXXX")"
  exec > >(tee >(sed -E $'s/\x1B\\[[0-9;]*[[:alpha:]]//g' >> "$UNINSTALL_LOG_FILE")) 2>&1
  log "Log (temp): $UNINSTALL_LOG_FILE"
}

archive_uninstall_log() {
  mkdir -p "$LOG_DIR"
  UNINSTALL_LOG_ARCHIVE_PATH="$LOG_DIR/$UNINSTALL_LOG_BASENAME"
  mv "$UNINSTALL_LOG_FILE" "$UNINSTALL_LOG_ARCHIVE_PATH"
  UNINSTALL_LOG_FILE="$UNINSTALL_LOG_ARCHIVE_PATH"
  log "Log saved:  $UNINSTALL_LOG_FILE"
}

ask() {
  local prompt="$1" default="${2:-n}" answer
  local hint; [[ "$default" == "y" ]] && hint="[Y/n]" || hint="[y/N]"
  printf "  %s %s " "$prompt" "$hint"
  read -r answer
  answer="${answer:-$default}"
  [[ "$(echo "$answer" | tr '[:upper:]' '[:lower:]')" == "y" ]]
}

# ── Banner ────────────────────────────────────────────────────────────────────

UNINSTALL_LOG_FILE=""
UNINSTALL_LOG_BASENAME=""
UNINSTALL_LOG_ARCHIVE_PATH=""
KEEP_LOG_FILES="unknown"
setup_uninstall_logging

printf "\n${BOLD}  Media Search Agent — Uninstaller${NC}\n"
printf "${DIM}  Shell install layout (ADR-005 removal tiers)${NC}\n\n"
if [[ "$OS" == "macos" ]]; then
  log "App:       $APP_BUNDLE"
else
  log "App code:  $APP_CODE_DIR"
fi
log "Data:      $APP_SUPPORT_DIR"
log "Config:    $CONFIG_PATH"
log "Venv:      $VENV_DIR"
log ""

if ! ask "Uninstall Media Search Agent?"; then
  echo "  Uninstall cancelled."
  exit 0
fi

# ── Tier 1: Stop services ─────────────────────────────────────────────────────

log_bold "Tier 1 — Stopping services"

if [[ -f "$APP_CODE_DIR/scripts/stop.sh" ]]; then
  bash "$APP_CODE_DIR/scripts/stop.sh" 2>/dev/null || true
fi
pkill -TERM -f "msa_indexer" 2>/dev/null || true
pkill -TERM -f "uvicorn.*msa" 2>/dev/null || true
log_ok "Services stopped"

# ── Tier 1: Remove auto-start ─────────────────────────────────────────────────

log_bold "Tier 1 — Removing auto-start"

if [[ "$OS" == "macos" && -f "$LAUNCH_AGENT_PLIST" ]]; then
  launchctl unload "$LAUNCH_AGENT_PLIST" 2>/dev/null || true
  rm -f "$LAUNCH_AGENT_PLIST"
  log_ok "LaunchAgent removed"
elif [[ "$OS" == "linux" ]]; then
  if systemctl --user is-active mediasearchagent.service &>/dev/null; then
    systemctl --user stop mediasearchagent.service 2>/dev/null || true
  fi
  if [[ -f "$SYSTEMD_UNIT" ]]; then
    systemctl --user disable mediasearchagent.service 2>/dev/null || true
    rm -f "$SYSTEMD_UNIT"
    systemctl --user daemon-reload 2>/dev/null || true
    log_ok "systemd user service removed"
  fi
fi

# ── Tier 1: Remove app bundle / code ──────────────────────────────────────────

log_bold "Tier 1 — Removing app"

pkill -x "MediaSearchAgent" 2>/dev/null || true

if [[ "$OS" == "macos" ]]; then
  # macOS: entire install lives inside the .app — one rm removes everything
  # (app code, src/, scripts/, bin/, venv/ are all inside Contents/Resources/).
  if [[ -d "$APP_BUNDLE" ]]; then
    rm -rf "$APP_BUNDLE"
    log_ok "App bundle removed: $APP_BUNDLE"
  else
    log_skip "App bundle not found at $APP_BUNDLE"
  fi
else
  # Linux: surgical removal — APP_CODE_DIR == APP_SUPPORT_DIR so we cannot
  # delete the whole directory (it also holds index/data/thumbnails).
  CODE_ITEMS=(src scripts pyproject.toml requirements-api.txt LICENSE NOTICE uninstall.sh bin .venv)
  _any_code=0
  for item in "${CODE_ITEMS[@]}"; do
    target="$APP_CODE_DIR/$item"
    if [[ -e "$target" ]]; then
      rm -rf "$target"
      log_ok "Removed: $APP_CODE_DIR/$item"
      _any_code=1
    fi
  done
  [[ "$_any_code" == "0" ]] && log_skip "No app code found at $APP_CODE_DIR"
fi

# ── Tier 1: Remove launcher ───────────────────────────────────────────────────

log_bold "Tier 1 — Removing launcher"

if [[ -f "$LAUNCHER" ]]; then
  rm -f "$LAUNCHER"
  log_ok "Launcher removed: $LAUNCHER"
fi

# ── Tier 2: Index + data (prompt, default KEEP) ───────────────────────────────

log_bold "Tier 2 — User data (kept by default)"
log "The media index, thumbnails, and Qdrant vector DB represent your indexing work."
log ""

INDEX_DIR="$APP_SUPPORT_DIR/index"
DATA_SUBDIR="$APP_SUPPORT_DIR/data"
QDRANT_DIR="$APP_SUPPORT_DIR/qdrant"
_any_data=0
for d in "$INDEX_DIR" "$DATA_SUBDIR" "$QDRANT_DIR"; do
  [[ -d "$d" ]] && _any_data=1
done

if [[ "$_any_data" == "1" ]]; then
  TOTAL_SIZE="$(du -sh "$INDEX_DIR" "$DATA_SUBDIR" "$QDRANT_DIR" 2>/dev/null \
    | tail -1 | cut -f1 || echo "unknown")"
  log "  $INDEX_DIR"
  log "  $DATA_SUBDIR"
  log "  $QDRANT_DIR"
  log "  (approx $TOTAL_SIZE)"
  log ""
  if ask "Delete media index and thumbnails? This cannot be undone."; then
    rm -rf "$INDEX_DIR" "$DATA_SUBDIR" "$QDRANT_DIR"
    log_ok "Index and thumbnails removed"
  else
    log_skip "Keeping index and thumbnails"
  fi
else
  log_skip "No index data found"
fi

# ── Tier 2: Config (prompt, default KEEP) ─────────────────────────────────────

if [[ -f "$CONFIG_PATH" ]]; then
  log ""
  log "  $CONFIG_PATH"
  if ask "Delete config file (media source paths)?"; then
    rm -f "$CONFIG_PATH"
    rmdir "$(dirname "$CONFIG_PATH")" 2>/dev/null || true
    log_ok "Config removed"
  else
    log_skip "Keeping config"
  fi
fi

# ── Tier 2: Logs (prompt, default KEEP) ───────────────────────────────────────

if [[ -d "$LOG_DIR" ]]; then
  log ""
  log "  $LOG_DIR"
  if ask "Delete log files?"; then
    KEEP_LOG_FILES="no"
    rm -rf "$LOG_DIR"
    log_ok "Logs removed"
  else
    KEEP_LOG_FILES="yes"
    log_skip "Keeping logs"
  fi
fi

# ── Tier 2: App-private model cache (prompt, default KEEP) ───────────────────

if [[ -d "$CACHE_DIR" ]]; then
  log ""
  log "  $CACHE_DIR"
  if ask "Delete app-private ML model cache? Re-download will be required later."; then
    rm -rf "$CACHE_DIR"
    log_ok "Model cache removed"
  else
    log_skip "Keeping model cache"
  fi
fi

# ── Tier 3: Never removed ─────────────────────────────────────────────────────

printf "\n${DIM}  Not removed (Tier 3 — never touched):${NC}\n"
printf "${DIM}    Your media library — never modified by this app${NC}\n\n"

if [[ "$KEEP_LOG_FILES" != "no" ]]; then
  archive_uninstall_log
else
  printf "${DIM}  Uninstall log kept in temp: %s${NC}\n\n" "$UNINSTALL_LOG_FILE"
fi

printf "${GREEN}${BOLD}✓ Media Search Agent uninstalled.${NC}\n\n"
