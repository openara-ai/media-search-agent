#!/usr/bin/env bash
set -euo pipefail

# Media Search Agent — tiered desktop uninstaller for macOS (ADR-005).
#
# Removes the Tauri desktop app and its provisioned runtime (Tier 1, always),
# prompts before touching persistent user data (Tier 2, default KEEP), and
# never enumerates shared resources (Tier 3).
#
# Direct run: bash uninstall-desktop.sh [--remove-data | --keep-data]
#
#   --remove-data   also delete the index, config.yaml, logs and the
#                   app-private model cache (no prompt)
#   --keep-data     keep all user data (no prompt)
#   (no flag)       interactive prompt, default keep; non-interactive runs
#                   (no TTY) always keep — unattended = keep
#
# Windows: uninstall via Settings > Apps > Media Search Agent — the NSIS
# uninstaller applies the same tiers (packaging/windows/msa-installer-hooks.nsh).
# The legacy (pre-desktop) install has its own scripts/uninstall.sh.

# ── Colours / logging (self-contained: this script runs on end-user machines
#    with no repo checkout, so nothing is sourced) ────────────────────────────

BOLD='\033[1m'
GREEN='\033[0;32m'
YELLOW='\033[0;33m'
RED='\033[0;31m'
DIM='\033[2m'
NC='\033[0m'

_disable_colors() { BOLD=''; GREEN=''; YELLOW=''; RED=''; DIM=''; NC=''; }
[[ ! -t 1 || -n "${NO_COLOR:-}" || "${TERM:-}" == "dumb" ]] && _disable_colors

log_info()  { printf "${DIM}·${NC} %s\n" "$*" >&2; }
log_ok()    { printf "${GREEN}✓${NC} %s\n" "$*" >&2; }
log_skip()  { printf "${DIM}– %s${NC}\n" "$*" >&2; }
log_warn()  { printf "${YELLOW}!${NC} %s\n" "$*" >&2; }
log_bold()  { printf "\n${BOLD}%s${NC}\n" "$*" >&2; }
die()       { printf "${RED}✗${NC} %s\n" "$*" >&2; exit 1; }

[[ "$(uname -s)" == "Darwin" ]] || die "This uninstaller is for the macOS desktop app. On Windows use Settings > Apps; for a legacy install use scripts/uninstall.sh."

APP_NAME="MediaSearchAgent"
APP_ID="ai.openara.mediasearchagent"

# Tier 1 — the app and everything the shell provisioned (always removed).
# The one-liner installs into ~/Applications; the documented .dmg path drags the
# app into /Applications. Remove both fixed, app-owned bundle locations.
APP_BUNDLE="${HOME:?}/Applications/${APP_NAME}.app"
APP_BUNDLE_SYSTEM="/Applications/${APP_NAME}.app"
RUNTIME_DIR="${HOME:?}/Library/Application Support/${APP_ID}"   # venv, uv-managed CPython, uv cache, staged tools
WEBKIT_DIRS=(
  "${HOME:?}/Library/WebKit/${APP_ID}"
  "${HOME:?}/Library/Caches/${APP_ID}"
  "${HOME:?}/Library/HTTPStorages/${APP_ID}"
  "${HOME:?}/Library/Saved Application State/${APP_ID}.savedState"
)
PREFS_PLIST="${HOME:?}/Library/Preferences/${APP_ID}.plist"
LEGACY_LAUNCH_AGENT="${HOME:?}/Library/LaunchAgents/${APP_ID}.plist"  # migration leftover
# Optional `msa` CLI opt-in symlink (Settings > Command-line tool). Removed in Tier 1
# only when it points into our runtime dir, so a user's own ~/.local/bin/msa is safe.
CLI_LAUNCHER="${HOME:?}/.local/bin/msa"

# Tier 2 — persistent user data (prompt, default KEEP).
DATA_DIR="${HOME:?}/Library/Application Support/${APP_NAME}"    # config.yaml, index/, data/ (thumbnails), qdrant/
LOG_DIR="${HOME:?}/Library/Logs/${APP_NAME}"
MODEL_CACHE="${HOME:?}/Library/Caches/${APP_NAME}"              # app-private model cache (re-downloadable)

# Tier 3 — NEVER touched, so never enumerated here: the user's media folders,
# shared ML caches (~/.cache/huggingface, ~/.cache/open_clip, ~/.insightface),
# and a user-wide uv (~/.local/share/uv). Models under the SHARED caches belong
# to the machine, not to MSA.

# ── Flags ─────────────────────────────────────────────────────────────────────

