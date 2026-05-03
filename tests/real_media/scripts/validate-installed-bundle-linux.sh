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

# Step 6: Write an isolated installed-app config that points at the staged
# fixture tree and a CI-only API port.
mkdir -p "$(dirname "$CONFIG_PATH")"
"$VENV_PY" - <<'PY' "$CONFIG_PATH" "$FIXTURE_ROOT" "$API_PORT"
from pathlib import Path
import sys
import yaml

config_path = Path(sys.argv[1])
fixture_root = sys.argv[2]
api_port = int(sys.argv[3])
data = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
data["media_sources"] = [{"name": "Real Media Fixtures", "path": fixture_root, "read_only": True}]
api = data.get("api") or {}
api["host"] = "127.0.0.1"
api["port"] = api_port
data["api"] = api
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

# Step 8: Run the full installed CLI workflow: index the staged fixtures,
# expose the generated artifact paths to the runtime tests, and start the API.
"$MSA_BIN" index run --config "$CONFIG_PATH" --media-source-override "$FIXTURE_ROOT" --export-to-qdrant

export MSA_REALDATA_WORKSPACE="$DATA_DIR"
export MSA_REALDATA_SQLITE_PATH="$DATA_DIR/index/media.sqlite"
export MSA_REALDATA_FAISS_PATH="$DATA_DIR/index/image_vec.faiss"
export MSA_REALDATA_FACE_FAISS_PATH="$DATA_DIR/index/face_vec.faiss"
export MSA_REALDATA_THUMB_DIR="$DATA_DIR/data/thumbnails"
export MSA_REALDATA_FACE_THUMB_DIR="$DATA_DIR/data/face_thumbnails"
export MSA_REALDATA_FIXTURE_ROOT="$FIXTURE_ROOT"
export MSA_REALDATA_BASE_URL="http://127.0.0.1:$API_PORT"

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
