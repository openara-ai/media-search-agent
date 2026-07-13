#!/usr/bin/env bash
set -euo pipefail

# Real-media BVT against the installed **Tauri desktop** artifact on macOS.
#
# Unlike validate-installed-bundle-macos.sh (which validates the retiring shell
# bundle via `msa api start`), this drives the real shippable desktop app:
# install the .dmg, LAUNCH the app, and run the same real-media index + search
# assertions against the app's own backend — reached over its ephemeral port.
#
# The test bodies are IDENTICAL to the bundle path (tests/real_media/*.py); the
# only differences are: install mechanic (.dmg mount+copy), backend serving
# (launch the app, not `msa api start`), and port discovery (the app binds an
# ephemeral port; the sidecar publishes it to LOG_DIR/sidecar-port — the file
# read below; issue #172 resolved).
#
# Usage: validate-installed-desktop-macos.sh <path-to.dmg>
#
# Env knobs (local dev speed; unset in CI so CI does a real fresh provision):
#   MSA_BVT_REUSE_RUNTIME=1   Reuse an existing provisioned runtime (venv + 1.8G
#                             model cache) from $MSA_BVT_SRC_HOME by symlink, so
#                             the run skips provisioning/downloads. Teardown still
#                             targets only isolated-HOME processes, so the source
#                             install is never touched.
#   MSA_BVT_SRC_HOME=<dir>    Source HOME to reuse runtime from (default: the
#                             invoking user's $HOME, captured before isolation).

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../../.." && pwd)"
DMG_PATH="${1:-}"
REUSE_RUNTIME="${MSA_BVT_REUSE_RUNTIME:-0}"
SRC_HOME="${MSA_BVT_SRC_HOME:-$HOME}"   # capture BEFORE we override HOME below

if [[ "$REUSE_RUNTIME" != "1" ]]; then
  [[ -n "$DMG_PATH" && -f "$DMG_PATH" ]] || { echo "Usage: $0 <path-to.dmg>  (or MSA_BVT_REUSE_RUNTIME=1)" >&2; exit 1; }
fi

# ── Step 1: isolated HOME (fresh user-space; nothing touches the real install) ──
RUN_ROOT="${RUNNER_TEMP:-$(mktemp -d)}"
export HOME="$RUN_ROOT/msa-desktop-home"
mkdir -p "$HOME/Applications"

# The shell no longer auto-updates (ADR-012 §5: no automatic update check at launch), so the BVT
# structurally validates THIS freshly-built .app — there is nothing to self-update, no gate needed.

# Desktop / Tauri layout (identifier-keyed app-private dir per ADR-009).
IDENT="ai.openara.mediasearchagent"
APPSUP="$HOME/Library/Application Support"
APPPRIV="$APPSUP/$IDENT"                       # venv / python / uv-cache
DATA_DIR="$APPSUP/MediaSearchAgent"            # config / index / qdrant / data
CACHE_DIR="$HOME/Library/Caches/MediaSearchAgent"
LOG_DIR="$HOME/Library/Logs/MediaSearchAgent"
DESKTOP_LOG="$LOG_DIR/msa-desktop.log"
PORT_FILE="$LOG_DIR/sidecar-port"   # authoritative published port (issue #172); scrape is fallback
CONFIG_PATH="$DATA_DIR/config.yaml"
# NB: do not pre-create CACHE_DIR/APPPRIV — reuse mode symlinks them (a pre-made
# dir would make `ln -s` nest the link inside it).
mkdir -p "$APPSUP" "$DATA_DIR" "$LOG_DIR" "$HOME/Library/Caches"
[[ "$REUSE_RUNTIME" == "1" ]] || mkdir -p "$CACHE_DIR"

# ── Step 2: stage the real-media payload outside the app ──
TEST_ROOT="$RUN_ROOT/msa-real-media-tests"
mkdir -p "$TEST_ROOT"
cp -R "$REPO_ROOT/tests/real_media/." "$TEST_ROOT/"
find "$TEST_ROOT" -type d -name '__pycache__' -prune -exec rm -rf {} +
FIXTURE_ROOT="$TEST_ROOT/fixtures"
LEDGER_DIR="$RUN_ROOT/ranker-ledger"

