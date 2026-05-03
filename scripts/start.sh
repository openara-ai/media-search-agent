#!/usr/bin/env bash
# Media Search Agent — Start
# Starts FastAPI (which serves the React UI and runs embedded Qdrant). Pre-warms ML models.
#
# Usage: ./scripts/start.sh [--no-prewarm] [--no-browser] [--bind-host <addr>]
#   --no-prewarm        Skip ML model pre-warming (faster start; first search will be slower)
#   --no-browser        Do not open the browser on ready (useful for headless / server use)
#   --bind-host <addr>  Override the host uvicorn binds to (default: from config.yaml, usually 127.0.0.1)
#                       Use 0.0.0.0 to accept connections from other machines on the network.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
MSA_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
export MSA_ROOT
source "$SCRIPT_DIR/lib/common.sh"

# ── Helper: open browser ──────────────────────────────────────────────────────

_open_browser() {
  local url="$1"
  if [[ "$(msa_os)" == "macos" ]]; then
    open "$url" 2>/dev/null || true
  elif is_wsl; then
    # Open in Windows default browser from WSL2
    cmd.exe /c start "$url" 2>/dev/null || \
    powershell.exe -Command "Start-Process '$url'" 2>/dev/null || true
  else
    xdg-open "$url" 2>/dev/null || true
  fi
}

_launch_url() {
  local port="$1"
  echo "http://localhost:$port/?launch=1"
}

# ── Options ───────────────────────────────────────────────────────────────────

PREWARM=true
OPEN_BROWSER=true
BIND_HOST_OVERRIDE=""

while [[ $# -gt 0 ]]; do
  case "$1" in
    --no-prewarm)   PREWARM=false ;;
    --no-browser)   OPEN_BROWSER=false ;;
    --bind-host)
      if [[ $# -lt 2 ]]; then
        echo "Error: --bind-host requires an argument" >&2; exit 1
      fi
      shift; BIND_HOST_OVERRIDE="$1" ;;
    --bind-host=*)  BIND_HOST_OVERRIDE="${1#*=}" ;;
    -h|--help)
      echo "Usage: ./scripts/start.sh [options]"
      echo ""
      echo "Options:"
      echo "  --bind-host <addr>  Override the host uvicorn binds to."
      echo "                      Default: read from config.yaml (usually 127.0.0.1)."
      echo "                      Use 127.0.0.1 to restrict to localhost only."
      echo "                      Use 0.0.0.0 to accept connections from other machines."
      echo "  --no-prewarm        Skip ML model pre-warming (faster start; first search slower)."
      echo "  --no-browser        Do not open the browser when the server is ready."
      echo "  -h, --help          Show this help message and exit."
      exit 0
      ;;
    *) log_warn "Unknown argument: $1" ;;
  esac
  shift
done

# ── Config ────────────────────────────────────────────────────────────────────

API_HOST="127.0.0.1"
API_MODULE="msa_apps.search_api.app:app"

VENV="${MSA_VENV_DIR:-$(msa_venv_dir)}"
CONFIG_PATH="${MSA_CONFIG_PATH:-$(msa_platform_config_path)}"
DATA_DIR="${MSA_DATA_DIR:-$(msa_runtime_data_dir)}"

# ── User-writable state dirs ───────────────────────────────────────────────────
# On macOS system-wide installs (/Applications/...) the app directory is
# root-owned and not writable by the console user.  PID files, the uvicorn log,
# and shell-script logs all go to per-user macOS-idiomatic locations.
# MSA_LOG_DIR is exported so that:
#   - setup_log() in common.sh uses it (priority 1)
#   - the Python layer honours it via _resolve_log_paths (MSA_LOG_DIR env var)
#   - /diagnostics finds shell logs in the same place as msa.log

# msa_log_dir() (common.sh) is the single source of truth for log dir resolution.
# Export MSA_LOG_DIR so the Python layer (_resolve_log_paths) and /diagnostics
# use the same directory as the shell scripts.
export MSA_LOG_DIR="${MSA_LOG_DIR:-$(msa_log_dir)}"

# RUN_DIR lives under MSA_LOG_DIR so that shell PID files (uvicorn.pid) and
# the Python indexer_manager PID file (indexer.pid, also written to
# $MSA_LOG_DIR/run) are always in the same directory — stop.sh can find both.
RUN_DIR="$MSA_LOG_DIR/run"
UVICORN_PID="$RUN_DIR/uvicorn.pid"

# ── Setup ─────────────────────────────────────────────────────────────────────

mkdir -p "$RUN_DIR"
setup_log "launch"

log_bold "Media Search Agent — starting"
log_info "Root:  $MSA_ROOT"
log_info "OS:    $(msa_os) / $(msa_arch)"

if [[ ! -d "$VENV" ]]; then
  die "Python venv not found at $VENV. Follow the setup instructions in README.md first."
fi

API_PORT="$("$VENV/bin/python" - <<PY
from pathlib import Path
import yaml

config_path = Path(r"$CONFIG_PATH")
port = 8000
host = "127.0.0.1"
if config_path.exists():
    with config_path.open("r", encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    api = data.get("api") or {}
    try:
        port = int(api.get("port", port))
    except Exception:
        pass
print(port)
PY
)"

