#!/usr/bin/env bash
# scripts/lib/common.sh — Shared functions for all MSA scripts.
# Source this file; do not execute it directly.
#
# Usage: source "$(dirname "${BASH_SOURCE[0]}")/lib/common.sh"

# ── Colours ──────────────────────────────────────────────────────────────────

_CLR_RESET='\033[0m'
_CLR_GREEN='\033[0;32m'
_CLR_YELLOW='\033[0;33m'
_CLR_RED='\033[0;31m'
_CLR_BOLD='\033[1m'
_CLR_DIM='\033[2m'

if [[ ! -t 1 || -n "${NO_COLOR:-}" || "${TERM:-}" == "dumb" || "${CLICOLOR:-1}" == "0" ]]; then
  _CLR_RESET=''
  _CLR_GREEN=''
  _CLR_YELLOW=''
  _CLR_RED=''
  _CLR_BOLD=''
  _CLR_DIM=''
fi

# ── Logging ───────────────────────────────────────────────────────────────────

# msa_log_dir — canonical log directory resolution (single source of truth).
#
# Resolution order:
#   1. $MSA_LOG_DIR env var — exported by start.sh; also honoured by the Python
#      layer (_resolve_log_paths) and /diagnostics, keeping all logs co-located.
#   2. macOS installed app heuristic — covers both the pkg install
#      (/Applications/MediaSearchAgent.app) and the shell install
#      (~/Applications/MediaSearchAgent.app). Both are .app bundles and both
#      use ~/Library/Logs/MediaSearchAgent as the user-writable log location.
#   3. Dev / Linux / WSL2 default — $MSA_ROOT/logs (app-local, always writable).
#
# start.sh and stop.sh call this to set MSA_LOG_DIR before any mkdir/setup_log.
# setup_log() calls it internally so postinstall scripts get the right dir too.
msa_log_dir() {
  if [[ -n "${MSA_LOG_DIR:-}" ]]; then
    echo "$MSA_LOG_DIR"
  elif [[ "$(uname -s)" == "Darwin" && "${MSA_ROOT:-}" == */MediaSearchAgent.app/Contents/Resources ]]; then
    echo "$HOME/Library/Logs/MediaSearchAgent"
  else
    echo "${MSA_ROOT:-$HOME/media-search-agent}/logs"
  fi
}

