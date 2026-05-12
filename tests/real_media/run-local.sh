#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"

SOURCE_MODE="staged"
KEEP_WORKSPACE="failure"
PORT="8600"
WORKSPACE=""
REPORT_DIR=""
DEFAULT_REPORT_DIR=".artifacts/real-data"
SKIP_INDEX=0
SKIP_API=0
SKIP_SLOW_MODEL_CHECKS=0

RUN_TIMESTAMP="$(date +%Y%m%d-%H%M%S)"
BRANCH_NAME="$(git -C "$REPO_ROOT" branch --show-current 2>/dev/null || echo detached)"
BRANCH_SLUG="$(printf '%s' "${BRANCH_NAME:-detached}" | tr '/[:space:]' '--' | tr -cd '[:alnum:]._-' )"
RANDOM_SUFFIX="$(od -An -N4 -tx1 /dev/urandom | tr -d ' \n')"
RUN_ID="${RUN_TIMESTAMP}-${BRANCH_SLUG:-detached}-${RANDOM_SUFFIX}"

WORKSPACE_PATH=""
ARTIFACTS_DIR=""
REPORT_COPY_DIR=""
API_PID=""
API_READY=0
WORKSPACE_PYTHONPATH=""
PYTHON_BIN=""

SETUP_STATUS="NOT_RUN"
FAST_STATUS="NOT_RUN"
INDEX_STATUS="NOT_RUN"
API_STATUS="NOT_RUN"
RUNTIME_STATUS="NOT_RUN"
SLOW_STATUS="NOT_RUN"

FIXTURE_ROOT_REL="tests/real_media/fixtures"
FIXTURE_INDEX_ROOT_REL="tests/real_media/fixtures"

usage() {
  cat <<'EOF'
Usage: bash tests/real_media/run-local.sh [options]

Options:
  --source-mode committed|staged|dirty  Source snapshot mode. Default: staged
  --keep-workspace always|failure|never Keep temp workspace. Default: failure
  --port <port>                         API port for harness run. Default: 8600
  --workspace <path>                    Override temp workspace path. Default: /tmp/msa-realdata-...
  --report-dir <path>                   Copy logs/summary to a durable directory. Default: .artifacts/real-data
  --skip-index                          Reuse existing runtime artifacts instead of indexing
  --skip-api                            Skip uvicorn startup and API/runtime HTTP checks
  --skip-slow-model-checks              Skip slow model-backed fixture checks. Default: run them
  --help                                Show this help text
EOF
}

log() {
  printf '[real-data] %s\n' "$*"
}

die() {
  printf '[real-data] ERROR: %s\n' "$*" >&2
  exit 1
}

phase_fail() {
  local phase="$1"
  local message="$2"
  case "$phase" in
    setup) SETUP_STATUS="FAILED" ;;
    fast) FAST_STATUS="FAILED" ;;
    index) INDEX_STATUS="FAILED" ;;
    api) API_STATUS="FAILED" ;;
    runtime) RUNTIME_STATUS="FAILED" ;;
    slow) SLOW_STATUS="FAILED" ;;
  esac
  die "$message"
}