API_HOST="$("$VENV/bin/python" - <<PY
from pathlib import Path
import yaml

config_path = Path(r"$CONFIG_PATH")
host = "127.0.0.1"
if config_path.exists():
    with config_path.open("r", encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    api = data.get("api") or {}
    host = str(api.get("host", host))
print(host)
PY
)"

# CLI flag takes precedence over config.yaml
if [[ -n "$BIND_HOST_OVERRIDE" ]]; then
  API_HOST="$BIND_HOST_OVERRIDE"
fi

# ── Preflight: React UI dist ──────────────────────────────────────────────────

if [[ ! -d "$MSA_ROOT/src/msa_apps/ui/dist" ]]; then
  log_warn "React UI has not been built — run: npm --prefix src/msa_apps/ui run build"
  log_warn "The API will start but http://localhost:$API_PORT will not serve the UI"
fi

# ── Process ownership helpers ─────────────────────────────────────────────────

_pid_cmdline() {
  local pid="$1"
  ps -p "$pid" -o args= 2>/dev/null || true
}

_pid_listens_on_port() {
  local pid="$1"
  local port="$2"
  command -v lsof &>/dev/null || return 1
  lsof -nP -tiTCP:"$port" -sTCP:LISTEN 2>/dev/null | grep -qxF "$pid"
}

_is_msa_uvicorn_pid() {
  local pid="$1"
  local args
  args="$(_pid_cmdline "$pid")"
  [[ "$args" == *"uvicorn"* && "$args" == *"$API_MODULE"* && "$args" == *"--port $API_PORT"* ]]
}

_is_current_msa_uvicorn_pid() {
  local pid="$1"
  local args
  args="$(_pid_cmdline "$pid")"
  [[ "$args" == *"$VENV"* ]] || return 1
  _is_msa_uvicorn_pid "$pid" || return 1
  _pid_listens_on_port "$pid" "$API_PORT"
}

# ── Guard: already running? ───────────────────────────────────────────────────
# Trust the PID file only when it points at this install's uvicorn process and
# that process is actually listening on the configured API port. Stale PID files
# are common after reinstall; PID reuse must never make us exit early or kill a
# foreign user process.

if pid_alive "$UVICORN_PID"; then
  _pid=$(cat "$UVICORN_PID")
  if _is_current_msa_uvicorn_pid "$_pid"; then
    log_ok "Services are already running."
    log_ok "Open: http://localhost:$API_PORT"
    if [[ "$OPEN_BROWSER" == true ]]; then
      _open_browser "$(_launch_url "$API_PORT")"
    fi
    exit 0
  fi
  if _is_msa_uvicorn_pid "$_pid"; then
    log_info "Uvicorn PID $_pid is from another MSA install — will replace"
  else
    log_warn "Ignoring stale uvicorn PID file; PID $_pid is not an MSA uvicorn process"
  fi
fi
unset _pid

# ── Stop any stale services ───────────────────────────────────────────────────
# Ensures we never try to bind a port that's already in use from a prior run.

_stop_stale() {
  local name="$1"
  local pidfile="$2"
  local port="${3:-}"

  _stop_pid() {
    local pid="$1"
    if ! _is_msa_uvicorn_pid "$pid"; then
      log_warn "$name: PID $pid is not an MSA uvicorn process — leaving it alone"
      return 1
    fi
    log_info "$name: MSA process running (PID $pid) — stopping..."
    kill -TERM "$pid" 2>/dev/null || true
    local i=0
    while kill -0 "$pid" 2>/dev/null && [[ $i -lt 10 ]]; do sleep 1; i=$((i+1)); done
    kill -0 "$pid" 2>/dev/null && kill -KILL "$pid" 2>/dev/null || true
  }

  # Kill by PID file only when it still identifies an MSA uvicorn process.
  if [[ -f "$pidfile" ]]; then
    local pid
    pid=$(cat "$pidfile")
    if kill -0 "$pid" 2>/dev/null; then
      _stop_pid "$pid" || true
    fi
    rm -f "$pidfile"
  fi

  # Also stop MSA uvicorn listeners with no PID file. Foreign listeners are left
  # in place so the explicit port-conflict check below can fail safely.
  if [[ -n "$port" ]]; then
    local port_pids
    port_pids=$(lsof -nP -tiTCP:"$port" -sTCP:LISTEN 2>/dev/null || true)
    if [[ -n "$port_pids" ]]; then
      local port_pid
      while IFS= read -r port_pid; do
        [[ -n "$port_pid" ]] || continue
        if _is_msa_uvicorn_pid "$port_pid"; then
          log_info "$name: port $port held by MSA process PID $port_pid — clearing..."
          _stop_pid "$port_pid" || true
        else
          log_warn "$name: port $port is held by foreign PID $port_pid — leaving it alone"
        fi
      done <<< "$port_pids"
    fi
  fi
}

_stop_stale "FastAPI (uvicorn)" "$UVICORN_PID" "$API_PORT"