DATA_MODE="prompt"
for arg in "$@"; do
  case "$arg" in
    --remove-data) DATA_MODE="remove" ;;
    --keep-data)   DATA_MODE="keep" ;;
    -h|--help)     grep '^#' "$0" | sed 's/^# \{0,1\}//' >&2; exit 0 ;;
    *) die "Unknown option: $arg (use --remove-data or --keep-data)" ;;
  esac
done

dir_size() { du -sh "$1" 2>/dev/null | awk '{print $1}' || echo "?"; }

remove_path() {
  # rm -rf restricted to the fixed, app-owned paths defined above.
  local path="$1"
  if [[ -e "$path" ]]; then
    rm -rf "$path"
    log_ok "Removed: $path"
  else
    log_skip "Not present: $path"
  fi
}

# Ask a y/N question on the controlling terminal. The documented invocation is the
# one-liner `curl … | bash`, where the script's stdin is the pipe (so `-t 0` is
# false) but /dev/tty is still the user's terminal — read from it directly so the
# prompt actually fires. Falls back to stdin when it's a TTY (local `bash script`),
# and to the caller-supplied default ("yes"|"no") only when there is no terminal at
# all (true non-interactive / CI). Returns 0 for yes.
ask_tty() {
  local prompt="$1" default="$2" answer
  # Try /dev/tty first (works for the piped one-liner), then stdin. If NEITHER
  # read succeeds — no controlling terminal, or /dev/tty exists but opening it for
  # read fails (daemon / cron / some CI) — fall back to the tier's default rather
  # than treating the empty answer as "no" (which would wrongly cancel an
  # unattended Tier-1 uninstall).
  if { [[ -r /dev/tty ]] && read -r -p "$prompt" answer < /dev/tty; } \
     || { [[ -t 0 ]] && read -r -p "$prompt" answer; }; then
    [[ "$(printf '%s' "${answer:-n}" | tr '[:upper:]' '[:lower:]')" == "y" ]]
  else
    [[ "$default" == "yes" ]]
  fi
}

# ── Confirm intent ────────────────────────────────────────────────────────────

log_bold "Media Search Agent — desktop uninstaller"
if [[ "$DATA_MODE" == "prompt" ]]; then
  # Tier 1 (app + runtime) removal. Confirm whenever a terminal is reachable —
  # including the piped one-liner. Only a truly non-interactive run proceeds
  # without asking, since invoking the uninstaller there is itself the intent.
  ask_tty "Uninstall Media Search Agent? [y/N] " yes \
    || { log_info "Uninstall cancelled."; exit 0; }
fi

# ── Stop the app (best-effort; the backend sidecar exits with its supervisor) ─

osascript -e "tell application \"${APP_NAME}\" to quit" >/dev/null 2>&1 || true
for _ in 1 2 3 4 5; do
  pgrep -x "${APP_NAME}" >/dev/null 2>&1 || break
  sleep 1
done
pkill -x "${APP_NAME}" 2>/dev/null || true

# ── Stop any detached indexer BEFORE removing the runtime it runs from ────────
# Closing the app window during an index deliberately leaves `msa index run`
# going in its own session (tracked at $LOG_DIR/run/indexer.pid). Quitting the
# Tauri app does not stop it. We must halt it before Tier 1 removes $RUNTIME_DIR
# (its venv) or Tier 2 removes $DATA_DIR (the index it is writing) — otherwise we
# orphan a CPU/GPU job and can delete state out from under a live writer.
stop_detached_indexer() {
  local run_dir="$LOG_DIR/run"
  local pid_file="$run_dir/indexer.pid"
  [[ -f "$pid_file" ]] || return 0
  local pid; pid="$(cat "$pid_file" 2>/dev/null || true)"
  [[ "$pid" =~ ^[0-9]+$ ]] && kill -0 "$pid" 2>/dev/null || return 0
  # Identity check (mirrors the app's _pid_is_indexer): a stale indexer.pid left
  # by a crash can have its PID reused by an unrelated process — verify the
  # cmdline actually looks like our indexer before signalling, so we never
  # TERM/KILL a stranger. Unverifiable cmdline → skip (don't kill on a guess).
  local args; args="$(ps -p "$pid" -o args= 2>/dev/null || true)"
  if ! { { [[ "$args" == *msa* && "$args" == *index* && "$args" == *run* ]] \
           || [[ "$args" == *msa_indexer* ]]; } && [[ -n "$args" ]]; }; then
    return 0
  fi
  log_info "Stopping the background indexer (PID $pid) before removal..."
  # Cooperative stop: the indexer subprocess watches for this sentinel (the same
  # path the app's Stop button uses), so it shuts the DB down cleanly.
  : > "$run_dir/indexer.stop" 2>/dev/null || true
  local waited=0
  while kill -0 "$pid" 2>/dev/null && [[ $waited -lt 30 ]]; do
    sleep 1; waited=$((waited + 1))
  done
  # Escalate only if it ignored the sentinel.
  if kill -0 "$pid" 2>/dev/null; then
    kill -TERM "$pid" 2>/dev/null || true
    sleep 2
    kill -9 "$pid" 2>/dev/null || true
  fi
  log_ok "Background indexer stopped."
}
stop_detached_indexer