# ── Step 3: install the real desktop artifact ──
if [[ "$REUSE_RUNTIME" == "1" ]]; then
  echo "== REUSE mode: symlinking provisioned runtime + model cache from $SRC_HOME =="
  ln -s "$SRC_HOME/Library/Application Support/$IDENT" "$APPPRIV"
  ln -s "$SRC_HOME/Library/Caches/MediaSearchAgent" "$CACHE_DIR"
  # Reuse the installed Tauri .app shell (its exe respects the overridden HOME).
  # The desktop app's exe is MediaSearchAgent (tauri.conf.json mainBinaryName; #163) —
  # probe for it so a stale/partial bundle without the executable is rejected.
  APP=""
  for cand in "/Applications/MediaSearchAgent.app" "$SRC_HOME/Applications/MediaSearchAgent.app"; do
    [[ -x "$cand/Contents/MacOS/MediaSearchAgent" ]] && { APP="$cand"; break; }
  done
  [[ -n "$APP" ]] || { echo "no installed MediaSearchAgent.app (with MacOS/MediaSearchAgent exe) found" >&2; exit 1; }
else
  echo "== Installing desktop artifact: $DMG_PATH =="
  MNT="$(mktemp -d)"
  hdiutil attach "$DMG_PATH" -nobrowse -mountpoint "$MNT" >/dev/null
  cp -R "$MNT/MediaSearchAgent.app" "$HOME/Applications/"
  hdiutil detach "$MNT" >/dev/null || true
  APP="$HOME/Applications/MediaSearchAgent.app"
fi

APP_EXE="$APP/Contents/MacOS/MediaSearchAgent"
APP_RES="$APP/Contents/Resources"
UV_BIN="$APP_RES/bin/uv"
VENV_PY="$APPPRIV/.venv/bin/python"
MSA_BIN="$APPPRIV/.venv/bin/msa"
API_LOG="$LOG_DIR/runtime-api.log"
[[ -x "$APP_EXE" ]] || { echo "app executable not found: $APP_EXE" >&2; exit 1; }

# ── teardown: kill the app we launched + reap any sidecar under THIS home ──
APP_PID=""
launch_app() {   # -> sets APP_PID, exports DISCOVERED_PORT
  : >"$DESKTOP_LOG" 2>/dev/null || true
  rm -f "$PORT_FILE" 2>/dev/null || true   # drop any stale published port from a prior launch
  HOME="$HOME" UV_CACHE_DIR="$APPPRIV/uv-cache" "$APP_EXE" >>"$LOG_DIR/app-stdout.log" 2>&1 &
  APP_PID=$!
  local port=""
  for _ in $(seq 1 300); do   # up to ~5 min (fresh provision can be slow)
    if ! kill -0 "$APP_PID" 2>/dev/null; then echo "FAIL: app exited during startup" >&2; tail -40 "$DESKTOP_LOG" >&2 || true; return 1; fi
    # The sidecar publishes its bound port to LOG_DIR/sidecar-port (issue #172); the /health
    # "ready" gate below still confirms liveness, so a stale file can never green-light.
    port="$(tr -dc '0-9' <"$PORT_FILE" 2>/dev/null || true)"
    if [[ -n "$port" ]] && curl -fsS "http://127.0.0.1:$port/health" 2>/dev/null | grep -q '"status":"ready"'; then
      DISCOVERED_PORT="$port"; echo "backend ready on ephemeral port $port"; return 0
    fi
    sleep 1
  done
  echo "FAIL: backend did not become ready" >&2; tail -60 "$DESKTOP_LOG" >&2 || true; return 1
}
quit_app() {     # graceful SIGTERM to the owned PID; verify no orphan under this home
  [[ -n "$APP_PID" ]] && kill -TERM "$APP_PID" 2>/dev/null || true
  for _ in $(seq 1 10); do kill -0 "$APP_PID" 2>/dev/null || break; sleep 1; done
  # Orphan check: any process whose args reference THIS isolated home is ours to reap.
  local orphans; orphans="$(pgrep -f "$HOME/Library/Application Support/$IDENT" 2>/dev/null || true)"
  if [[ -n "$orphans" ]]; then
    echo "WARN: reaping orphaned sidecar(s) under isolated home: $orphans" >&2
    echo "$orphans" | xargs kill -9 2>/dev/null || true
    return 1
  fi
  return 0
}
ORPHAN_SEEN=0
cleanup() { quit_app || ORPHAN_SEEN=1; }
trap cleanup EXIT