# ── Port conflict check ───────────────────────────────────────────────────────
# After clearing our own stale processes, if something else is still holding
# the port it's a foreign process. Die with a clear message rather than letting
# uvicorn crash with a buried "Address already in use" error.

if command -v lsof &>/dev/null; then
  # lsof -ti can return multiple PIDs (newline-separated); take the first for the message
  _port_holder=$(lsof -nP -tiTCP:"$API_PORT" -sTCP:LISTEN 2>/dev/null | head -1 || true)
  if [[ -n "$_port_holder" ]]; then
    _holder_cmd=$(ps -p "$_port_holder" -o comm= 2>/dev/null || echo "unknown")
    die "Port $API_PORT is already in use by '$_holder_cmd' (PID $_port_holder). Stop that process first, or use a different port."
  fi
  unset _port_holder _holder_cmd
elif command -v ss &>/dev/null; then
  if ss -tlnp 2>/dev/null | grep -q ":$API_PORT "; then
    die "Port $API_PORT is already in use. Stop the conflicting process first, or use a different port."
  fi
fi

# ── Preflight checks ──────────────────────────────────────────────────────────

log_bold "Preflight checks..."

if [[ ! -f "$CONFIG_PATH" ]]; then
  die "config.yaml not found at $CONFIG_PATH. On a dev checkout run: git checkout config.yaml"
fi

log_ok "Preflight passed"

# ── Runtime environment — bundled binaries ────────────────────────────────────
# $MSA_ROOT/bin holds binaries bundled by the installer (exiftool, mediainfo).
# Prepending to PATH makes them visible to shutil.which() inside Python.
# DYLD_LIBRARY_PATH lets pymediainfo's ctypes loader find libmediainfo.dylib on macOS.
export PATH="$MSA_ROOT/bin:$PATH"
export DYLD_LIBRARY_PATH="$MSA_ROOT/lib${DYLD_LIBRARY_PATH:+:$DYLD_LIBRARY_PATH}"

# Suppress the HuggingFace Hub rate-limit advisory ("unauthenticated requests").
# This is an informational warning that appears even when all models are cached
# locally. Setting verbosity to error hides it without affecting model loading.
export HF_HUB_VERBOSITY=error

# Tell load_config() exactly where config.yaml lives and where to root data paths.
# Without these, the Python settings layer falls back to platform config dirs which
# differ from the install layout and cause path mismatches for thumbnails, index, etc.
export MSA_CONFIG_PATH="$CONFIG_PATH"
export MSA_DATA_DIR="$DATA_DIR"
if [[ -n "$(msa_runtime_cache_dir)" ]]; then
  export MSA_CACHE_DIR="$(msa_runtime_cache_dir)"
fi

# ── Start uvicorn (FastAPI + embedded Qdrant) ─────────────────────────────────

log_bold "Starting FastAPI..."

UVICORN_LOG="$MSA_LOG_DIR/uvicorn.log"
cd "$MSA_ROOT"
"$VENV/bin/uvicorn" "$API_MODULE" \
  --host "$API_HOST" \
  --port "$API_PORT" \
  --log-level warning \
  >> "$UVICORN_LOG" 2>&1 &
UVICORN_PID_VAL=$!
write_pid "$UVICORN_PID" "$UVICORN_PID_VAL"
log_info "uvicorn PID: $UVICORN_PID_VAL"

wait_for_ready "http://localhost:$API_PORT/health" "FastAPI" 90

# ── Pre-warm ML models ────────────────────────────────────────────────────────

if [[ "$PREWARM" == true ]]; then
  # Model setup runs in the background from API startup. Poll setup/status to
  # check whether models are already on disk before firing a search request —
  # the prewarm only makes sense once models are loaded and ready.
  SETUP_STATUS=$(curl -sf "http://localhost:$API_PORT/api/setup/status" 2>/dev/null || true)
  MODELS_READY=$(echo "$SETUP_STATUS" | python3 -c "import sys,json; print(json.load(sys.stdin).get('ready', False))" 2>/dev/null || echo "False")
  if [[ "$MODELS_READY" == "True" ]]; then
    log_bold "Pre-warming ML models (loading into memory)..."
    PREWARM_RESPONSE=$(curl -sf -X POST "http://localhost:$API_PORT/search" \
      -H "Content-Type: application/json" \
      -d '{"q": "warmup", "top_k": 1, "min_score": 0.0}' 2>&1 || true)
    log_ok "Models pre-warmed"
  else
    log_skip "Model pre-warming skipped — models not yet ready (setup still in progress or first launch)"
  fi
else
  log_skip "Model pre-warming skipped (--no-prewarm)"
fi

# ── Ready ─────────────────────────────────────────────────────────────────────

log_ok "───────────────────────────────────────────"
log_ok "Media Search Agent is ready"
log_ok "  UI:   http://localhost:$API_PORT"
log_ok "  Log:  $MSA_LOG_FILE"
log_ok "  Stop: ./scripts/stop.sh"
log_ok "───────────────────────────────────────────"

if [[ "$OPEN_BROWSER" == true ]]; then
  _open_browser "$(_launch_url "$API_PORT")"
fi