# ── Tier 1: always remove ─────────────────────────────────────────────────────

log_bold "Removing the app and provisioned runtime (Tier 1)..."
remove_path "$APP_BUNDLE"
remove_path "$APP_BUNDLE_SYSTEM"
remove_path "$RUNTIME_DIR"
for d in "${WEBKIT_DIRS[@]}"; do
  remove_path "$d"
done
defaults delete "$APP_ID" >/dev/null 2>&1 || true
remove_path "$PREFS_PLIST"

# The `msa` CLI launcher at ~/.local/bin/msa — removed only when it belongs to THIS
# runtime, never a user's own launcher of the same name. Two shapes exist: the
# Settings opt-in writes a symlink (`ln -sf` into the venv); the headless install
# (install.sh --headless) writes a shell WRAPPER that execs the app-private venv.
if [[ -L "$CLI_LAUNCHER" ]]; then
  target="$(readlink "$CLI_LAUNCHER" 2>/dev/null || true)"
  case "$target" in
    "$RUNTIME_DIR"/*) rm -f "$CLI_LAUNCHER"; log_ok "Removed: $CLI_LAUNCHER (msa CLI link)" ;;
    *) log_skip "Kept: $CLI_LAUNCHER (not our link)" ;;
  esac
elif [[ -f "$CLI_LAUNCHER" ]] && grep -qF "$RUNTIME_DIR" "$CLI_LAUNCHER" 2>/dev/null; then
  # Wrapper-script form: remove only when its contents reference OUR runtime.
  rm -f "$CLI_LAUNCHER"; log_ok "Removed: $CLI_LAUNCHER (msa CLI wrapper)"
elif [[ -e "$CLI_LAUNCHER" ]]; then
  log_skip "Kept: $CLI_LAUNCHER (not ours)"
fi

# Migration leftover: the legacy launch agent (unload by label too — a launchd
# job can stay registered after its plist is gone).
if [[ -f "$LEGACY_LAUNCH_AGENT" ]]; then
  launchctl bootout "gui/$(id -u)" "$LEGACY_LAUNCH_AGENT" 2>/dev/null || true
  remove_path "$LEGACY_LAUNCH_AGENT"
fi
launchctl remove "$APP_ID" 2>/dev/null || true

# ── Tier 2: user data (default KEEP) ──────────────────────────────────────────

REMOVE_DATA=false
if [[ "$DATA_MODE" == "remove" ]]; then
  REMOVE_DATA=true
elif [[ "$DATA_MODE" == "prompt" ]]; then
  log_bold "Persistent user data (kept by default)"
  [[ -d "$DATA_DIR" ]]    && log_info "Index, config, thumbnails: $DATA_DIR ($(dir_size "$DATA_DIR"))"
  [[ -d "$LOG_DIR" ]]     && log_info "Logs: $LOG_DIR ($(dir_size "$LOG_DIR"))"
  [[ -d "$MODEL_CACHE" ]] && log_info "Model cache (re-downloadable): $MODEL_CACHE ($(dir_size "$MODEL_CACHE"))"
  log_warn "Re-indexing a large library takes hours; a kept index is reused on reinstall."
  # Default "no" when there is no terminal: unattended = keep.
  if ask_tty "Delete this data permanently? [y/N] " no; then
    REMOVE_DATA=true
  fi
fi

if [[ "$REMOVE_DATA" == "true" ]]; then
  log_bold "Removing user data (Tier 2, opted in)..."
  remove_path "$DATA_DIR"
  remove_path "$LOG_DIR"
  remove_path "$MODEL_CACHE"
else
  log_bold "User data kept (Tier 2 default)"
  log_skip "Kept: $DATA_DIR"
  log_skip "Kept: $LOG_DIR"
  log_skip "Kept: $MODEL_CACHE"
  log_info "A reinstall reuses the kept index and config. Delete later by re-running with --remove-data."
fi

log_bold "Media Search Agent has been uninstalled."