msa_is_macos_system_install() {
  [[ "$(uname -s)" == "Darwin" && "${MSA_ROOT:-}" == /Applications/* ]]
}

msa_platform_config_dir() {
  if msa_is_macos_system_install; then
    echo "$HOME/Library/Application Support/MediaSearchAgent"
  else
    echo "${MSA_ROOT:-$HOME/media-search-agent}"
  fi
}

msa_platform_config_path() {
  local config_dir
  config_dir="$(msa_platform_config_dir)"
  echo "$config_dir/config.yaml"
}

msa_runtime_data_dir() {
  if msa_is_macos_system_install; then
    echo "$HOME/Library/Application Support/MediaSearchAgent"
  else
    echo "${MSA_ROOT:-$HOME/media-search-agent}"
  fi
}

msa_runtime_cache_dir() {
  if msa_is_macos_system_install; then
    echo "$HOME/Library/Caches/MediaSearchAgent"
  else
    echo "${MSA_CACHE_DIR:-}"
  fi
}

msa_venv_dir() {
  if msa_is_macos_system_install; then
    echo "$HOME/Library/Application Support/MediaSearchAgent/.venv"
  else
    echo "${MSA_ROOT:-$HOME/media-search-agent}/.venv"
  fi
}

MSA_LOG_FILE=""

# Call setup_log "<prefix>" before using log_* functions.
# Creates a timestamped log file under msa_log_dir() and sets MSA_LOG_FILE.
setup_log() {
  local prefix="${1:-msa}"
  local log_dir
  log_dir="$(msa_log_dir)"
  mkdir -p "$log_dir"
  MSA_LOG_FILE="$log_dir/${prefix}-$(date '+%Y-%m-%d_%H%M%S').log"
  log_info "Log: $MSA_LOG_FILE"
}

_log() {
  local level="$1"; shift
  local colour="$1"; shift
  local msg="$*"
  local ts
  ts="$(date '+%Y-%m-%d %H:%M:%S')"
  local plain="$ts [$level] $msg"
  printf "${colour}%s${_CLR_RESET}\n" "$plain"
  [[ -n "$MSA_LOG_FILE" ]] && echo "$plain" >> "$MSA_LOG_FILE"
}

log_info()  { _log "INFO " "$_CLR_RESET"   "$@"; }
log_ok()    { _log "OK   " "$_CLR_GREEN"   "$@"; }
log_skip()  { _log "SKIP " "$_CLR_DIM"     "$@"; }
log_warn()  { _log "WARN " "$_CLR_YELLOW"  "$@"; }
log_error() { _log "ERROR" "$_CLR_RED"     "$@"; }
log_bold()  { _log ".... " "$_CLR_BOLD"    "$@"; }

# Log and exit with error
die() {
  log_error "$*"
  [[ -n "$MSA_LOG_FILE" ]] && log_error "Full log: $MSA_LOG_FILE"
  exit 1
}

# ── OS / Platform detection ───────────────────────────────────────────────────

# Returns: linux | macos
msa_os() {
  case "$(uname -s)" in
    Darwin) echo "macos" ;;
    Linux)  echo "linux" ;;
    *)      echo "unknown" ;;
  esac
}

# Returns: arm64 | x86_64
msa_arch() {
  case "$(uname -m)" in
    arm64|aarch64) echo "arm64" ;;
    x86_64|amd64)  echo "x86_64" ;;
    *)             echo "unknown" ;;
  esac
}

# Returns true if running inside WSL2
is_wsl() {
  [[ -f /proc/version ]] && grep -qi microsoft /proc/version
}

# ── Path resolution ───────────────────────────────────────────────────────────

# Resolve MSA_ROOT from the location of the calling script.
# The scripts/ directory is one level below the repo root.
resolve_msa_root() {
  local script_dir
  script_dir="$(cd "$(dirname "${BASH_SOURCE[1]}")" && pwd)"
  echo "$(cd "$script_dir/.." && pwd)"
}

# ── Process / PID helpers ─────────────────────────────────────────────────────

# Write a PID file
write_pid() {
  local pidfile="$1"
  local pid="$2"
  mkdir -p "$(dirname "$pidfile")"
  echo "$pid" > "$pidfile"
}

# Return true if the process in a PID file is still alive
pid_alive() {
  local pidfile="$1"
  [[ -f "$pidfile" ]] && kill -0 "$(cat "$pidfile")" 2>/dev/null
}

# Read PID from file or return empty
read_pid() {
  local pidfile="$1"
  [[ -f "$pidfile" ]] && cat "$pidfile" || echo ""
}

# ── Network helpers ───────────────────────────────────────────────────────────

# Wait for a TCP port to accept connections.
# Usage: wait_for_port <port> <service-name> [timeout-seconds]
wait_for_port() {
  local port="$1"
  local name="$2"
  local timeout="${3:-30}"
  local elapsed=0
  log_info "Waiting for $name on port $port..."
  while ! nc -z localhost "$port" 2>/dev/null; do
    sleep 1
    elapsed=$((elapsed + 1))
    if [[ $elapsed -ge $timeout ]]; then
      die "$name did not open port $port within ${timeout}s"
    fi
  done
  log_ok "$name is accepting connections on port $port (${elapsed}s)"
}

# Wait for an HTTP endpoint to return 200.
# Usage: wait_for_http <url> <service-name> [timeout-seconds]
wait_for_http() {
  local url="$1"
  local name="$2"
  local timeout="${3:-60}"
  local elapsed=0
  log_info "Waiting for $name at $url..."
  while ! curl -sf "$url" >/dev/null 2>&1; do
    sleep 2
    elapsed=$((elapsed + 2))
    if [[ $elapsed -ge $timeout ]]; then
      die "$name did not respond at $url within ${timeout}s"
    fi
  done
  log_ok "$name is healthy (${elapsed}s)"
}

# Wait for a health endpoint to return {"status":"ready"}.
# Falls back gracefully if jq is unavailable (treats any 200 as ready).
# Usage: wait_for_ready <url> <service-name> [timeout-seconds]
wait_for_ready() {
  local url="$1"
  local name="$2"
  local timeout="${3:-90}"
  local elapsed=0
  log_info "Waiting for $name to be ready at $url..."
  while true; do
    local body
    body=$(curl -sf "$url" 2>/dev/null || true)
    if [[ -n "$body" ]]; then
      if command -v jq &>/dev/null; then
        local status
        status=$(echo "$body" | jq -r '.status // empty' 2>/dev/null || true)
        [[ "$status" == "ready" ]] && break
      else
        # jq not available — any 200 response is good enough
        break
      fi
    fi
    sleep 2
    elapsed=$((elapsed + 2))
    if [[ $elapsed -ge $timeout ]]; then
      die "$name did not become ready at $url within ${timeout}s"
    fi
  done
  log_ok "$name is ready (${elapsed}s)"
}

# ── Idempotency helpers ───────────────────────────────────────────────────────

# Run a block only if a command is missing from PATH.
# Usage: ensure_cmd <cmd> <description> <install-block>
ensure_cmd() {
  local cmd="$1"
  local desc="$2"
  if command -v "$cmd" &>/dev/null; then
    log_skip "$desc already installed ($(command -v "$cmd"))"
    return 0
  fi
  log_info "Installing: $desc"
  return 1
}

# ── Download helper ───────────────────────────────────────────────────────────

# Download a URL to a destination file, with progress logging.
download() {
  local url="$1"
  local dest="$2"
  local desc="${3:-$url}"
  log_info "Downloading $desc..."
  if command -v curl &>/dev/null; then
    curl -fsSL --progress-bar "$url" -o "$dest" 2>&1 | \
      while IFS= read -r line; do log_info "  $line"; done || \
      die "Download failed: $url"
  else
    die "curl not found — cannot download $desc"
  fi
  log_ok "Downloaded: $(basename "$dest")"
}
