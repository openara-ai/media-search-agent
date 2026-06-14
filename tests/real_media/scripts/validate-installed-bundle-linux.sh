#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../../.." && pwd)"

BUNDLE_PATH="${1:-}"

if [[ -z "$BUNDLE_PATH" ]]; then
  echo "Usage: $0 <bundle-tar.gz>" >&2
  exit 1
fi

[[ -f "$BUNDLE_PATH" ]] || { echo "Bundle not found: $BUNDLE_PATH" >&2; exit 1; }

# Step 1: Create an isolated HOME so the installer behaves like a fresh
# end-user install and does not reuse any developer machine state. The
# runtime ignores XDG_* on Linux (per ADR-009), so HOME alone is enough
# to redirect every platform default.
RUN_ROOT="${RUNNER_TEMP:-$(mktemp -d)}"
export HOME="$RUN_ROOT/msa-linux-home"
mkdir -p "$HOME"

# Step 2: Stage the real-media validation payload outside the installed app.
# The bundle should provide only the product; tests/fixtures stay external.
TEST_ROOT="$RUN_ROOT/msa-real-media-tests"
mkdir -p "$TEST_ROOT"
cp -R "$REPO_ROOT/tests/real_media/." "$TEST_ROOT/"
find "$TEST_ROOT" -type d -name '__pycache__' -prune -exec rm -rf {} +

# Step 3: Run the real bundle installer exactly the way CI is validating it.
echo "Bundle: $BUNDLE_PATH"
bash "$REPO_ROOT/installer/macos/shell/install.sh" --bundle "$BUNDLE_PATH" --skip-autostart

# Step 4: Resolve the installed launcher, config, and runtime directories.
APP_DIR="$HOME/.local/share/MediaSearchAgent"
DATA_DIR="$HOME/.local/share/MediaSearchAgent"
CONFIG_PATH="$HOME/.config/MediaSearchAgent/config.yaml"
LOG_DIR="$HOME/.local/share/MediaSearchAgent/logs"
FIXTURE_ROOT="$TEST_ROOT/fixtures"
API_PORT=18080
LEDGER_DIR="$RUN_ROOT/ranker-ledger"
UV_BIN="$APP_DIR/bin/uv"
VENV_PY="$APP_DIR/.venv/bin/python"
MSA_BIN="$HOME/.local/bin/msa"
API_LOG="$LOG_DIR/runtime-api.log"
API_PID=""
API_STARTED=0

cleanup() {
  if [[ "$API_STARTED" -eq 1 ]]; then
    "$MSA_BIN" api stop >/dev/null 2>&1 || true
  fi
  if [[ -n "$API_PID" ]] && kill -0 "$API_PID" 2>/dev/null; then
    wait "$API_PID" 2>/dev/null || true
  fi
}
trap cleanup EXIT

# Step 5: Add the only test-only dep BVT needs (pytest) on top of the
# bundle venv. The bundle's install.sh already produced a venv with the
# full runtime stack — torch+torchvision+facenet-pytorch were installed
# via the strip+--no-deps pattern, so transformers' RT-DETR imports work.
# Layering tests/requirements-ci.txt on top would re-resolve that tree
# (with facenet-pytorch as a top-level constraint pulling old torch and
# torchvision pins), defeating the point of validating the bundle's
# runtime. real_media tests only import pytest + stdlib + msa_*.
"$MSA_BIN" --help >/dev/null
"$UV_BIN" pip install --python "$VENV_PY" pytest

# S-5.4: IF the bundle shipped the learned-reranker serving library (zero-dep wheel),
# confirm it installed into the bundle venv and imports. Public-mirror bundles ship
# without the (private) wheel — skip cleanly there. Serving stays flag-off (heuristic).
if ls "$APP_DIR"/wheels/msa_ranker-*.whl >/dev/null 2>&1; then
  "$VENV_PY" -c "import msa_ranker.serving, msa_ranker.features, msa_ranker.model; print('msa_ranker serving lib OK')"
else
  echo "(no msa_ranker wheel in bundle — serving-lib check skipped; heuristic-only bundle)"
fi

# Step 6: Write an isolated installed-app config that points at the staged
# fixture tree and a CI-only API port.
mkdir -p "$(dirname "$CONFIG_PATH")"
"$VENV_PY" - <<'PY' "$CONFIG_PATH" "$FIXTURE_ROOT" "$API_PORT" "$LEDGER_DIR"
from pathlib import Path
import sys
import yaml

