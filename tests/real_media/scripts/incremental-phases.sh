#!/usr/bin/env bash
# BVT Phases C/D — incremental indexing (M-8/S-1 + S-3, plan §6.2).
#
# Counter-based assertions over the INDEXER_SUMMARY complete payload —
# never wall-clock. Shared by the dev-env harness (run-local.sh) and the
# Linux/macOS bundle validators; the Windows validator mirrors this logic
# in PowerShell, reusing the same incremental_checks.py helper.
#
# Usage:
#   incremental-phases.sh seed --fixture-root <path>
#       Stage the content-unique seed files the phases mutate (run BEFORE
#       the first index). Requires exiftool on PATH.
#
#   incremental-phases.sh run --python <python> --sqlite <db> \
#       --fixture-root <path> --log-dir <dir> [--qdrant <dir>] \
#       -- <indexer command...>
#       Run Phase C (no-op re-run) and Phase D (targeted mutations of the
#       staged fixture copy). The indexer command is executed once per
#       phase with output captured to per-phase logs under --log-dir.
#       With --qdrant (M-8/S-3): Phase D additionally asserts the delta
#       export's sent counts are single-change-sized and that deleting the
#       seed video drops the Qdrant point count by its keyframe count.
#       These open embedded Qdrant, so the phases must run while nothing
#       else (API) holds it — true for every current caller.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CHECKS_PY="$SCRIPT_DIR/incremental_checks.py"

SEED_DIR_NAME="incremental"
SEED_IMAGE_1="incremental_seed_image_01.jpg"
SEED_IMAGE_2="incremental_seed_image_02.jpg"
SEED_VIDEO_1="incremental_seed_video_01.mp4"
MOVED_SUBDIR="moved"

log() { printf '[incremental] %s\n' "$*"; }
die() { printf '[incremental] FAIL: %s\n' "$*" >&2; exit 1; }

cmd_seed() {
  local fixture_root=""
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --fixture-root) fixture_root="$2"; shift 2 ;;
      *) die "seed: unknown argument $1" ;;
    esac
  done
  [[ -n "$fixture_root" ]] || die "seed: --fixture-root is required"
  [[ -d "$fixture_root" ]] || die "seed: fixture root not found: $fixture_root"
  command -v exiftool >/dev/null 2>&1 || die "seed: exiftool not on PATH"

  local seed_dir="$fixture_root/$SEED_DIR_NAME"
  mkdir -p "$seed_dir"

  # Two image seeds derived from the same original but made content-unique
  # (distinct EXIF Artist) so each has its own media_id — Phase D's
  # supersede and move steps must not collide with duplicate-copy logic.
  cp "$fixture_root/originals/object_landscape_01.jpg" "$seed_dir/$SEED_IMAGE_1"
  exiftool -overwrite_original -Artist="MSA-BVT-Incremental-Seed-01" \
    "$seed_dir/$SEED_IMAGE_1" >/dev/null
  cp "$fixture_root/originals/object_landscape_01.jpg" "$seed_dir/$SEED_IMAGE_2"
  exiftool -overwrite_original -Artist="MSA-BVT-Incremental-Seed-02" \
    "$seed_dir/$SEED_IMAGE_2" >/dev/null

  # Video seed: content-unique copy (exiftool writes QuickTime tags in MP4).
  # Renamed away from the GoPro naming so it indexes as a regular video.
  cp "$fixture_root/derived/trimmed_gopro_gps_01.mp4" "$seed_dir/$SEED_VIDEO_1"
  exiftool -overwrite_original -Title="MSA-BVT-Incremental-Seed-Video" \
    "$seed_dir/$SEED_VIDEO_1" >/dev/null

  log "seeded $seed_dir ($SEED_IMAGE_1, $SEED_IMAGE_2, $SEED_VIDEO_1)"
}

