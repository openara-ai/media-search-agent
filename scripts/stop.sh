#!/usr/bin/env bash
# Media Search Agent — Stop
# Gracefully stops the indexer and FastAPI (uvicorn).
# Qdrant is embedded (no Docker container to stop).
#
# Usage: ./scripts/stop.sh [-h|--help]

set -euo pipefail

for arg in "$@"; do
  case "$arg" in
    -h|--help)
      echo "Usage: ./scripts/stop.sh"
      echo ""
      echo "Gracefully stops the indexer and FastAPI (uvicorn)."
      echo "Qdrant is embedded — no separate process to stop."
      echo ""
      echo "Options:"
      echo "  -h, --help  Show this help message and exit."
      exit 0
      ;;
  esac
done

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
MSA_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
source "$SCRIPT_DIR/lib/common.sh"

# msa_log_dir() (common.sh) is the single source of truth — same resolution
# as start.sh — so RUN_DIR points to the same directory start.sh used.
MSA_LOG_DIR="${MSA_LOG_DIR:-$(msa_log_dir)}"
RUN_DIR="$MSA_LOG_DIR/run"

UVICORN_PID="$RUN_DIR/uvicorn.pid"
INDEXER_PID="$RUN_DIR/indexer.pid"

setup_log "stop"
log_bold "Media Search Agent — stopping"

stop_service() {
  local name="$1"
  local pidfile="$2"
  local wait_seconds="${3:-10}"

  if [[ ! -f "$pidfile" ]]; then
    log_skip "$name: no PID file found (already stopped?)"
    return 0
  fi

  local pid
  pid=$(cat "$pidfile")

  if ! kill -0 "$pid" 2>/dev/null; then
    log_skip "$name: process $pid is not running (stale PID file)"
    rm -f "$pidfile"
    return 0
  fi

  log_info "$name: sending SIGTERM to PID $pid..."
  kill -TERM "$pid" 2>/dev/null || true

  local elapsed=0
  while kill -0 "$pid" 2>/dev/null; do
    sleep 1
    elapsed=$((elapsed + 1))
    if [[ $elapsed -ge $wait_seconds ]]; then
      log_warn "$name: did not exit after ${wait_seconds}s — sending SIGKILL"
      kill -KILL "$pid" 2>/dev/null || true
      break
    fi
  done

  rm -f "$pidfile"
  log_ok "$name: stopped (${elapsed}s)"
}

# Stop indexer first, then API
stop_service "Indexer" "$INDEXER_PID" 15
stop_service "FastAPI (uvicorn)"   "$UVICORN_PID" 10

log_ok "All services stopped."