config_path = Path(sys.argv[1])
fixture_root = sys.argv[2]
api_port = int(sys.argv[3])
ledger_dir = sys.argv[4]
data = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
data["media_sources"] = [{"name": "Real Media Fixtures", "path": fixture_root, "read_only": True}]
api = data.get("api") or {}
api["host"] = "127.0.0.1"
api["port"] = api_port
data["api"] = api
# S-5.4: pin the ranker event ledger to a known dir so the runtime BVT can assert
# end-to-end label capture (search -> shown -> open). Logging is on by default.
ranker = data.get("ranker") or {}
ranker["event_logging"] = True
ranker["ledger_dir"] = ledger_dir
data["ranker"] = ranker
# BVT runs on CPU (MSA_DEVICE=cpu, see Step 7), where the bundled config
# default `enable_object_detection: auto` resolves to "skip". Force it on
# so the indexer actually populates per-keyframe tags that the runtime
# tests assert on (test_video_media_*_includes_keyframe_tags).
data["enable_object_detection"] = True
data["enable_video_object_detection"] = True
config_path.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
PY

# Step 7: Put the installed media tools on PATH and run the staged fixture
# suite from the installed Python environment. The runtime's platform
# config dir matches the installer location ($HOME/.config/MediaSearchAgent
# on Linux per ADR-009), and the runtime intentionally ignores XDG_* on
# Linux — so the HOME override from Step 1 is sufficient and pytest
# inherits the redirected platform defaults. MSA_DEVICE=cpu pins the
# runner to CPU regardless of any GPU detection.
export MSA_DEVICE="cpu"
export PATH="$APP_DIR/bin:$PATH"
echo "exiftool: $(which exiftool) - $(exiftool -ver)"
"$VENV_PY" -m pytest "$TEST_ROOT/test_real_media_fixtures.py" -v -m "not slow"

# Step 8a: Indexer lifecycle stress — interrupted-run clean-exit gate.
# Start a real `msa index run` against the staged fixtures, wait for the
# first BATCH_COMMIT line (proves CLIP loaded and a per-file commit landed),
# then `msa index stop` — which writes the stop sentinel and on POSIX also
# sends SIGTERM, then blocks with progress until the indexer exits cleanly.
# Asserts: clean exit (rc=0) and NO "forrtl: error" in the log. The forrtl
# check is the regression gate for the Windows Intel-Fortran abort path the
# stop sentinel was introduced to avoid (PR #121, WIN-006). See
# internal/docs/testing/INDEXER_LIFECYCLE_STRESS.md for terminology and
# per-assertion rationale.
mkdir -p "$LOG_DIR"
LIFECYCLE_LOG="$LOG_DIR/indexer-lifecycle.log"
# Lower the commit-batch threshold so the small fixture set reliably crosses
# a commit boundary before we issue the stop.
export MSA_INDEXER_COMMIT_BATCH_FILES=2
export MSA_INDEXER_COMMIT_BATCH_SECONDS=2

"$MSA_BIN" index run --config "$CONFIG_PATH" --media-source-override "$FIXTURE_ROOT" \
  >"$LIFECYCLE_LOG" 2>&1 &
LIFECYCLE_PID=$!
# Up to 240s for BATCH_COMMIT (cold CLIP load can take 30-60s on CPU runners).
for _ in $(seq 1 480); do
  if grep -q "BATCH_COMMIT" "$LIFECYCLE_LOG" 2>/dev/null; then break; fi
  if ! kill -0 "$LIFECYCLE_PID" 2>/dev/null; then
    echo "FAIL: indexer exited before BATCH_COMMIT" >&2
    tail -n 200 "$LIFECYCLE_LOG" >&2 || true
    exit 1
  fi
  sleep 0.5
done
if ! grep -q "BATCH_COMMIT" "$LIFECYCLE_LOG" 2>/dev/null; then
  echo "FAIL: no BATCH_COMMIT within 240s" >&2
  tail -n 200 "$LIFECYCLE_LOG" >&2 || true
  kill "$LIFECYCLE_PID" 2>/dev/null || true
  exit 1
fi

# Don't let `set -e` kill us with a stray background indexer still running
# if msa index stop returns non-zero — kill the background process first
# so subsequent CI steps don't inherit a process holding locks.
STOP_RC=0
"$MSA_BIN" index stop --config "$CONFIG_PATH" --wait 60 --require-running || STOP_RC=$?
if [[ $STOP_RC -ne 0 ]]; then
  echo "FAIL: msa index stop returned rc=$STOP_RC; killing background indexer (PID $LIFECYCLE_PID)" >&2
  kill "$LIFECYCLE_PID" 2>/dev/null || true
  wait "$LIFECYCLE_PID" 2>/dev/null || true
  tail -n 200 "$LIFECYCLE_LOG" >&2
  exit 1