# ── Step 4: first launch → provisions (or serves, in reuse mode) ──
echo "== Launch #1 (provision / warm) =="
DISCOVERED_PORT=""
launch_app
quit_app || { echo "NOTE: orphan after launch #1 (see #171)"; }   # bring app down so the indexer can take the Qdrant lock

# ── Step 5: pytest to a SCRATCH dir (venv has no pip; and in reuse mode the venv
#            is shared with the real install, so we must not modify it) ──
"$MSA_BIN" --help >/dev/null
PYTEST_DIR="$RUN_ROOT/pytest-libs"
"$UV_BIN" pip install --python "$VENV_PY" --target "$PYTEST_DIR" pytest >/dev/null

# ── Step 6: config → fixtures + CPU + object detection (same as bundle path) ──
"$VENV_PY" - <<'PY' "$CONFIG_PATH" "$FIXTURE_ROOT" "$LEDGER_DIR"
from pathlib import Path
import sys, yaml
cfg = Path(sys.argv[1]); fixtures, ledger = sys.argv[2], sys.argv[3]
data = yaml.safe_load(cfg.read_text(encoding="utf-8")) if cfg.exists() else {}
data = data or {}
data["media_sources"] = [{"name": "Real Media Fixtures", "path": fixtures, "read_only": True}]
data["enable_object_detection"] = True
data["enable_video_object_detection"] = True
r = data.get("ranker") or {}; r["event_logging"] = True; r["ledger_dir"] = ledger; data["ranker"] = r
cfg.parent.mkdir(parents=True, exist_ok=True)
cfg.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
PY

export MSA_DEVICE="cpu"
export PATH="$APP_RES/bin:$PATH"
PYTHONPATH="$PYTEST_DIR" "$VENV_PY" -m pytest "$TEST_ROOT/test_real_media_fixtures.py" -v -m "not slow"

# ── Step 7: index the fixtures (app NOT serving → no Qdrant lock contention) ──
echo "== Indexing fixtures via installed venv msa (app down) =="
"$MSA_BIN" index run --config "$CONFIG_PATH" --media-source-override "$FIXTURE_ROOT" --export-to-qdrant

# ── Step 8: relaunch the app to SERVE, run runtime suite against its backend ──
echo "== Launch #2 (serve) =="
launch_app
export MSA_REALDATA_WORKSPACE="$DATA_DIR"
export MSA_REALDATA_SQLITE_PATH="$DATA_DIR/index/media.sqlite"
export MSA_REALDATA_FAISS_PATH="$DATA_DIR/index/image_vec.faiss"
export MSA_REALDATA_FACE_FAISS_PATH="$DATA_DIR/index/face_vec.faiss"
export MSA_REALDATA_THUMB_DIR="$DATA_DIR/data/thumbnails"
export MSA_REALDATA_FACE_THUMB_DIR="$DATA_DIR/data/face_thumbnails"
export MSA_REALDATA_FIXTURE_ROOT="$FIXTURE_ROOT"
export MSA_REALDATA_BASE_URL="http://127.0.0.1:$DISCOVERED_PORT"
export MSA_CACHE_DIR="$CACHE_DIR"
if ls "$APP_RES"/backend/wheels/msa_ranker-*.whl >/dev/null 2>&1; then
  export MSA_REALDATA_LEDGER_DIR="$LEDGER_DIR"
fi

echo "Warming up CLIP text encoder via sentinel POST /search"
curl -fsS --max-time 180 -X POST -H "Content-Type: application/json" \
  -d '{"q":"warmup"}' "$MSA_REALDATA_BASE_URL/search" >/dev/null \
  || { echo "Sentinel /search warmup failed; see $DESKTOP_LOG" >&2; exit 1; }

RUNTIME_RC=0
PYTHONPATH="$PYTEST_DIR" "$VENV_PY" -m pytest "$TEST_ROOT/test_real_media_runtime.py" -v || RUNTIME_RC=$?

quit_app || ORPHAN_SEEN=1
trap - EXIT
if [[ "$ORPHAN_SEEN" == "1" ]]; then
  echo "WARN: an orphaned sidecar was reaped during teardown — see issue #171" >&2
fi
echo "== desktop BVT complete (runtime rc=$RUNTIME_RC, orphan_seen=$ORPHAN_SEEN) =="
exit $RUNTIME_RC