resolve_path() {
  local path="$1"
  local base="$2"
  local target py_cmd

  if [[ "$path" = /* ]]; then
    target="$path"
  else
    target="$base/$path"
  fi

  if realpath -m / >/dev/null 2>&1; then
    realpath -m "$target"
    return 0
  fi

  if command -v python >/dev/null 2>&1; then
    py_cmd="python"
  elif command -v python3 >/dev/null 2>&1; then
    py_cmd="python3"
  else
    die "Required command not found: python or python3"
  fi

  "$py_cmd" - "$target" <<'PY'
from pathlib import Path
import sys

print(Path(sys.argv[1]).resolve(strict=False))
PY
}

require_cmd() {
  command -v "$1" >/dev/null 2>&1 || die "Required command not found: $1"
}

resolve_python_bin() {
  if command -v python >/dev/null 2>&1; then
    printf '%s\n' "python"
    return 0
  fi
  if command -v python3 >/dev/null 2>&1; then
    printf '%s\n' "python3"
    return 0
  fi
  return 1
}

ensure_clip_cache_layout() {
  local cache_root="$1"
  local model_root="$cache_root/models--timm--vit_large_patch14_clip_224.openai"
  local refs_main="$model_root/refs/main"
  local blob_dir="$model_root/blobs"
  local commit_hash snapshot_dir blob_file snapshot_file

  if find "$cache_root" -type f \( -name '*.pt' -o -name '*.safetensors' \) | grep -q .; then
    return 0
  fi

  [[ -f "$refs_main" ]] || return 1
  [[ -d "$blob_dir" ]] || return 1

  commit_hash="$(tr -d '[:space:]' < "$refs_main")"
  [[ -n "$commit_hash" ]] || return 1

  blob_file="$(find "$blob_dir" -maxdepth 1 -type f ! -name '*.lock' ! -name '*.incomplete' -size +100M | head -n 1)"
  if [[ -z "$blob_file" ]]; then
    blob_file="$(find "$blob_dir" -maxdepth 1 -type f ! -name '*.lock' ! -name '*.incomplete' | head -n 1)"
  fi
  [[ -n "$blob_file" ]] || return 1

  snapshot_dir="$model_root/snapshots/$commit_hash"
  snapshot_file="$snapshot_dir/open_clip_pytorch_model.bin"

  mkdir -p "$snapshot_dir"
  if [[ ! -e "$snapshot_file" ]]; then
    ln -s "../../blobs/$(basename "$blob_file")" "$snapshot_file"
  fi

  [[ -e "$snapshot_file" ]]
}

RTDETR_REVISION="ac77a11ff0170a41b771c03264987f8ce2b0d753"
RTDETR_SLUG="models--PekingU--rtdetr_r18vd"

find_rtdetr_model_dir() {
  # Returns the parent directory (containing models--PekingU--rtdetr_r18vd/) that
  # holds a fully cached RT-DETR snapshot (model weights + processor config).
  # Checks the standard location first, then the spike evaluation cache as fallback.
  local rev="$RTDETR_REVISION"
  local slug="$RTDETR_SLUG"
  local candidate
  for candidate in \
    "$REPO_ROOT/models/rtdetr" \
    "$REPO_ROOT/build/spikes/object-detection/model-cache/rtdetr"
  do
    if [[ -f "$candidate/$slug/snapshots/$rev/model.safetensors" ]] \
       && [[ -f "$candidate/$slug/snapshots/$rev/preprocessor_config.json" ]]; then
      printf '%s\n' "$candidate"
      return 0
    fi
  done
  return 1
}

require_model_artifacts() {
  local require_clip="${1:-1}"
  local require_rtdetr="${2:-1}"
  local require_face="${3:-1}"
  local clip_cache_root="$REPO_ROOT/models/clip"
  local insightface_dir="$REPO_ROOT/models/insightface/models/buffalo_l"
  local facenet_dir="$REPO_ROOT/models/facenet_pytorch/checkpoints"
  local torch_home_default="$HOME/.cache/torch/checkpoints"

  if [[ "$require_clip" -eq 1 ]] && ! ensure_clip_cache_layout "$clip_cache_root"; then
    die "Missing cached CLIP weights under $clip_cache_root. Populate the local model cache before running the real-data harness."
  fi

  if [[ "$require_rtdetr" -eq 1 ]]; then
    find_rtdetr_model_dir >/dev/null || die \
      "Missing cached RT-DETR weights (revision $RTDETR_REVISION). " \
      "Populate models/rtdetr/$RTDETR_SLUG/snapshots/$RTDETR_REVISION/ " \
      "or run the spike eval to populate build/spikes/object-detection/model-cache/rtdetr/."
  fi

  # Face model: accept either the project default (facenet-pytorch) or the
  # legacy InsightFace cache. The project default is facenet_pytorch as of
  # the facenet-pytorch backend migration; only one needs to be populated.
  if [[ "$require_face" -eq 1 ]]; then
    local has_facenet=0
    local has_insightface=0
    if [[ -d "$facenet_dir" ]] \
       && find "$facenet_dir" -maxdepth 1 -name '*.pt' | grep -q .; then
      has_facenet=1
    elif [[ -d "$torch_home_default" ]] \
         && find "$torch_home_default" -maxdepth 1 -name '*vggface2*.pt' | grep -q .; then
      has_facenet=1
    fi
    if [[ -d "$insightface_dir" ]] \
       && find "$insightface_dir" -maxdepth 1 -name '*.onnx' | grep -q .; then
      has_insightface=1
    fi
    if [[ "$has_facenet" -eq 0 && "$has_insightface" -eq 0 ]]; then
      die "Missing face-recognition model cache. Populate either: \
$facenet_dir/*.pt (facenet-pytorch, default backend), \
$torch_home_default/*vggface2*.pt, \
or $insightface_dir/*.onnx (legacy InsightFace)."
    fi
  fi
}

port_in_use() {
  local port="$1"
  if command -v ss >/dev/null 2>&1; then
    ss -ltn | awk '{print $4}' | grep -Eq "(^|:)$port$"
    return
  fi
  if command -v lsof >/dev/null 2>&1; then
    lsof -nP -iTCP:"$port" -sTCP:LISTEN >/dev/null 2>&1
    return
  fi
  return 1
}

write_summary() {
  local final_status="$1"
  [[ -n "$ARTIFACTS_DIR" ]] || return 0
  mkdir -p "$ARTIFACTS_DIR"
  cat >"$ARTIFACTS_DIR/summary.md" <<EOF
# Local Real-Data Run Summary

- Timestamp: $RUN_TIMESTAMP
- Branch: ${BRANCH_NAME:-detached}
- Source mode: $SOURCE_MODE
- Keep workspace: $KEEP_WORKSPACE
- Port: $PORT
- Skip index: $SKIP_INDEX
- Skip API: $SKIP_API
- Skip slow model checks: $SKIP_SLOW_MODEL_CHECKS
- Workspace: ${WORKSPACE_PATH:-"(not created)"}
- Durable report copy: ${REPORT_COPY_DIR:-"(none)"}

## Phase Status

- Setup: $SETUP_STATUS
- Fast fixtures: $FAST_STATUS
- Index: $INDEX_STATUS
- API: $API_STATUS
- Runtime: $RUNTIME_STATUS
- Slow checks: $SLOW_STATUS

## Final Result

- Overall: $final_status
EOF
}

copy_reports() {
  [[ -n "$REPORT_DIR" ]] || return 0
  mkdir -p "$REPORT_COPY_DIR"
  cp -f "$ARTIFACTS_DIR"/summary.md "$REPORT_COPY_DIR"/
  for log_file in indexer.log api.log pytest-fast.log pytest-runtime.log pytest-slow.log pytest-lifecycle.log; do
    if [[ -f "$ARTIFACTS_DIR/$log_file" ]]; then
      cp -f "$ARTIFACTS_DIR/$log_file" "$REPORT_COPY_DIR"/
    fi
  done
}

cleanup() {
  local exit_code=$?

  if [[ -n "$API_PID" ]]; then
    if kill -0 "$API_PID" >/dev/null 2>&1; then
      kill "$API_PID" >/dev/null 2>&1 || true
      wait "$API_PID" >/dev/null 2>&1 || true
    fi
  fi

  local final_status="FAILED"
  if [[ $exit_code -eq 0 ]]; then
    final_status="PASSED"
  fi

  if [[ -n "$REPORT_DIR" ]]; then
    mkdir -p "$REPORT_DIR"
    REPORT_COPY_DIR="$REPORT_DIR/$RUN_ID"
  fi

  write_summary "$final_status"
  copy_reports

  if [[ -n "$WORKSPACE_PATH" ]]; then
    case "$KEEP_WORKSPACE" in
      never)
        rm -rf "$WORKSPACE_PATH"
        ;;
      failure)
        if [[ $exit_code -eq 0 ]]; then
          rm -rf "$WORKSPACE_PATH"
        fi
        ;;
      always)
        ;;
    esac
  fi

  if [[ $exit_code -eq 0 ]]; then
    log "PASS"
  else
    log "FAIL"
  fi

  if [[ -n "$REPORT_COPY_DIR" ]]; then
    log "Durable report copy: $REPORT_COPY_DIR"
  fi
  if [[ -n "$WORKSPACE_PATH" && -d "$WORKSPACE_PATH" ]]; then
    log "Workspace preserved at: $WORKSPACE_PATH"
    log "Workspace summary: $WORKSPACE_PATH/artifacts/summary.md"
  elif [[ -n "$WORKSPACE_PATH" ]]; then
    log "Workspace deleted: $WORKSPACE_PATH"
    if [[ -z "$REPORT_COPY_DIR" ]]; then
      log "No durable report copy was requested, so logs/results were not copied out of the temp workspace."
      log "Rerun with --report-dir .artifacts/real-data to keep a repo-local copy, or --keep-workspace always to preserve the temp workspace."
    fi
  fi

  exit "$exit_code"
}

trap cleanup EXIT

while [[ $# -gt 0 ]]; do
  case "$1" in
    --source-mode)
      [[ $# -ge 2 ]] || die "--source-mode requires a value"
      SOURCE_MODE="$2"
      shift 2
      ;;
    --keep-workspace)
      [[ $# -ge 2 ]] || die "--keep-workspace requires a value"
      KEEP_WORKSPACE="$2"
      shift 2
      ;;
    --port)
      [[ $# -ge 2 ]] || die "--port requires a value"
      PORT="$2"
      shift 2
      ;;
    --workspace)
      [[ $# -ge 2 ]] || die "--workspace requires a value"
      WORKSPACE="$2"
      shift 2
      ;;
    --report-dir)
      [[ $# -ge 2 ]] || die "--report-dir requires a value"
      REPORT_DIR="$2"
      shift 2
      ;;
    --skip-index)
      SKIP_INDEX=1
      shift
      ;;
    --skip-api)
      SKIP_API=1
      shift
      ;;
    --skip-slow-model-checks)
      SKIP_SLOW_MODEL_CHECKS=1
      shift
      ;;
    --help)
      usage
      exit 0
      ;;
    *)
      die "Unknown argument: $1"
      ;;
  esac
done

case "$SOURCE_MODE" in
  committed|staged|dirty) ;;
  *) die "--source-mode must be one of: committed, staged, dirty" ;;
esac

case "$KEEP_WORKSPACE" in
  always|failure|never) ;;
  *) die "--keep-workspace must be one of: always, failure, never" ;;
esac

[[ "$PORT" =~ ^[0-9]+$ ]] || die "--port must be numeric"
(( PORT >= 1 && PORT <= 65535 )) || die "--port must be between 1 and 65535"

if [[ -z "$REPORT_DIR" ]]; then
  REPORT_DIR="$DEFAULT_REPORT_DIR"
fi

REPORT_DIR="$(resolve_path "$REPORT_DIR" "$REPO_ROOT")"

if [[ -n "$WORKSPACE" ]]; then
  WORKSPACE_PATH="$(resolve_path "$WORKSPACE" "$REPO_ROOT")"
else
  WORKSPACE_PATH="/tmp/msa-realdata-${BRANCH_SLUG:-detached}-${RUN_TIMESTAMP}-${RANDOM_SUFFIX}"
fi

ARTIFACTS_DIR="$WORKSPACE_PATH/artifacts"

log "Resolved configuration:"
log "  source mode: $SOURCE_MODE"
log "  keep workspace: $KEEP_WORKSPACE"
log "  port: $PORT"
log "  workspace: $WORKSPACE_PATH"
log "  report dir: $REPORT_DIR"
log "  skip index: $SKIP_INDEX"
log "  skip api: $SKIP_API"
log "  skip slow model checks: $SKIP_SLOW_MODEL_CHECKS"

SETUP_STATUS="RUNNING"

require_cmd git
require_cmd bash
require_cmd curl
require_cmd exiftool
require_cmd tar
PYTHON_BIN="$(resolve_python_bin)" || phase_fail setup "Required command not found: python or python3"

[[ -f "$REPO_ROOT/.venv/bin/activate" ]] || phase_fail setup "Missing virtualenv at $REPO_ROOT/.venv"
[[ -d "$REPO_ROOT/$FIXTURE_ROOT_REL/originals" ]] || phase_fail setup "Missing fixture originals at $REPO_ROOT/$FIXTURE_ROOT_REL/originals"
[[ -d "$REPO_ROOT/$FIXTURE_ROOT_REL/derived" ]] || phase_fail setup "Missing fixture derived dir at $REPO_ROOT/$FIXTURE_ROOT_REL/derived"
find "$REPO_ROOT/$FIXTURE_ROOT_REL" -type f | grep -q . || phase_fail setup "No fixture files found under $REPO_ROOT/$FIXTURE_ROOT_REL"
REQUIRE_CLIP=0
REQUIRE_RTDETR=0
REQUIRE_FACE=0

if [[ $SKIP_INDEX -eq 0 ]]; then
  REQUIRE_CLIP=1
  REQUIRE_RTDETR=1
  REQUIRE_FACE=1
fi

if [[ $SKIP_SLOW_MODEL_CHECKS -eq 0 ]]; then
  REQUIRE_RTDETR=1
  REQUIRE_FACE=1
fi

require_model_artifacts "$REQUIRE_CLIP" "$REQUIRE_RTDETR" "$REQUIRE_FACE"

if [[ $SKIP_API -eq 0 ]] && port_in_use "$PORT"; then
  phase_fail setup "Port $PORT is already in use"
fi

if [[ -e "$WORKSPACE_PATH" ]]; then
  phase_fail setup "Workspace path already exists: $WORKSPACE_PATH"
fi

mkdir -p "$WORKSPACE_PATH"
mkdir -p "$ARTIFACTS_DIR"

log "Materializing source snapshot into workspace"
git -C "$REPO_ROOT" archive --format=tar HEAD | tar -xf - -C "$WORKSPACE_PATH"

if [[ "$SOURCE_MODE" == "staged" || "$SOURCE_MODE" == "dirty" ]]; then
  if ! git -C "$REPO_ROOT" diff --binary --cached --quiet HEAD --; then
    git -C "$REPO_ROOT" diff --binary --cached HEAD -- | (cd "$WORKSPACE_PATH" && git apply --allow-binary-replacement --whitespace=nowarn)
  fi
fi

if [[ "$SOURCE_MODE" == "dirty" ]]; then
  if ! git -C "$REPO_ROOT" diff --binary --quiet --; then
    git -C "$REPO_ROOT" diff --binary -- | (cd "$WORKSPACE_PATH" && git apply --allow-binary-replacement --whitespace=nowarn)
  fi
fi

ln -s "$REPO_ROOT/.venv" "$WORKSPACE_PATH/.venv"
mkdir -p "$WORKSPACE_PATH/models"
for rel in clip insightface facenet_pytorch; do
  if [[ -e "$REPO_ROOT/models/$rel" && ! -e "$WORKSPACE_PATH/models/$rel" ]]; then
    ln -s "$REPO_ROOT/models/$rel" "$WORKSPACE_PATH/models/$rel"
  fi
done
# RT-DETR: resolve from standard location or spike cache, whichever is populated.
_rtdetr_src="$(find_rtdetr_model_dir 2>/dev/null)" || true
if [[ -n "$_rtdetr_src" && ! -e "$WORKSPACE_PATH/models/rtdetr" ]]; then
  ln -s "$_rtdetr_src" "$WORKSPACE_PATH/models/rtdetr"
fi
unset _rtdetr_src

copy_existing_runtime_artifacts() {
  local source_dir target_dir
  for rel in index qdrant data/thumbnails data/face_thumbnails; do
    source_dir="$REPO_ROOT/$rel"
    target_dir="$WORKSPACE_PATH/$rel"
    if [[ -e "$source_dir" ]]; then
      mkdir -p "$(dirname "$target_dir")"
      cp -a "$source_dir" "$target_dir"
    fi
  done
}

if [[ $SKIP_INDEX -eq 1 ]]; then
  log "Reusing existing runtime artifacts from main checkout because --skip-index was requested"
  copy_existing_runtime_artifacts
fi

# BVT runs on CPU on CI runners. The repo's config.yaml ships with
# `enable_object_detection: auto`, which the indexer resolves to "skip"
# on CPU — leaving video_keyframes.tags empty and failing
# test_video_media_*_includes_keyframe_tags. Force detection on so the
# indexer actually runs RT-DETR, mirroring the override the bundle BVT
# validators apply (tests/real_media/scripts/validate-installed-bundle-*).
"$WORKSPACE_PATH/.venv/bin/python" - "$WORKSPACE_PATH/config.yaml" <<'PY'
import sys, yaml
from pathlib import Path
p = Path(sys.argv[1])
data = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
data["enable_object_detection"] = True
data["enable_video_object_detection"] = True
p.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
PY

export MSA_CONFIG_PATH="$WORKSPACE_PATH/config.yaml"
export MSA_REALDATA_WORKSPACE="$WORKSPACE_PATH"
export MSA_REALDATA_SQLITE_PATH="$WORKSPACE_PATH/index/media.sqlite"
# Tell the test layer whether the indexer phase actually ran in this
# invocation. The "no .faiss files written" assertion only makes sense
# when we just produced the artifacts ourselves — with --skip-index we
# reuse the developer's existing index/ directory, which may include
# legacy FAISS leftovers from before the SQLite migration.
export MSA_REALDATA_INDEX_RAN="$(if [[ $SKIP_INDEX -eq 0 ]]; then echo 1; else echo 0; fi)"
# FAISS paths intentionally not exported — embeddings now live in SQLite
# (image_embedding / keyframe_embedding / face_embedding tables).
export MSA_REALDATA_THUMB_DIR="$WORKSPACE_PATH/data/thumbnails"
export MSA_REALDATA_FACE_THUMB_DIR="$WORKSPACE_PATH/data/face_thumbnails"
export MSA_REALDATA_FIXTURE_ROOT="$WORKSPACE_PATH/$FIXTURE_INDEX_ROOT_REL"
export MSA_REALDATA_BASE_URL=""
export MSA_REALDATA_INDEXER_LOG="$ARTIFACTS_DIR/indexer.log"
WORKSPACE_PYTHONPATH="$WORKSPACE_PATH/src${PYTHONPATH:+:$PYTHONPATH}"
export HF_HUB_OFFLINE=1

# Lower per-batch commit thresholds so the small fixture (~18 files) actually
# exercises the per-batch commit path. Without this override the default
# N=200/M=15s would never fire and downstream test assertions would silently
# pass on a no-op.
export MSA_INDEXER_COMMIT_BATCH_FILES=5
export MSA_INDEXER_COMMIT_BATCH_SECONDS=5

SETUP_STATUS="PASSED"

FAST_STATUS="RUNNING"
log "Running fast real-media fixture checks"
if (
  cd "$WORKSPACE_PATH"
  export MSA_CONFIG_PATH PYTHONPATH="$WORKSPACE_PYTHONPATH" HF_HUB_OFFLINE
  exec bash scripts/dev-cli.sh pytest tests/real_media/test_real_media_fixtures.py -v -m "not slow"
) >"$ARTIFACTS_DIR/pytest-fast.log" 2>&1; then
  FAST_STATUS="PASSED"
else
  phase_fail fast "Fast fixture checks failed. See $ARTIFACTS_DIR/pytest-fast.log"
fi

if [[ $SKIP_INDEX -eq 0 ]]; then
  INDEX_STATUS="RUNNING"
  log "Running indexer against $FIXTURE_INDEX_ROOT_REL"
  if (
    cd "$WORKSPACE_PATH"
    export MSA_CONFIG_PATH PYTHONPATH="$WORKSPACE_PYTHONPATH" HF_HUB_OFFLINE \
      MSA_INDEXER_COMMIT_BATCH_FILES MSA_INDEXER_COMMIT_BATCH_SECONDS
    exec bash scripts/dev-cli.sh msa index run --config "$WORKSPACE_PATH/config.yaml" --media-source-override "$WORKSPACE_PATH/$FIXTURE_INDEX_ROOT_REL" --export-to-qdrant
  ) >"$ARTIFACTS_DIR/indexer.log" 2>&1; then
    INDEX_STATUS="PASSED"
  else
    phase_fail index "Indexer run failed. See $ARTIFACTS_DIR/indexer.log"
  fi
else
  INDEX_STATUS="SKIPPED"
fi

if [[ $SKIP_API -eq 0 ]]; then
  API_STATUS="RUNNING"
  log "Starting API on port $PORT"
  (
    cd "$WORKSPACE_PATH"
    export MSA_CONFIG_PATH PYTHONPATH="$WORKSPACE_PYTHONPATH" HF_HUB_OFFLINE
    exec bash scripts/dev-cli.sh uvicorn msa_apps.search_api.app:app --host 127.0.0.1 --port "$PORT"
  ) >"$ARTIFACTS_DIR/api.log" 2>&1 &
  API_PID=$!

  for _ in $(seq 1 120); do
    if curl -fsS "http://127.0.0.1:$PORT/health" | grep -q '"status":"ready"'; then
      API_READY=1
      break
    fi
    if ! kill -0 "$API_PID" >/dev/null 2>&1; then
      break
    fi
    sleep 1
  done

  if [[ $API_READY -ne 1 ]]; then
    phase_fail api "API did not become ready. See $ARTIFACTS_DIR/api.log"
  fi
  # Warm up the search path: /health returns ready before CLIP is loaded
  # (lazy-load on first /search), and on slower runners the cold-start
  # encoding can exceed pytest's 20s urlopen timeout in
  # test_search_endpoint_returns_json. A throwaway POST /search with a
  # generous timeout absorbs that cold start outside the test budget;
  # subsequent /search calls in tests hit the warm model.
  log "Warming up CLIP text encoder via sentinel POST /search"
  curl -fsS --max-time 180 -X POST -H "Content-Type: application/json" \
    -d '{"q":"warmup"}' "http://127.0.0.1:$PORT/search" >/dev/null \
    || phase_fail api "Sentinel /search warmup failed. See $ARTIFACTS_DIR/api.log"
  API_STATUS="PASSED"
  export MSA_REALDATA_BASE_URL="http://127.0.0.1:$PORT"
else
  API_STATUS="SKIPPED"
fi

RUNTIME_STATUS="RUNNING"
log "Running runtime validation checks"
if (
  cd "$WORKSPACE_PATH"
  export MSA_CONFIG_PATH PYTHONPATH="$WORKSPACE_PYTHONPATH" HF_HUB_OFFLINE
  exec bash scripts/dev-cli.sh pytest tests/real_media/test_real_media_runtime.py -v
) >"$ARTIFACTS_DIR/pytest-runtime.log" 2>&1; then
  RUNTIME_STATUS="PASSED"
else
  phase_fail runtime "Runtime validation failed. See $ARTIFACTS_DIR/pytest-runtime.log"
fi

if [[ $SKIP_SLOW_MODEL_CHECKS -eq 0 ]]; then
  SLOW_STATUS="RUNNING"
  log "Running slow model-backed real-media checks"
  if (
    cd "$WORKSPACE_PATH"
    export MSA_CONFIG_PATH PYTHONPATH="$WORKSPACE_PYTHONPATH" HF_HUB_OFFLINE
    exec bash scripts/dev-cli.sh pytest tests/real_media/test_real_media_fixtures.py -v -m "slow"
  ) >"$ARTIFACTS_DIR/pytest-slow.log" 2>&1; then
    :  # success; SLOW_STATUS is set to PASSED below only if the lifecycle
       # block also succeeds.
  else
    phase_fail slow "Slow model-backed checks failed. See $ARTIFACTS_DIR/pytest-slow.log"
  fi

  # Indexer lifecycle stress tests — these spawn the real msa binary in
  # their own isolated tmp_path workspaces, so they don't touch the
  # indexed state from the steps above. They cover:
  #   • cooperative-stop end-to-end (PID published, msa index stop drives
  #     a clean rc=0 exit, no forrtl: error)
  #   • Qdrant export ran during the cooperative-stop finalisation
  #   • non-cooperative kill (SIGKILL/TerminateProcess) recovers on
  #     subsequent msa index run
  # See internal/docs/testing/INDEXER_LIFECYCLE_STRESS.md.
  log "Running indexer lifecycle stress tests"
  if (
    cd "$WORKSPACE_PATH"
    export MSA_CONFIG_PATH PYTHONPATH="$WORKSPACE_PYTHONPATH" HF_HUB_OFFLINE
    exec bash scripts/dev-cli.sh pytest tests/test_cmd_index_stop.py -v -m "slow"
  ) >"$ARTIFACTS_DIR/pytest-lifecycle.log" 2>&1; then
    :  # success
  else
    phase_fail slow "Lifecycle stress tests failed. See $ARTIFACTS_DIR/pytest-lifecycle.log"
  fi

  # Mark slow phase passed only if neither block called phase_fail above.
  [[ "$SLOW_STATUS" == "RUNNING" ]] && SLOW_STATUS="PASSED"
else
  SLOW_STATUS="SKIPPED"
fi

exit 0