fi
LIFECYCLE_RC=0
wait "$LIFECYCLE_PID" || LIFECYCLE_RC=$?
if [[ $LIFECYCLE_RC -ne 0 ]]; then
  echo "FAIL: indexer did not exit cleanly after \`msa index stop\` (rc=$LIFECYCLE_RC)" >&2
  tail -n 200 "$LIFECYCLE_LOG" >&2
  exit 1
fi
if grep -q "forrtl: error" "$LIFECYCLE_LOG"; then
  echo "FAIL: 'forrtl: error' in indexer log — the stop sentinel path didn't" >&2
  echo "      engage. See WIN-006 in BUGS_AND_GOTCHAS.md." >&2
  tail -n 200 "$LIFECYCLE_LOG" >&2
  exit 1
fi
# Cooperative stop must include Qdrant export of the batches that were
# durably committed before the stop. The indexer's pipeline.run_index
# finalisation runs the export when stop_event is set AND files were
# committed. Without this, SQLite and Qdrant fall out of sync — the API
# can return search hits backed by SQLite metadata that has no Qdrant
# vector entry.
if ! grep -q "Qdrant image/video export complete" "$LIFECYCLE_LOG"; then
  echo "FAIL: cooperative stop did not complete Qdrant export — SQLite and" >&2
  echo "      Qdrant are now out of sync. The cooperative-stop path in" >&2
  echo "      pipeline.py must run the export on stop_event with local changes." >&2
  tail -n 200 "$LIFECYCLE_LOG" >&2
  exit 1
fi
unset MSA_INDEXER_COMMIT_BATCH_FILES
unset MSA_INDEXER_COMMIT_BATCH_SECONDS

# Step 8b: Resume the indexer to finish the remaining fixtures and export to
# Qdrant. Per-batch durability from Step 8a means rows committed before the
# stop are now skipped. The runtime tests in Step 9 then assert the indexed
# state and API contracts.
"$MSA_BIN" index run --config "$CONFIG_PATH" --media-source-override "$FIXTURE_ROOT" --export-to-qdrant

export MSA_REALDATA_WORKSPACE="$DATA_DIR"
export MSA_REALDATA_SQLITE_PATH="$DATA_DIR/index/media.sqlite"
export MSA_REALDATA_FAISS_PATH="$DATA_DIR/index/image_vec.faiss"
export MSA_REALDATA_FACE_FAISS_PATH="$DATA_DIR/index/face_vec.faiss"
export MSA_REALDATA_THUMB_DIR="$DATA_DIR/data/thumbnails"
export MSA_REALDATA_FACE_THUMB_DIR="$DATA_DIR/data/face_thumbnails"
export MSA_REALDATA_FIXTURE_ROOT="$FIXTURE_ROOT"
export MSA_REALDATA_BASE_URL="http://127.0.0.1:$API_PORT"
# Expose the ledger dir (→ the BVT open-capture test runs) ONLY when the ranker wheel is
# actually bundled. Public-mirror bundles ship without it ⇒ no LedgerWriter ⇒ no events;
# leaving the var unset makes the test skip cleanly instead of failing.
if ls "$APP_DIR"/wheels/msa_ranker-*.whl >/dev/null 2>&1; then
  export MSA_REALDATA_LEDGER_DIR="$LEDGER_DIR"
fi

"$MSA_BIN" api start --no-browser >"$API_LOG" 2>&1 &
API_PID=$!
API_STARTED=1

READY=0
for _ in $(seq 1 120); do
  if curl -fsS "$MSA_REALDATA_BASE_URL/health" | grep -q '"status":"ready"'; then
    READY=1
    break
  fi
  sleep 1
done

if [[ "$READY" -ne 1 ]]; then
  echo "API did not become ready at $MSA_REALDATA_BASE_URL/health" >&2
  echo "API log: $API_LOG" >&2
  exit 1
fi

# Warm up the search path: /health returns ready before CLIP is loaded
# (lazy-load on first /search), and on slower runners cold-start encoding
# exceeds pytest's 20s urlopen timeout in test_search_endpoint_returns_json.
# A throwaway POST /search with a generous timeout absorbs the cold start
# outside the test budget; subsequent /search calls hit the warm model.
echo "Warming up CLIP text encoder via sentinel POST /search"
curl -fsS --max-time 180 -X POST -H "Content-Type: application/json" \
  -d '{"q":"warmup"}' "$MSA_REALDATA_BASE_URL/search" >/dev/null \
  || { echo "Sentinel /search warmup failed; see $API_LOG" >&2; exit 1; }

# Step 9: Run the runtime test suite against the installed indexed state and
# live API server, then stop the API in the cleanup trap.
"$VENV_PY" -m pytest "$TEST_ROOT/test_real_media_runtime.py" -v