cmd_run() {
  local python_bin="" sqlite_db="" fixture_root="" log_dir="" qdrant_dir=""
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --python) python_bin="$2"; shift 2 ;;
      --sqlite) sqlite_db="$2"; shift 2 ;;
      --fixture-root) fixture_root="$2"; shift 2 ;;
      --log-dir) log_dir="$2"; shift 2 ;;
      --qdrant) qdrant_dir="$2"; shift 2 ;;
      --) shift; break ;;
      *) die "run: unknown argument $1" ;;
    esac
  done
  [[ -n "$python_bin" && -n "$sqlite_db" && -n "$fixture_root" && -n "$log_dir" ]] \
    || die "run: --python, --sqlite, --fixture-root, --log-dir are all required"
  [[ $# -gt 0 ]] || die "run: indexer command missing after --"
  [[ -f "$sqlite_db" ]] || die "run: SQLite DB not found: $sqlite_db"

  local seed_dir="$fixture_root/$SEED_DIR_NAME"
  [[ -f "$seed_dir/$SEED_IMAGE_1" ]] || die "run: seed files missing — was 'seed' run before the first index?"
  mkdir -p "$log_dir"

  index_once() {
    local phase_log="$1"
    log "indexer re-run -> $(basename "$phase_log")"
    if ! "${INDEXER_CMD[@]}" >"$phase_log" 2>&1; then
      tail -n 100 "$phase_log" >&2 || true
      die "indexer run failed (see $phase_log)"
    fi
  }

  INDEXER_CMD=("$@")

  # ── Phase C: no-op re-run ────────────────────────────────────────────────
  log "Phase C: no-op re-run"
  "$python_bin" "$CHECKS_PY" snapshot --sqlite "$sqlite_db" --out "$log_dir/phase-c-baseline.json"
  index_once "$log_dir/indexer-phase-c.log"
  "$python_bin" "$CHECKS_PY" assert-complete --log "$log_dir/indexer-phase-c.log" \
    --expect files_hashed=0 \
    --expect moves_detected=0 --expect superseded=0 \
    --expect missing_marked=0 --expect tombstoned=0 --expect resurrected=0 \
    --expect-gt fingerprint_hits=0 \
    --expect-eq-key fingerprint_hits=total_found
  "$python_bin" "$CHECKS_PY" assert-contains --log "$log_dir/indexer-phase-c.log" \
    --text "Skipping Qdrant export (no index changes detected"
  "$python_bin" "$CHECKS_PY" compare-counts --sqlite "$sqlite_db" \
    --baseline "$log_dir/phase-c-baseline.json"
  log "Phase C PASSED"

  # ── Phase D(a): EXIF re-inject one image ────────────────────────────────
  log "Phase D(a): EXIF re-inject $SEED_IMAGE_1"
  exiftool -overwrite_original -Artist="MSA-BVT-Incremental-Mutated" \
    "$seed_dir/$SEED_IMAGE_1" >/dev/null
  index_once "$log_dir/indexer-phase-d-exif.log"
  # Content replaced in place: exactly one file hashed, the old content
  # superseded (tombstoned), only that media re-processed.
  "$python_bin" "$CHECKS_PY" assert-complete --log "$log_dir/indexer-phase-d-exif.log" \
    --expect files_hashed=1 --expect superseded=1 \
    --expect processed_images=1 --expect processed_videos=0 \
    --expect moves_detected=0
  if [[ -n "$qdrant_dir" ]]; then
    # M-8/S-3: the delta export sends ONLY the changed file's rows — image
    # sent == 1, video sent == 0 — and the supersede tombstone's old point
    # is removed by the same run's deletion pass.
    "$python_bin" "$CHECKS_PY" assert-export-sent --log "$log_dir/indexer-phase-d-exif.log" \
      --expect-image 1 --expect-video 0 --min-deleted 1
  fi

  # ── Phase D(b): move one image into a subfolder ─────────────────────────
  log "Phase D(b): move $SEED_IMAGE_2 -> $SEED_DIR_NAME/$MOVED_SUBDIR/"
  "$python_bin" "$CHECKS_PY" snapshot --sqlite "$sqlite_db" --out "$log_dir/phase-d-move-baseline.json"
  mkdir -p "$seed_dir/$MOVED_SUBDIR"
  mv "$seed_dir/$SEED_IMAGE_2" "$seed_dir/$MOVED_SUBDIR/$SEED_IMAGE_2"
  index_once "$log_dir/indexer-phase-d-move.log"
  "$python_bin" "$CHECKS_PY" assert-complete --log "$log_dir/indexer-phase-d-move.log" \
    --expect moves_detected=1 --expect files_hashed=1 \
    --expect processed_images=0 --expect processed_videos=0
  # A move must not re-embed: embedding tables byte-for-byte same row counts.
  "$python_bin" "$CHECKS_PY" compare-counts --sqlite "$sqlite_db" \
    --baseline "$log_dir/phase-d-move-baseline.json" \
    --tables image_embedding,keyframe_embedding,face_embedding

  # ── Phase D(c): delete one video → grace, then tombstone ────────────────
  log "Phase D(c): delete $SEED_VIDEO_1 (two-scan grace)"
  rm "$seed_dir/$SEED_VIDEO_1"
  index_once "$log_dir/indexer-phase-d-del1.log"
  "$python_bin" "$CHECKS_PY" assert-complete --log "$log_dir/indexer-phase-d-del1.log" \
    --expect missing_marked=1 --expect tombstoned=0
  if [[ -n "$qdrant_dir" ]]; then
    # M-8/S-3: snapshot the video collection BEFORE the tombstoning re-run
    # (the grace run exports nothing, so the count is still pre-deletion).
    "$python_bin" "$CHECKS_PY" qdrant-count --qdrant "$qdrant_dir" \
      --collection video_emb --out "$log_dir/phase-d-del-qdrant-baseline.json"
  fi
  index_once "$log_dir/indexer-phase-d-del2.log"
  "$python_bin" "$CHECKS_PY" assert-complete --log "$log_dir/indexer-phase-d-del2.log" \
    --expect tombstoned=1
  if [[ -n "$qdrant_dir" ]]; then
    # M-8/S-3: the tombstoned video's keyframe points are gone — the point
    # count dropped by exactly its keyframe count.
    "$python_bin" "$CHECKS_PY" assert-qdrant-drop --qdrant "$qdrant_dir" \
      --collection video_emb --baseline "$log_dir/phase-d-del-qdrant-baseline.json" \
      --sqlite "$sqlite_db" --deleted-name "$SEED_VIDEO_1"
  fi
  "$python_bin" "$CHECKS_PY" assert-media-state --sqlite "$sqlite_db" \
    --moved-name "$SEED_IMAGE_2" --moved-rel "$SEED_DIR_NAME/$MOVED_SUBDIR/$SEED_IMAGE_2" \
    --deleted-name "$SEED_VIDEO_1"
  log "Phase D PASSED"
}

[[ $# -gt 0 ]] || die "missing subcommand (seed|run)"
SUBCOMMAND="$1"
shift
case "$SUBCOMMAND" in
  seed) cmd_seed "$@" ;;
  run) cmd_run "$@" ;;
  *) die "unknown subcommand: $SUBCOMMAND" ;;
esac
