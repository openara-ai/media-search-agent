from pathlib import Path
import json
import re
from PIL import Image
from loguru import logger
from .utils.logging import with_ctx
from .io.scanner import iter_media
from msa_settings.paths import resolve_for_access
from .io.exif import get_exif_basic
try:
    import reverse_geocoder as rg
    RG_AVAILABLE = True
except ImportError:
    rg = None  # define symbol to satisfy static analyzers
    RG_AVAILABLE = False
from .io.video import (
    get_video_meta,
    extract_keyframes_from_shot,
    extract_video_gps_track,
    sample_video_gps_at_timestamp,
    should_extract_video_gps_track,
)
from .io.shot_detection import detect_shots
from .io.thumbnails import write_thumbnail, write_video_thumbnail, write_face_thumbnail
from .utils.hashes import sha256_of_file
from .db.sqlite_store import SQLiteStore
from .models.embeddings import ClipEmbedder
from .models.objects import ObjectDetector
from .db.qdrant_export import (
    export_images_to_qdrant,
    export_video_frames_to_qdrant,
    get_qdrant_export_version,
    record_qdrant_export_version,
)

IMAGE_EXT = {".jpg",".jpeg",".png",".heic",".tif",".tiff",".webp"}
VIDEO_EXT = {".mp4",".mov",".m4v",".avi",".mkv",".wmv",".flv",".webm"}


def _emit_indexer_summary(**payload) -> None:
    logger.info(f"INDEXER_SUMMARY {json.dumps(payload, sort_keys=True)}")


def _parse_duration_token(token: str) -> float | None:
    token = token.strip().lower()
    if token.endswith("/min"):
        token = token[:-4]
    match = re.fullmatch(r"([0-9]+(?:\.[0-9]+)?)(ms|s|m|h)", token)
    if not match:
        return None
    value = float(match.group(1))
    unit = match.group(2)
    if unit == "ms":
        return value / 1000.0
    if unit == "s":
        return value
    if unit == "m":
        return value * 60.0
    if unit == "h":
        return value * 3600.0
    return None


def _load_historical_perf(log_path: Path | str) -> tuple[float, float, float]:
    avg_img_time = 1.0
    avg_vid_time = 30.0
    avg_vid_time_per_min = 5.0
    try:
        path = Path(log_path)
        if not path.exists():
            return avg_img_time, avg_vid_time, avg_vid_time_per_min
        with path.open("r", errors="replace") as fh:
            lines = fh.readlines()[-2000:]
        for line in reversed(lines):
            if "Performance |" not in line:
                continue
            img_match = re.search(r"avg_image=([0-9.]+(?:ms|s|m|h))", line)
            vid_match = re.search(r"avg_video=([0-9.]+(?:ms|s|m|h))", line)
            per_min_match = re.search(r"avg_video_per_min=([0-9.]+(?:ms|s|m|h)/min)", line)
            parsed_img = _parse_duration_token(img_match.group(1)) if img_match else None
            parsed_vid = _parse_duration_token(vid_match.group(1)) if vid_match else None
            parsed_per_min = _parse_duration_token(per_min_match.group(1)) if per_min_match else None
            if parsed_img is not None:
                avg_img_time = parsed_img
            if parsed_vid is not None:
                avg_vid_time = parsed_vid
            if parsed_per_min is not None:
                avg_vid_time_per_min = parsed_per_min
            break
    except Exception:
        pass
    return avg_img_time, avg_vid_time, avg_vid_time_per_min

def get_place_name(lat: float, lon: float) -> str | None:
    """Convert GPS coordinates to place name using reverse geocoding.
    
    Returns formatted place name like "San Jose, California, United States"
    or None if geocoding fails or library not available.
    """
    if not RG_AVAILABLE or rg is None:
        return None
    
    try:
        result = rg.search((lat, lon), mode=1)  # mode=1 for single result
        if result and len(result) > 0:
            r = result[0]
            # Build hierarchical place name: city, state/admin, country
            parts = []
            if r.get('name'):  # City/town name
                parts.append(r['name'])
            if r.get('admin1'):  # State/province
                parts.append(r['admin1'])
            if r.get('cc'):  # Country code - could map to full name if needed
                parts.append(r['cc'])
            return ", ".join(parts) if parts else None
    except Exception as e:
        logger.debug(f"Reverse geocoding failed for ({lat}, {lon}): {e}")
    
    return None

def run_index(config, stop_event=None):
    import time
    import os
    # Track total run time
    run_start_time = time.perf_counter()

    # Per-batch commit configuration. The indexer commits to SQLite every N
    # files or every M seconds, whichever fires first, so that browse readers
    # see partial results during a long run and a crash loses at most one
    # batch of work.
    def _env_int(name: str, default: int) -> int:
        raw = os.getenv(name)
        if raw is None or raw.strip() == "":
            return default
        try:
            return int(raw)
        except ValueError:
            logger.warning(
                f"{name}={raw!r} is not a valid integer; falling back to default {default}"
            )
            return default

    def _env_float(name: str, default: float) -> float:
        raw = os.getenv(name)
        if raw is None or raw.strip() == "":
            return default
        try:
            return float(raw)
        except ValueError:
            logger.warning(
                f"{name}={raw!r} is not a valid number; falling back to default {default}"
            )
            return default

    commit_batch_files = max(1, _env_int("MSA_INDEXER_COMMIT_BATCH_FILES", 200))
    commit_batch_seconds = max(1.0, _env_float("MSA_INDEXER_COMMIT_BATCH_SECONDS", 15.0))
    files_since_commit = 0
    last_commit_ts = run_start_time
    commit_batch_serial = 0
    commit_count = 0
    total_commit_ms = 0
    inter_commit_ms_list: list[int] = []
    
    # Collect media sources to index (sources-only; no legacy fallback)
    sources_to_index = []
    # Highest precedence: explicit CLI media_source_override
    if getattr(config, 'media_source_override', None):
        from dataclasses import dataclass
        @dataclass
        class OverrideSource:
            name: str
            path: str
            enabled: bool = True
        sources_to_index = [OverrideSource(name="cli-override", path=str(config.media_source_override))]
        logger.info(f"Using CLI media_source_override: {config.media_source_override}")
    elif hasattr(config, 'media_sources') and config.media_sources:
        sources_to_index = [s for s in config.media_sources if getattr(s, 'enabled', True)]
        if not sources_to_index:
            logger.error("No enabled media_sources found in config; aborting.")
            return
        logger.info(f"Found {len(sources_to_index)} enabled media sources: {[s.name for s in sources_to_index]}")
    else:
        logger.error("No media_sources defined and no --media-source-override provided; aborting.")
        return

    # Validate source paths exist
    valid_sources = []
    for s in sources_to_index:
        try:
            rp = Path(resolve_for_access(str(s.path)))
            if rp.exists() and rp.is_dir():
                valid_sources.append(s)
            else:
                logger.warning(f"Source path does not exist or is not a directory, skipping: {s.path}")
        except Exception as e:
            logger.warning(f"Invalid source path '{getattr(s, 'path', None)}': {e}")
    if not valid_sources:
        logger.error("No valid source paths to index; aborting.")
        return
    sources_to_index = valid_sources

    # Quick count phase for immediate user feedback and a rough initial estimate.
    hist_img_time, hist_vid_time, hist_vid_time_per_min = _load_historical_perf(
        Path(getattr(config, "log_dir", "logs")) / "msa.log"
    )
    total_found = 0
    total_images_found = 0
    total_videos_found = 0
    media_type_filter = None
    if getattr(config, 'image_only', False):
        media_type_filter = "image"
    elif getattr(config, 'video_only', False):
        media_type_filter = "video"
    for source in sources_to_index:
        root = Path(resolve_for_access(str(source.path)))
        for p in iter_media(root, media_type=media_type_filter, stop_event=stop_event):
            ext = p.suffix.lower()
            total_found += 1
            if ext in IMAGE_EXT:
                total_images_found += 1
            elif ext in VIDEO_EXT:
                total_videos_found += 1
    est_img_time = total_images_found * hist_img_time
    est_vid_time = total_videos_found * hist_vid_time
    _emit_indexer_summary(
        phase="processing",
        total_found=total_found,
        images_to_process=total_images_found,
        videos_to_process=total_videos_found,
        estimated_remaining_seconds=round(est_img_time + est_vid_time),
    )
    
    reprocess_flags = []
    if getattr(config, 'reprocess_gps', False):
        reprocess_flags.append("GPS")
    if getattr(config, 'reprocess_objects', False):
        reprocess_flags.append("objects")
    if getattr(config, 'reprocess_faces', False):
        reprocess_flags.append("faces")
    if getattr(config, 'reprocess_embeddings', False):
        reprocess_flags.append("embeddings")
    reprocess_msg = f" | Reprocessing: {', '.join(reprocess_flags)}" if reprocess_flags else ""
    logger.info(f"Starting indexing run | sources={len(sources_to_index)} device={getattr(config,'device','cpu')} model={getattr(config,'model_name','?')} pretrained={getattr(config,'pretrained','?')}{reprocess_msg}")
    
    # Backup SQLite database if reprocessing faces
    # This allows rollback if face deletion causes issues
    if getattr(config, 'reprocess_faces', False):
        import shutil
        from datetime import datetime
        
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        sqlite_path = Path(config.sqlite_path)
        
        if sqlite_path.exists():
            sqlite_backup = sqlite_path.parent / f"{sqlite_path.stem}.backup.{timestamp}{sqlite_path.suffix}"
            shutil.copy2(sqlite_path, sqlite_backup)
            logger.info(f"Created SQLite backup: {sqlite_backup.name}")
            logger.info(f"Restore with: cp {sqlite_backup} {sqlite_path}")
    
    # Note: face index cleanup on reprocess_faces is now handled by
    # ON DELETE CASCADE on face_embedding -> face. The pipeline calls
    # db.delete_faces_for_media(media_id) when reprocess_faces is set,
    # which transitively removes the per-face embedding rows. Legacy
    # face_vec.faiss files (if any) are left in place harmlessly — the
    # user can delete them manually after a successful re-index.

    # Helper function to normalize device name for logging
    def get_device_label(device: str) -> str:
        """Convert device string to 'CPU' or 'GPU' for logging."""
        if device and device.lower().startswith('cuda'):
            return 'GPU'
        elif device and device.lower() == 'mps':
            return 'GPU'  # Apple Silicon GPU
        else:
            return 'CPU'
    
    # DB + Vector init
    db = SQLiteStore(config.sqlite_path, autocommit=False)
    db.init_schema(Path(__file__).parent / "db" / "schema.sql")
    # total_changes is connection-scoped and counts all writes on this SQLite
    # connection. We snapshot it after init_schema() so schema bootstrap/migration
    # writes don't look like index content changes for this run.
    initial_db_total_changes = db.get_total_changes()

    # Stage 3 upgrade-path advisory: surface pre-Stage-3 DBs where face
    # rows exist without face_embedding rows. We don't auto-fix this
    # because re-running face detection would overwrite manual labels
    # (face.person_id) on ON CONFLICT — the user must opt in.
    # Image and keyframe backfills *are* automatic in the per-file
    # gating below; only faces require an explicit user action because
    # of the label-preservation concern.
    try:
        orphan_faces = db.count_orphan_face_embeddings()
        if orphan_faces > 0:
            logger.warning(
                f"{orphan_faces} face row(s) lack face_embedding entries. The DB was "
                "likely indexed before the FAISS->SQLite migration. Face search will "
                "return 'not found' for these faces until embeddings are regenerated. "
                "Two recovery paths: (a) run internal/scripts/port_faiss_to_sqlite.py to import "
                "vectors from legacy index/face_vec.faiss while preserving manual "
                "labels (face.person_id), or (b) run the indexer with "
                "--reprocess-faces to re-detect from images (faster than option a if "
                "FAISS files were lost, but drops any manual labels)."
            )
    except Exception as exc:
        logger.debug(f"Could not check orphan face_embedding count: {exc}")
    models_dir = getattr(config, 'models_dir', None)
    embedder = ClipEmbedder(config.model_name, config.pretrained, config.device,
                            cache_dir=models_dir)
    logger.info(f"CLIP model ({config.model_name}) running on {get_device_label(config.device)}")
    # Embeddings are stored as BLOBs in the image_embedding /
    # keyframe_embedding / face_embedding tables, written per file
    # in the same per-batch SQLite transaction as the metadata.
    # See internal/docs/storage/SQLITE_INCREMENTAL_VISIBILITY_PLAN.md (Stage 3 / Part B).
    image_embedding_count = 0
    face_embedding_count = 0
    keyframe_embedding_count = 0

    # Object detection init (optional, based on config)
    object_detector = None
    det_setting = getattr(config, "enable_object_detection", False)
    on_cpu = config.device == "cpu"
    should_detect = det_setting is True or (det_setting == "auto" and not on_cpu)

    if should_detect:
        try:
            obj_backend = getattr(config, "object_detector_backend", "rtdetr")
            obj_model   = getattr(config, "object_model", "PekingU/rtdetr_r18vd")
            obj_conf    = getattr(config, "object_confidence_threshold", 0.35)
            if on_cpu and det_setting is True:
                logger.warning(
                    "Object detection enabled on CPU (backend={}, model={}). "
                    "Indexing will be slow — expect ~{}ms per image. "
                    "Set enable_object_detection: auto to skip on CPU automatically.",
                    obj_backend, obj_model,
                    700,
                )
            object_detector = ObjectDetector(
                model_name=obj_model,
                device=config.device,
                conf_threshold=obj_conf,
                model_dir=models_dir,
                backend=obj_backend,
            )
            logger.info(
                "Object detection: backend={} model={} device={}",
                obj_backend, obj_model, get_device_label(config.device),
            )
        except Exception as e:
            logger.warning("Failed to initialize object detector: {}. Continuing without object detection.", e)
    elif on_cpu and det_setting == "auto":
        logger.info(
            "Object detection SKIPPED on CPU (set enable_object_detection: true to force it on, "
            "or use a GPU for automatic enabling)."
        )
    else:
        logger.info("Object detection DISABLED (enable_object_detection: false in config.yaml)")
    
    # Face recognition init (optional, based on config)
    face_recognizer = None
    face_model_id = "unknown"
    if hasattr(config, 'enable_face_recognition') and config.enable_face_recognition:
        try:
            from .models.faces import FaceRecognizer
            face_model = getattr(config, 'face_model', 'vggface2')
            face_conf = getattr(config, 'face_confidence_threshold', 0.95)
            face_min_size = getattr(config, 'face_min_size', 60)
            face_backend = getattr(config, 'face_recognizer_backend', 'facenet_pytorch')
            face_recognizer = FaceRecognizer(
                model_name=face_model,
                device=config.device,
                conf_threshold=face_conf,
                min_face_size=face_min_size,
                model_root=models_dir,
                backend=face_backend,
            )
            face_model_id = f"{face_backend}:{face_model}"
            logger.info(f"Face recognition ({face_backend}/{face_model}) running on {get_device_label(config.device)}")
            logger.info(f"Face recognition ENABLED with backend={face_backend} model={face_model} threshold={face_conf} min_size={face_min_size}")
        except Exception as e:
            logger.error(
                f"Failed to initialize face recognizer: {e}. "
                "Check that facenet-pytorch is installed and the model cache is intact. "
                "Fix the error or set enable_face_recognition: false in config.yaml to skip faces."
            )
            raise
    else:
        logger.info("Face recognition DISABLED (set enable_face_recognition: true in config.yaml to enable)")
    
    # Scene detector (shot detection) info
    # PySceneDetect uses OpenCV and runs on CPU
    # Skip initialization if image_only mode (not needed for images)
    if getattr(config, 'image_only', False):
        logger.info("Scene detector SKIPPED (image_only mode - not needed for images)")
    elif hasattr(config, 'enable_video_shot_detection') and config.enable_video_shot_detection:
        try:
            from .io.shot_detection import detect_shots, ContentDetector
            # Check if PySceneDetect is available
            if ContentDetector is not None:
                shot_threshold = getattr(config, 'shot_detection_threshold', 30.0)
                shot_min_len = getattr(config, 'min_shot_length_frames', 15)
                logger.info(f"Scene detector (PySceneDetect) running on CPU (threshold={shot_threshold}, min_length={shot_min_len} frames)")
            else:
                logger.warning("Scene detector (PySceneDetect) is not available. Install 'scenedetect[opencv]' to enable.")
        except ImportError:
            logger.warning("Scene detector (PySceneDetect) is not available. Install 'scenedetect[opencv]' to enable.")
    else:
        logger.info("Scene detector DISABLED (set enable_video_shot_detection: true in config.yaml to enable)")
    
    file_count = 0
    img_count = 0
    vid_count = 0
    embed_ok = embed_fail = 0
    tag_items = 0
    skipped_count = 0
    skipped_up_to_date_count = 0
    skipped_filtered_count = 0
    files_processed_with_faces = 0  # Track files that were processed with face detection enabled
    # Performance tracking
    processed_img_count = 0  # Images that were actually processed (not skipped)
    processed_vid_count = 0  # Videos that were actually processed (not skipped)
    total_img_time = 0.0  # Total time spent processing images
    total_vid_time = 0.0  # Total time spent processing videos
    total_video_duration = 0.0  # Total duration of processed videos (in seconds)
    total_shots = 0  # Total shots detected across all videos
    
    # Iterate through all enabled sources
    for source in sources_to_index:
        root = Path(resolve_for_access(str(source.path)))
        source_name = source.name
        logger.info(f"📂 Indexing source: {source_name} | path: {root}")
        for p in iter_media(root):
            # Check for graceful shutdown between files
            if stop_event is not None and stop_event.is_set():
                logger.info("Stop requested — finishing indexing run early (FAISS will still be saved)")
                break

            # Track time for each file
            file_start_time = time.perf_counter()
            
            ext = p.suffix.lower()
            media_id = sha256_of_file(p)
            file_count += 1
            # Count images and videos as they are discovered (before skip check)
            if ext in IMAGE_EXT:
                img_count += 1
            elif ext in VIDEO_EXT:
                vid_count += 1
            # Truncate media_id: first 5 + .. + last 5 chars
            if len(media_id) > 10:
                short_media_id = f"{media_id[:5]}..{media_id[-5:]}"
            else:
                short_media_id = media_id
            # Show relative path from project root, prefer ../data/ if present
            try:
                rel_path = p.relative_to(Path.cwd())
            except Exception:
                rel_path = p
            rel_path_str = str(rel_path)
            # If ../data/ in path, show from there
            data_idx = rel_path_str.find("data/")
            if data_idx != -1:
                rel_path_str = "../" + rel_path_str[data_idx:]
            log = with_ctx(media_id=short_media_id, path=rel_path_str)
            log.debug(f"Discovered media file ext={ext}")

            # Skip based on media type filtering flags (before any processing)
            if ext in IMAGE_EXT and getattr(config, 'video_only', False):
                log.info(f"Skipping image (video_only mode): {p.name}")
                skipped_count += 1
                skipped_filtered_count += 1
                continue
            elif ext in VIDEO_EXT and getattr(config, 'image_only', False):
                log.info(f"Skipping video (image_only mode): {p.name}")
                skipped_count += 1
                skipped_filtered_count += 1
                continue

            # Check processing status to determine what needs to be done
            status = db.get_processing_status(media_id)
            needs_gps = bool(getattr(config, 'reprocess_gps', False)) or not status.get("gps_processed", False)
            needs_objects = bool(getattr(config, 'reprocess_objects', False)) or not status.get("object_detection_done", False)
            needs_faces = bool(getattr(config, 'reprocess_faces', False)) or not status.get("face_detection_done", False)
            needs_embeddings = bool(getattr(config, 'reprocess_embeddings', False)) or (status.get("embeddings_version") != getattr(config, 'model_version', None))

            # Stage 3 upgrade-path backfill: a pre-Stage-3 DB has the
            # status flags set above but no rows in the new embedding
            # tables. Trigger CLIP re-embedding (cheap, idempotent —
            # writes overwrite via ON CONFLICT) for those rows so the
            # incremental fast path doesn't silently leave them stale.
            # Faces are NOT auto-backfilled because re-running face
            # detection would risk losing manually-applied labels;
            # the orphan count is logged once at indexer-run startup
            # so the user can address it explicitly.
            media_exists = db.media_exists(media_id)
            if media_exists and not needs_embeddings:
                if ext in IMAGE_EXT and not db.has_image_embedding(media_id):
                    needs_embeddings = True
                elif ext in VIDEO_EXT and db.media_has_unembedded_keyframes(media_id):
                    needs_embeddings = True

            # Optimization: Skip files that don't need ANY processing
            # This only helps when no reprocessing flags are set (normal incremental indexing)
            if media_exists and not (needs_gps or needs_objects or needs_faces or needs_embeddings):
                log.info("Skipping - no processing needed")
                skipped_count += 1
                skipped_up_to_date_count += 1
                continue

            # Track what was processed for this file (for summary log)
            processed_gps = None  # None = not processed, True = GPS data found, False = processed but no GPS data
            processed_face_count = 0
            processed_object_count = 0
            processed_shot_count = 0
            processed_embedding = False
            gps_data_mode = None
            video_gps_samples = []
            
            # EXIF / video meta (always read for new items, or if GPS reprocessing requested)
            meta = {}
            if ext in IMAGE_EXT:
                if needs_gps or not media_exists:
                    meta.update(get_exif_basic(p))
            elif ext in VIDEO_EXT:
                if needs_gps or not media_exists:
                    extract_gopro_gps_track = should_extract_video_gps_track(p)
                    meta.update(get_video_meta(p))
                    if extract_gopro_gps_track:
                        video_gps_samples = extract_video_gps_track(p)
                        if video_gps_samples and (meta.get("gps_lat") is None or meta.get("gps_lon") is None):
                            meta["gps_lat"] = video_gps_samples[0]["gps_lat"]
                            meta["gps_lon"] = video_gps_samples[0]["gps_lon"]

            # Fallback to file modification time if no timestamp from EXIF/metadata
            if not meta.get("ts_utc") and not media_exists:
                from datetime import datetime
                mtime = p.stat().st_mtime
                meta["ts_utc"] = datetime.fromtimestamp(mtime).isoformat()

            # Reverse geocoding: convert GPS to place name
            place_name = None
            if needs_gps and meta.get("gps_lat") is not None and meta.get("gps_lon") is not None:
                place_name = get_place_name(meta["gps_lat"], meta["gps_lon"])
                if place_name:
                    log.debug(f"Reverse geocoded to: {place_name}")
            if needs_gps:
                if ext in VIDEO_EXT and video_gps_samples:
                    gps_data_mode = "media_static_plus_keyframe" if meta.get("gps_lat") is not None and meta.get("gps_lon") is not None else "keyframe_representative"
                elif meta.get("gps_lat") is not None and meta.get("gps_lon") is not None:
                    gps_data_mode = "media_static"
                else:
                    gps_data_mode = "none"

            # Thumbnail: generate for new files only.
            # Named <media_id>.jpg to avoid filename collisions (e.g. DSC_1248.JPG in many folders).
            if not media_exists:
                if ext in IMAGE_EXT:
                    write_thumbnail(p, config.thumb_dir, media_id)
                    log.debug("Wrote image thumbnail")
                elif ext in VIDEO_EXT:
                    write_video_thumbnail(p, config.thumb_dir, media_id)
                    log.debug("Wrote video thumbnail")

            # upsert media row - only if new file or updating GPS/metadata
            # Skip metadata upsert when doing selective feature reprocessing (faces, objects, embeddings)
            should_upsert = not media_exists or needs_gps
            if should_upsert:
                # Compute relative path to current source root for portability
                try:
                    rel_path = str(p.relative_to(root))
                except Exception:
                    # Fallback: just the basename if file is outside root for some reason
                    rel_path = p.name
                row = dict(
                    media_id=media_id, path=str(p), source_name=source_name, rel_path=rel_path, size_bytes=p.stat().st_size,
                    mime=("image" if ext in IMAGE_EXT else "video") + "/" + ext.strip("."),
                    ts_utc=meta.get("ts_utc") if meta.get("ts_utc") else None,
                    place=place_name, camera=meta.get("camera"), lens=meta.get("lens"),
                    width=meta.get("width"), height=meta.get("height"),
                    duration=meta.get("duration"), hash_blake3=None
                )
                # Only write GPS if we extracted it this run (avoid overwriting with None if reprocessing other stages)
                if needs_gps and meta:
                    row["gps_lat"] = meta.get("gps_lat")
                    row["gps_lon"] = meta.get("gps_lon")
                # Mark GPS as processed if we attempted extraction, regardless of whether GPS data was found
                # Absence of GPS data can be determined later by checking if lat/lon values are None
                if needs_gps:
                    row["gps_processed"] = 1
                    row["gps_data_mode"] = gps_data_mode
                    # Track whether GPS data was actually found
                    gps_lat = meta.get("gps_lat")
                    gps_lon = meta.get("gps_lon")
                    processed_gps = bool(video_gps_samples) or (gps_lat is not None and gps_lon is not None)
                db.upsert_media(row)
                log.debug(f"Upserted media row: {row}")
        
            # Process images (embeddings, object detection, face detection)
            if ext in IMAGE_EXT:
                # Load image if needed for any processing stage
                img = None
                if needs_embeddings or (object_detector and needs_objects) or (face_recognizer and needs_faces):
                    try:
                        from PIL import ImageOps
                        img_raw = Image.open(p).convert("RGB")
                        # Apply EXIF orientation correction to ensure faces are detected in correct orientation
                        img = ImageOps.exif_transpose(img_raw) if img_raw else img_raw
                    except Exception as e:
                        log.error(f"Failed to load image: {e}")
                
                # Embeddings
                if needs_embeddings and img is not None:
                    try:
                        v = embedder.image_embed([img])[0]
                        db.upsert_image_embedding(media_id, v, model=config.model_version)
                        image_embedding_count += 1
                        embed_ok += 1
                        processed_embedding = True
                        log.debug("Image embedding generated")
                        # Mark embeddings done
                        db.mark_embeddings_done(media_id, config.model_version)
                    except Exception as e:
                        embed_fail += 1
                        log.warning(f"Image embedding failed: {e}")
                
                # Object detection and tagging - only if needed
                if object_detector and needs_objects and img is not None:
                    try:
                        object_labels = object_detector.get_labels(img)
                        if object_labels:
                            db.add_tags(media_id, object_labels)
                            tag_items += 1
                            processed_object_count = len(object_labels)
                            log.debug(f"Object tags added count={len(object_labels)} labels={object_labels}")
                        # Mark object detection as done (even if no objects found)
                        db.mark_object_detection_done(media_id)
                    except Exception as e:
                        log.warning(f"Object detection failed: {e}")
                
                # Face detection and recognition - only if needed
                if face_recognizer and needs_faces and img is not None:
                    files_processed_with_faces += 1
                    try:
                        # Delete old faces when reprocessing to avoid orphaned face_ids
                        if getattr(config, 'reprocess_faces', False):
                            db.delete_faces_for_media(media_id)
                            log.debug("Deleted old face records for reprocessing")
                        
                        # Run face detection (reprocess or first time)
                        log.debug("Running face detection on image...")
                        faces = face_recognizer.detect_and_embed(img)
                        if faces:
                            face_entries = []
                            face_emb_pairs: list[tuple[str, object]] = []
                            for i, face in enumerate(faces):
                                face_id = f"{media_id}:f{i}"
                                face_entries.append({
                                    "face_id": face_id,
                                    "bbox": face.bbox,
                                    "confidence": face.confidence,
                                    "gender": face.metadata.get("gender"),
                                    "age": face.metadata.get("age"),
                                })
                                face_emb_pairs.append((face_id, face.embedding))
                                # Generate face thumbnail
                                write_face_thumbnail(img, face.bbox, face_id, config.face_thumb_dir)
                            db.add_faces(media_id, face_entries)
                            # face rows must exist before face_embedding rows
                            # for the FK to be satisfied — order matters here.
                            for fid, emb in face_emb_pairs:
                                db.upsert_face_embedding(fid, emb, model=face_model_id)
                                face_embedding_count += 1
                            processed_face_count = len(faces)
                            log.debug(f"Detected {len(faces)} faces in image")
                        else:
                            log.debug("No faces detected in image")
                        # Mark face detection as done (even if no faces found)
                        db.mark_face_detection_done(media_id)
                    except Exception as e:
                        log.warning(f"Face detection failed: {e}")
                
                # Summary log for image processing
                file_elapsed = time.perf_counter() - file_start_time
                file_size = p.stat().st_size
                # Format file size: show MB if >= 1MB, KB if >= 1KB, otherwise bytes
                if file_size >= 1024 * 1024:
                    size_str = f"{file_size / (1024 * 1024):.2f}MB"
                elif file_size >= 1024:
                    size_str = f"{file_size / 1024:.2f}KB"
                else:
                    size_str = f"{file_size}B"
                
                parts = []
                if processed_gps is not None:
                    parts.append("gps" if processed_gps else "gps=none")
                if processed_embedding:
                    parts.append("embedding")
                if processed_object_count > 0:
                    parts.append(f"object={processed_object_count}")
                if processed_face_count > 0:
                    parts.append(f"face={processed_face_count}")
                if parts:
                    # Format time: show seconds if >= 1, otherwise milliseconds
                    if file_elapsed >= 1.0:
                        time_str = f"{file_elapsed:.2f}s"
                    else:
                        time_str = f"{file_elapsed*1000:.0f}ms"
                    log.info(f"Processed: {', '.join(parts)} size={size_str} ({time_str})")
                    
                    # Track performance metrics
                    processed_img_count += 1
                    total_img_time += file_elapsed
            
            # Videos: shot detection and per-shot keyframes (no representative frame to image_emb)
            elif ext in VIDEO_EXT:
                try:
                    # Check if shots already exist in database
                    existing_shots = db.get_shots_for_video(media_id)
                    has_keyframes = db.has_keyframes_for_video(media_id)

                    # For videos, the historical heuristic was: if keyframes
                    # exist, embeddings and object-detection tags are implicitly
                    # done (in the FAISS era keyframes couldn't exist without
                    # vectors). Stage 3 broke that invariant — a pre-Stage-3
                    # upgrade has video_keyframes rows but no keyframe_embedding
                    # rows. Detect that case explicitly so we still re-run the
                    # video path on those keyframes.
                    reprocess_embeddings = getattr(config, 'reprocess_embeddings', False)
                    reprocess_objects = getattr(config, 'reprocess_objects', False)
                    needs_keyframe_emb_backfill = (
                        has_keyframes and db.media_has_unembedded_keyframes(media_id)
                    )
                    video_needs_embeddings = (
                        not has_keyframes
                        or reprocess_embeddings
                        or needs_keyframe_emb_backfill
                    ) and needs_embeddings
                    video_needs_objects = (not has_keyframes or reprocess_objects) and needs_objects
                    
                    # Debug: log why video is or isn't being skipped
                    log.debug(f"Video skip check: existing_shots={bool(existing_shots)} has_keyframes={has_keyframes} video_needs_embeddings={video_needs_embeddings} video_needs_objects={video_needs_objects} needs_faces={needs_faces}")
                    
                    # If shots and keyframes exist, and no processing is needed, skip the video
                    if existing_shots and has_keyframes and not (video_needs_embeddings or video_needs_objects or needs_faces):
                        log.info("Skipping - no processing needed")
                        skipped_count += 1
                        skipped_up_to_date_count += 1
                        continue

                    # Timing variables for video processing breakdown
                    scene_detect_time = 0.0
                    other_ops_time = 0.0
                    
                    if existing_shots:
                        log.debug(f"Using existing shots from database count={len(existing_shots)}")
                        shots = [(s['t_start'], s['t_end']) for s in existing_shots]
                        is_synthetic = False  # Already stored
                        processed_shot_count = len(existing_shots)
                        # No scene detection time since we're using existing shots
                    else:
                        # Run shot detection for videos
                        log.debug("Starting shot detection for video")
                        scene_detect_start = time.perf_counter()
                        shots = detect_shots(
                            p,
                            threshold=float(getattr(config, 'shot_detection_threshold', 30.0)),
                            min_scene_len=int(getattr(config, 'min_shot_length_frames', 15)),
                        )
                        scene_detect_time = time.perf_counter() - scene_detect_start
                        log.debug(f"Shot detection complete count={len(shots)} time={scene_detect_time:.2f}s")
                        
                        is_synthetic = False
                        if not shots:
                            # Fallback: create synthetic full-video shot
                            duration = meta.get("duration")
                            if duration and duration > 0:
                                shots = [(0.0, duration)]
                                is_synthetic = True
                                log.debug(f"No shots detected, created synthetic full-video shot duration={duration:.2f}s")
                            else:
                                log.warning("No shots detected and no duration metadata - skipping video keyframe extraction")
                        
                        if shots:
                            # Store shots in SQLite (with synthetic flag)
                            db.add_shots(media_id, shots, is_synthetic=is_synthetic)
                            processed_shot_count = len(shots)

                    # Time the rest of video processing operations (keyframes, embedding, object detection, face detection)
                    # This applies whether shots came from database or were newly detected
                    if shots:
                        other_ops_start = time.perf_counter()
                        
                        # Extract keyframes; metadata + per-keyframe embedding go to SQLite
                        kps = int(getattr(config, 'keyframes_per_shot', 1))
                        enable_video_detection = object_detector and getattr(config, 'enable_video_object_detection', False)
                        geocode_cache = {}
                        
                        # Delete old faces when reprocessing to avoid orphaned face_ids
                        if face_recognizer and needs_faces and getattr(config, 'reprocess_faces', False):
                            db.delete_faces_for_media(media_id)
                            log.debug("Deleted old face records for video reprocessing")
                        
                        keyframe_entries = []
                        # (shot_index, kf_index, embedding) tuples — embeddings
                        # are inserted into keyframe_embedding *after* add_keyframes
                        # populates video_keyframes (FK requirement).
                        kf_emb_pairs: list[tuple[int, int, object]] = []
                        first_keyframe_place = None
                        has_keyframe_gps = False
                        for s_idx, shot in enumerate(shots):
                            keyframes = extract_keyframes_from_shot(p, shot, keyframes_per_shot=kps)
                            for k_idx, (t_sec, img) in enumerate(keyframes):
                                if img is None:
                                    continue
                                vec_kf = embedder.image_embed([img])[0]
                                kf_emb_pairs.append((s_idx, k_idx, vec_kf))
                                processed_embedding = True
                                
                                # Per-keyframe object detection
                                keyframe_tags = []
                                if enable_video_detection and object_detector:
                                    try:
                                        keyframe_tags = object_detector.get_labels(img)
                                        if keyframe_tags:
                                            tag_items += 1
                                            processed_object_count += len(keyframe_tags)
                                            log.debug(f"Keyframe {s_idx}:{k_idx} objects: {keyframe_tags}")
                                    except Exception as e:
                                        log.warning(f"Object detection failed for keyframe {s_idx}:{k_idx}: {e}")
                                
                                # Per-keyframe face detection
                                if face_recognizer and needs_faces:
                                    # Track that we're processing this video with face detection
                                    # (only count once per video, not per keyframe)
                                    if s_idx == 0 and k_idx == 0:
                                        files_processed_with_faces += 1
                                    try:
                                        log.debug(f"Running face detection on video keyframe {s_idx}:{k_idx}...")
                                        faces = face_recognizer.detect_and_embed(img)
                                        if faces:
                                            face_entries = []
                                            face_emb_pairs: list[tuple[str, object]] = []
                                            for f_idx, face in enumerate(faces):
                                                face_id = f"vf:{media_id}:{s_idx}:{k_idx}:f{f_idx}"
                                                face_entries.append({
                                                    "face_id": face_id,
                                                    "bbox": face.bbox,
                                                    "confidence": face.confidence,
                                                    "gender": face.metadata.get("gender"),
                                                    "age": face.metadata.get("age"),
                                                    "shot_index": s_idx,
                                                    "kf_index": k_idx,
                                                })
                                                face_emb_pairs.append((face_id, face.embedding))
                                                # Generate face thumbnail
                                                write_face_thumbnail(img, face.bbox, face_id, config.face_thumb_dir)
                                            db.add_faces(media_id, face_entries)
                                            # face rows must exist before face_embedding rows
                                            for fid, emb in face_emb_pairs:
                                                db.upsert_face_embedding(fid, emb, model=face_model_id)
                                                face_embedding_count += 1
                                            processed_face_count += len(faces)
                                            log.debug(f"Keyframe {s_idx}:{k_idx} detected {len(faces)} faces")
                                        else:
                                            log.debug(f"No faces detected in keyframe {s_idx}:{k_idx}")
                                    except Exception as e:
                                        log.warning(f"Face detection failed for keyframe {s_idx}:{k_idx}: {e}")
                                
                                # Metadata row for SQLite
                                gps_payload = sample_video_gps_at_timestamp(video_gps_samples, float(t_sec)) if video_gps_samples else None
                                keyframe_place = None
                                if gps_payload and gps_payload.get("gps_lat") is not None and gps_payload.get("gps_lon") is not None:
                                    cache_key = (
                                        round(float(gps_payload["gps_lat"]), 3),
                                        round(float(gps_payload["gps_lon"]), 3),
                                    )
                                    if cache_key not in geocode_cache:
                                        geocode_cache[cache_key] = get_place_name(
                                            gps_payload["gps_lat"], gps_payload["gps_lon"]
                                        )
                                    keyframe_place = geocode_cache[cache_key]
                                    has_keyframe_gps = True
                                    if first_keyframe_place is None and keyframe_place:
                                        first_keyframe_place = keyframe_place
                                keyframe_entries.append({
                                    "shot_index": int(s_idx),
                                    "kf_index": int(k_idx),
                                    "timestamp": float(t_sec),
                                    "shot_start": float(shot[0]),
                                    "shot_end": float(shot[1]),
                                    "tags": keyframe_tags,
                                    "gps_lat": gps_payload.get("gps_lat") if gps_payload else None,
                                    "gps_lon": gps_payload.get("gps_lon") if gps_payload else None,
                                    "gps_alt": gps_payload.get("gps_alt") if gps_payload else None,
                                    "gps_datetime_utc": gps_payload.get("gps_datetime_utc") if gps_payload else None,
                                    "gps_fix": gps_payload.get("gps_fix") if gps_payload else None,
                                    "gps_source": gps_payload.get("gps_source") if gps_payload else None,
                                    "place": keyframe_place,
                                })
                        if kf_emb_pairs:
                            # Insert keyframe metadata first so the FK from
                            # keyframe_embedding -> video_keyframes(id) holds.
                            db.add_keyframes(media_id, keyframe_entries)
                            for s_idx_kf, k_idx_kf, vec_kf in kf_emb_pairs:
                                kf_id = db.get_keyframe_id(media_id, s_idx_kf, k_idx_kf)
                                if kf_id is None:
                                    log.warning(
                                        f"Keyframe row missing after add_keyframes "
                                        f"(media_id={media_id}, shot={s_idx_kf}, kf={k_idx_kf})"
                                    )
                                    continue
                                db.upsert_keyframe_embedding(
                                    kf_id, vec_kf, model=config.model_version
                                )
                                keyframe_embedding_count += 1
                            if needs_gps:
                                if has_keyframe_gps:
                                    gps_mode = "media_static_plus_keyframe" if meta.get("gps_lat") is not None and meta.get("gps_lon") is not None else "keyframe_representative"
                                elif meta.get("gps_lat") is not None and meta.get("gps_lon") is not None:
                                    gps_mode = "media_static"
                                else:
                                    gps_mode = "none"
                                update_fields = {"gps_data_mode": gps_mode}
                                if not place_name and first_keyframe_place:
                                    update_fields["place"] = first_keyframe_place
                                db.update_media_fields(media_id, update_fields)
                            embed_ok += 1  # Count as successful video embedding
                            log.debug(f"Stored video keyframes count={len(kf_emb_pairs)} shots={len(shots)}")
                        else:
                            log.warning(f"No valid keyframes extracted from {len(shots)} shots")
                            embed_fail += 1
                        
                        # Mark face detection as done for video (if face recognizer is enabled and we processed faces)
                        if face_recognizer and needs_faces:
                            db.mark_face_detection_done(media_id)
                        
                        # Mark object detection as done for video (if object detection was enabled and run)
                        if enable_video_detection and needs_objects:
                            db.mark_object_detection_done(media_id)
                        
                        other_ops_time = time.perf_counter() - other_ops_start
                        
                        # Summary log for video processing
                        file_elapsed = time.perf_counter() - file_start_time
                        file_size = p.stat().st_size
                        # Format file size: show MB if >= 1MB, KB if >= 1KB, otherwise bytes
                        if file_size >= 1024 * 1024:
                            size_str = f"{file_size / (1024 * 1024):.2f}MB"
                        elif file_size >= 1024:
                            size_str = f"{file_size / 1024:.2f}KB"
                        else:
                            size_str = f"{file_size}B"
                        
                        # Format duration: show minutes:seconds if >= 1min, otherwise seconds
                        duration = meta.get("duration")
                        if duration:
                            if duration >= 60:
                                duration_str = f"{int(duration // 60)}m{int(duration % 60)}s"
                            else:
                                duration_str = f"{duration:.1f}s"
                        else:
                            duration_str = None
                        
                        parts = []
                        if processed_gps is not None:
                            parts.append("gps" if processed_gps else "gps=none")
                        if processed_shot_count > 0:
                            parts.append(f"shot={processed_shot_count}")
                        if processed_embedding:
                            parts.append("embedding")
                        if processed_object_count > 0:
                            parts.append(f"object={processed_object_count}")
                        if processed_face_count > 0:
                            parts.append(f"face={processed_face_count}")
                        if parts:
                            # Format time: show seconds if >= 1, otherwise milliseconds
                            if file_elapsed >= 1.0:
                                time_str = f"{file_elapsed:.2f}s"
                            else:
                                time_str = f"{file_elapsed*1000:.0f}ms"
                            
                            # Add timing breakdown for video processing
                            timing_parts = []
                            if scene_detect_time > 0:
                                if scene_detect_time >= 1.0:
                                    timing_parts.append(f"scene={scene_detect_time:.2f}s")
                                else:
                                    timing_parts.append(f"scene={scene_detect_time*1000:.0f}ms")
                            if other_ops_time > 0:
                                if other_ops_time >= 1.0:
                                    timing_parts.append(f"other={other_ops_time:.2f}s")
                                else:
                                    timing_parts.append(f"other={other_ops_time*1000:.0f}ms")
                            
                            timing_str = f" ({', '.join(timing_parts)})" if timing_parts else ""
                            
                            # Build log message with size and duration
                            info_parts = [f"Processed: {', '.join(parts)}", f"size={size_str}"]
                            if duration_str:
                                info_parts.append(f"duration={duration_str}")
                            info_parts.append(f"({time_str}{timing_str})")
                            log.info(" ".join(info_parts))
                            
                            # Track performance metrics
                            processed_vid_count += 1
                            total_vid_time += file_elapsed
                            if duration:
                                total_video_duration += duration
                            if processed_shot_count > 0:
                                total_shots += processed_shot_count
                except Exception as e:
                    embed_fail += 1
                    log.error(f"Video shot detection/indexing failed: {e}", exc_info=True)

            # Per-batch commit boundary. Runs after this file's full processing
            # (media + faces + tags) has landed in the open SQLite transaction.
            files_since_commit += 1
            now_ts = time.perf_counter()
            if (
                files_since_commit >= commit_batch_files
                or (now_ts - last_commit_ts) >= commit_batch_seconds
            ):
                commit_t0 = time.perf_counter()
                try:
                    db.commit()
                except Exception as commit_exc:
                    logger.error(f"Per-batch commit failed: {commit_exc}")
                    db.rollback()
                    raise
                commit_ms = int((time.perf_counter() - commit_t0) * 1000)
                since_last_ms = int((now_ts - last_commit_ts) * 1000)
                logger.info(
                    f"BATCH_COMMIT files={files_since_commit} "
                    f"commit_ms={commit_ms} since_last_ms={since_last_ms} "
                    f"batch_serial={commit_batch_serial}"
                )
                commit_count += 1
                total_commit_ms += commit_ms
                inter_commit_ms_list.append(since_last_ms)
                files_since_commit = 0
                last_commit_ts = time.perf_counter()
                commit_batch_serial += 1

        # If stop was requested mid-source, don't continue to the next source
        if stop_event is not None and stop_event.is_set():
            break

    # Embeddings are now durably committed per file via image_embedding /
    # keyframe_embedding / face_embedding tables, so there is no separate
    # end-of-run save step. The local_index_changed signal collapses to
    # "did SQLite get any net writes this run", which already covers
    # metadata, faces, keyframes, and embeddings.
    face_count = face_embedding_count
    try:
        if face_count == 0 and face_recognizer and files_processed_with_faces > 0:
            logger.warning(
                f"No faces detected in {files_processed_with_faces} processed file(s) with "
                "face detection enabled. If you expected faces, check your config and input data."
            )
        if image_embedding_count > 0 or keyframe_embedding_count > 0 or face_count > 0:
            logger.info(
                f"Wrote embeddings to SQLite: image={image_embedding_count} "
                f"keyframe={keyframe_embedding_count} face={face_count}"
            )

        db_changed = db.get_total_changes() > initial_db_total_changes
        local_index_changed = db_changed
        committed_index_version: dict | None = None
        if local_index_changed:
            committed_index_version = db.bump_index_version()
            logger.info(
                "Local index version advanced seq={} ts={}",
                committed_index_version["index_version_seq"],
                committed_index_version["index_version_ts"],
            )

        db.commit()
    except Exception as e:
        logger.error(f"Failed to finalize local index checkpoint: {e}")
        db.rollback()
        raise
    
    # Calculate total elapsed time
    run_elapsed = time.perf_counter() - run_start_time
    # Format time: show hours if >= 1, minutes if >= 1, otherwise seconds
    if run_elapsed >= 3600:
        time_str = f"{run_elapsed/3600:.2f}h"
    elif run_elapsed >= 60:
        time_str = f"{run_elapsed/60:.2f}m"
    else:
        time_str = f"{run_elapsed:.2f}s"
    
    logger.info(f"Indexing complete | files={file_count} images={img_count} videos={vid_count} embed_ok={embed_ok} embed_fail={embed_fail} tagged_items={tag_items} faces={face_count} skipped={skipped_count} time={time_str}")
    
    # Performance summary
    perf_parts = [f"total_time={time_str}"]
    
    if processed_img_count > 0:
        avg_img_time = total_img_time / processed_img_count
        if avg_img_time >= 1.0:
            avg_img_str = f"{avg_img_time:.2f}s"
        else:
            avg_img_str = f"{avg_img_time*1000:.0f}ms"
        perf_parts.append(f"avg_image={avg_img_str}")
    
    if processed_vid_count > 0:
        avg_vid_time = total_vid_time / processed_vid_count
        if avg_vid_time >= 1.0:
            avg_vid_str = f"{avg_vid_time:.2f}s"
        else:
            avg_vid_str = f"{avg_vid_time*1000:.0f}ms"
        perf_parts.append(f"avg_video={avg_vid_str}")
        
        if total_video_duration > 0:
            # Calculate time per minute of video
            time_per_minute = (total_vid_time / total_video_duration) * 60
            if time_per_minute >= 1.0:
                time_per_min_str = f"{time_per_minute:.2f}s/min"
            else:
                time_per_min_str = f"{time_per_minute*1000:.0f}ms/min"
            perf_parts.append(f"avg_video_per_min={time_per_min_str}")
    
    logger.info(f"Performance | {' '.join(perf_parts)}")
    
    # Video processing statistics
    if processed_vid_count > 0:
        vid_stats_parts = [f"videos={processed_vid_count}"]
        
        if total_video_duration > 0:
            # Format total duration
            if total_video_duration >= 3600:
                total_dur_str = f"{total_video_duration/3600:.2f}h"
            elif total_video_duration >= 60:
                total_dur_str = f"{total_video_duration/60:.2f}m"
            else:
                total_dur_str = f"{total_video_duration:.1f}s"
            vid_stats_parts.append(f"total_duration={total_dur_str}")
            
            # Calculate average duration
            avg_duration = total_video_duration / processed_vid_count
            if avg_duration >= 60:
                avg_dur_str = f"{int(avg_duration // 60)}m{int(avg_duration % 60)}s"
            else:
                avg_dur_str = f"{avg_duration:.1f}s"
            vid_stats_parts.append(f"avg_duration={avg_dur_str}")
        
        if total_shots > 0:
            vid_stats_parts.append(f"total_shots={total_shots}")
            avg_shots = total_shots / processed_vid_count
            vid_stats_parts.append(f"avg_shots={avg_shots:.1f}")
        
        logger.info(f"Video processing | {' '.join(vid_stats_parts)}")

    # Per-batch commit telemetry: aggregate stats over the run plus a
    # self-checking acceptance log. Emitted as its own INDEXER_SUMMARY phase
    # so the existing log parser in IndexerManager picks it up.
    total_wall_ms = int((time.perf_counter() - run_start_time) * 1000)
    commit_pct = (total_commit_ms / total_wall_ms) if total_wall_ms > 0 else 0.0
    sorted_inter = sorted(inter_commit_ms_list)
    if sorted_inter:
        p50_inter = sorted_inter[len(sorted_inter) // 2]
        p95_idx = max(0, min(len(sorted_inter) - 1, int(len(sorted_inter) * 0.95)))
        p95_inter = sorted_inter[p95_idx]
        max_inter = sorted_inter[-1]
    else:
        p50_inter = p95_inter = max_inter = 0
    _emit_indexer_summary(
        phase="commit_stats",
        total_commits=commit_count,
        total_commit_ms=total_commit_ms,
        wall_clock_ms=total_wall_ms,
        commit_pct=round(commit_pct, 6),
        p50_inter_commit_ms=p50_inter,
        p95_inter_commit_ms=p95_inter,
        max_inter_commit_ms=max_inter,
    )
    if commit_count > 0:
        if commit_pct > 0.01:
            logger.warning(
                f"Commit overhead {commit_pct * 100:.2f}% > 1% — consider raising "
                f"MSA_INDEXER_COMMIT_BATCH_FILES (currently {commit_batch_files})"
            )
        if p50_inter > 30_000:
            logger.warning(
                f"Median inter-commit time {p50_inter}ms > 30s — consider lowering "
                f"MSA_INDEXER_COMMIT_BATCH_SECONDS (currently {commit_batch_seconds:.1f}s)"
            )

    _emit_indexer_summary(
        phase="complete",
        total_found=file_count,
        already_indexed=skipped_up_to_date_count,
        needs_processing=processed_img_count + processed_vid_count,
        processed_images=processed_img_count,
        processed_videos=processed_vid_count,
        skipped=skipped_up_to_date_count,
        faces=face_count,
        tagged_items=tag_items,
        avg_image_seconds=(total_img_time / processed_img_count) if processed_img_count > 0 else None,
        avg_video_seconds=(total_vid_time / processed_vid_count) if processed_vid_count > 0 else None,
        avg_video_seconds_per_min=((total_vid_time / total_video_duration) * 60.0) if total_video_duration > 0 else None,
    )

    current_index_state = committed_index_version or db.get_index_state()
    db.close()

    stop_requested = stop_event is not None and stop_event.is_set()

    force_export_to_qdrant = getattr(config, 'export_to_qdrant', False)
    try:
        qdrant_export_state = get_qdrant_export_version()
    except Exception as _e:
        logger.warning("Could not read Qdrant export state ({}); treating as unknown — will export conservatively.", _e)
        qdrant_export_state = None
    qdrant_stale = False
    local_index_version_seq = int(current_index_state.get("index_version_seq") or 0)
    if local_index_version_seq > 0:
        exported_seq = int(qdrant_export_state["index_version_seq"]) if qdrant_export_state else -1
        qdrant_stale = exported_seq < local_index_version_seq

    # Auto-export when local state changed this run, when Qdrant is behind local
    # SQLite state, or when explicitly forced. A graceful stop still finishes the
    # export so local index state and Qdrant stay in sync.
    should_export_to_qdrant = force_export_to_qdrant or local_index_changed or qdrant_stale
    if should_export_to_qdrant:
        if force_export_to_qdrant:
            logger.info("Forced Qdrant export requested, starting export...")
        elif qdrant_stale and not local_index_changed:
            logger.info(
                "Qdrant export state is stale (qdrant_seq={} local_seq={}), exporting to catch up...",
                qdrant_export_state["index_version_seq"] if qdrant_export_state else None,
                local_index_version_seq,
            )
        elif stop_requested:
            logger.info("Stop requested — finishing graceful shutdown with Qdrant export...")
        else:
            logger.info("Local index changes detected during indexing, exporting to Qdrant...")
        try:
            images_exported = _do_qdrant_export(config, face_recognizer, face_count, export_all=False)
        except Exception as _export_exc:
            logger.error("Qdrant export failed (local index is intact; retry with 'msa index export'): {}", _export_exc)
            images_exported = False
        if images_exported:
            record_qdrant_export_version(
                local_index_version_seq,
                current_index_state.get("index_version_ts"),
            )
            logger.info(
                "Recorded Qdrant export version seq={} ts={}",
                local_index_version_seq,
                current_index_state.get("index_version_ts"),
            )
        else:
            logger.info("Skipping version record — image/video export was skipped (reprocessing mode)")
    else:
        if qdrant_stale:
            logger.info("Skipping Qdrant export because local index has no committed version yet.")
        else:
            logger.info("Skipping Qdrant export (no index changes detected; use --export-to-qdrant or 'msa index export' to force)")


def _do_qdrant_export(config, face_recognizer=None, face_count=0, export_all=False):
    """Helper function to perform Qdrant export. Used by both run_index and run_export.
    
    Args:
        config: Configuration object
        face_recognizer: Face recognizer instance (if available)
        face_count: Number of faces detected (if available)
        export_all: If True, export everything regardless of reprocessing flags (used by run_export)
    """
    # Check if recreate flag is set (from export command)
    recreate = getattr(config, 'export_recreate', False)

    # Embeddings are sourced from SQLite tables (image_embedding,
    # keyframe_embedding, face_embedding). The export functions detect
    # an empty source and skip cleanly; we still gate on the
    # reprocessing flags so a metadata-only re-run doesn't reupload.
    if export_all:
        export_images = True
        export_faces = True
    else:
        export_images = not any([
            getattr(config, 'reprocess_gps', False),
            getattr(config, 'reprocess_objects', False),
            getattr(config, 'reprocess_faces', False),
            getattr(config, 'reprocess_embeddings', False),
        ])
        export_faces = export_images

    images_exported = False
    try:
        if export_images:
            logger.info("Exporting indexed items to Qdrant...")
            image_stats = export_images_to_qdrant(Path(config.sqlite_path), recreate=recreate)
            video_collection = getattr(config, 'col_video', None) or 'video_emb'
            video_stats = export_video_frames_to_qdrant(
                Path(config.sqlite_path), collection=video_collection, recreate=recreate
            )
            image_sent = int((image_stats or {}).get('sent', 0))
            video_sent = int((video_stats or {}).get('sent', 0))
            # Only signal "we exported the current state" when there was
            # actually content to export. A pre-Stage-3 upgrade (SQLite
            # metadata present, image_embedding empty) would otherwise
            # falsely advance the recorded export version and make later
            # runs believe Qdrant is current. See export gating below.
            if image_sent > 0 or video_sent > 0:
                logger.info(
                    f"Qdrant image/video export complete (image={image_sent} video={video_sent})"
                )
                images_exported = True
            else:
                logger.warning(
                    "Image/video export sent 0 points — SQLite has no image_embedding "
                    "or keyframe_embedding rows. Skipping export-version record so a "
                    "later run won't believe Qdrant is current. Run 'msa index run' "
                    "to populate the new tables (image and keyframe backfills are "
                    "automatic on the next normal indexer run)."
                )
        else:
            logger.info("Skipping image/video export to Qdrant (reprocessing mode)")

        # Export faces to Qdrant
        if export_faces:
            from .db.qdrant_export import export_faces_to_qdrant
            face_collection = getattr(config, 'col_face', None) or 'face_emb'
            # Force recreate when reprocessing to remove orphaned face vectors, or if export_recreate is set
            recreate_faces = getattr(config, 'reprocess_faces', False) or recreate
            try:
                if recreate_faces:
                    logger.info(f"Exporting {face_count} faces to Qdrant (recreating collection to remove orphaned vectors)...")
                else:
                    logger.info(f"Exporting {face_count} faces to Qdrant collection '{face_collection}'...")
                export_faces_to_qdrant(
                    Path(config.sqlite_path),
                    collection=face_collection,
                    recreate=recreate_faces,
                    embedding_backend=getattr(config, 'face_recognizer_backend', 'facenet_pytorch'),
                )
                logger.info("Qdrant face export complete")
            except Exception as e:
                logger.error(f"Face export to Qdrant failed: {e}")

    except Exception as e:
        logger.error(f"Qdrant export failed: {e}", exc_info=True)
        raise

    return images_exported


def run_export(config):
    """Export indexed data to Qdrant without running the indexer.

    Reads embeddings from the SQLite ``image_embedding`` /
    ``keyframe_embedding`` / ``face_embedding`` tables and upserts to
    Qdrant. The ``faiss_path`` config setting is no longer consulted —
    embeddings live in SQLite as of the FAISS-elimination work.

    On a pre-Stage-3 DB the new tables don't exist yet.
    ``init_schema_no_migrations`` creates them via ``IF NOT EXISTS``,
    after which the export helpers will see empty tables and bail out
    with a clear "no embeddings to export" log line. We deliberately
    avoid the full ``init_schema`` here because that variant runs the
    destructive pre-Stage-1 ``DROP TABLE face`` migration, which is
    only safe in the indexer's own opt-in re-process path.
    """
    logger.info("Starting Qdrant export (export-only mode)")

    sqlite_path = Path(config.sqlite_path)
    if not sqlite_path.exists():
        logger.error(f"SQLite database not found at {sqlite_path}. Run 'msa index run' first to index your media.")
        return

    # Ensure the Stage-3 embedding tables exist before any export query
    # touches them (handles the pre-Stage-3 upgrade case). Use the
    # no-migrations variant — export-only mode must not mutate face
    # data on legacy schemas.
    from .db.sqlite_store import LegacyFaceSchemaError
    schema_path = Path(__file__).parent / "db" / "schema.sql"
    init_db = SQLiteStore(sqlite_path, autocommit=True)
    legacy_face = False
    try:
        init_db.init_schema_no_migrations(schema_path)
    except LegacyFaceSchemaError as exc:
        logger.error(f"Cannot run export-only mode: {exc}")
        legacy_face = True
    finally:
        init_db.close()
    if legacy_face:
        return

    face_recognizer = None  # Not needed for export, but passed to helper
    face_count = 0
    try:
        with SQLiteStore(sqlite_path) as count_db:
            row = count_db.conn.execute("SELECT COUNT(1) FROM face_embedding").fetchone()
            face_count = int(row[0]) if row is not None else 0
        logger.info(f"Found {face_count} face embedding rows in SQLite")
    except Exception as e:
        logger.warning(f"Could not read face_embedding count: {e}")

    images_exported = _do_qdrant_export(config, face_recognizer, face_count, export_all=True)
    if images_exported:
        export_db = SQLiteStore(config.sqlite_path)
        try:
            latest_state = export_db.get_index_state()
        finally:
            export_db.close()
        record_qdrant_export_version(
            int(latest_state.get("index_version_seq") or 0),
            latest_state.get("index_version_ts"),
        )
        logger.info(
            "Recorded Qdrant export version seq={} ts={}",
            int(latest_state.get("index_version_seq") or 0),
            latest_state.get("index_version_ts"),
        )
    else:
        logger.warning("Image/video export did not complete — skipping version record to avoid stale state")
    
    logger.info("Qdrant export complete")

    # Summary: SQLite (canonical) vs Qdrant (search index)
    logger.info("")
    logger.info("Export Summary (SQLite → Qdrant):")

    try:
        from qdrant_client import QdrantClient
        from msa_settings import load_config

        S = load_config()
        sqlite_path = Path(config.sqlite_path)

        with SQLiteStore(sqlite_path) as sqlite_db:
            VIDEO_EXTENSIONS = ('.mp4', '.mov', '.avi', '.mkv', '.m4v', '.wmv', '.flv', '.webm')
            cur = sqlite_db.conn.execute(
                "SELECT media_id, path, source_name, rel_path FROM media WHERE deleted=0"
            )
            image_count_sqlite = 0
            for media_id, path, source_name, rel_path in cur:
                resolved_path = sqlite_db._resolve_abs_path(path, source_name, rel_path)
                if resolved_path and not resolved_path.lower().endswith(VIDEO_EXTENSIONS):
                    image_count_sqlite += 1

            video_keyframes_sqlite = sqlite_db.conn.execute(
                "SELECT COUNT(1) FROM video_keyframes"
            ).fetchone()[0]
            faces_sqlite = sqlite_db.conn.execute(
                "SELECT COUNT(1) FROM face"
            ).fetchone()[0]
            image_emb_sqlite = sqlite_db.conn.execute(
                "SELECT COUNT(1) FROM image_embedding"
            ).fetchone()[0]
            keyframe_emb_sqlite = sqlite_db.conn.execute(
                "SELECT COUNT(1) FROM keyframe_embedding"
            ).fetchone()[0]
            face_emb_sqlite = sqlite_db.conn.execute(
                "SELECT COUNT(1) FROM face_embedding"
            ).fetchone()[0]

        client = QdrantClient(path=str(S.qdrant_path))
        collections_to_check = [
            ("image_emb", getattr(S.collections, 'image', 'image_emb')),
            ("video_emb", getattr(S.collections, 'video', 'video_emb')),
            ("face_emb", getattr(S.collections, 'face', 'face_emb')),
        ]
        qdrant_counts = {}
        for display_name, collection_name in collections_to_check:
            try:
                info = client.get_collection(collection_name)
                points_count = getattr(info, 'points_count', 0)
                vector_size = getattr(info.vectors_config.params, 'size', 'unknown') if hasattr(info, 'vectors_config') else 'unknown'
                qdrant_counts[display_name] = (points_count, vector_size)
            except Exception as e:
                error_msg = str(e).lower()
                if "not found" in error_msg or "404" in error_msg or "does not exist" in error_msg:
                    qdrant_counts[display_name] = (0, 'unknown')
                else:
                    logger.debug(f"Error querying {display_name}: {e}")
                    qdrant_counts[display_name] = (0, 'unknown')

        logger.info("")
        logger.info("  Images:")
        logger.info(f"    SQLite media:           {image_count_sqlite:,} rows")
        logger.info(f"    SQLite image_embedding: {image_emb_sqlite:,} rows")
        logger.info(f"    Qdrant {qdrant_counts['image_emb'][1]}d:           {qdrant_counts['image_emb'][0]:,} points")

        logger.info("")
        logger.info("  Video Keyframes:")
        logger.info(f"    SQLite video_keyframes:    {video_keyframes_sqlite:,} rows")
        logger.info(f"    SQLite keyframe_embedding: {keyframe_emb_sqlite:,} rows")
        logger.info(f"    Qdrant {qdrant_counts['video_emb'][1]}d:              {qdrant_counts['video_emb'][0]:,} points")

        logger.info("")
        logger.info("  Faces:")
        logger.info(f"    SQLite face:           {faces_sqlite:,} rows")
        logger.info(f"    SQLite face_embedding: {face_emb_sqlite:,} rows")
        logger.info(f"    Qdrant {qdrant_counts['face_emb'][1]}d:          {qdrant_counts['face_emb'][0]:,} points")

    except Exception as e:
        logger.warning(f"Could not generate export summary: {e}")
        logger.debug("", exc_info=True)


def run_dry_run(config):
    """Dry-run mode: scan files and show statistics without processing.
    
    This mode does not:
    - Load ML models
    - Write to database
    - Process any files
    - Generate embeddings, detect objects/faces, etc.
    
    It only:
    - Scans media sources
    - Collects file statistics
    - Estimates processing time based on average performance
    """
    import time
    
    logger.info("Starting dry-run scan (no processing, no ML models)")
    
    # Log media type filtering if enabled
    if getattr(config, 'image_only', False):
        logger.info("Mode: image_only (videos will be skipped)")
    elif getattr(config, 'video_only', False):
        logger.info("Mode: video_only (images will be skipped)")
    
    # Collect media sources (same logic as run_index)
    sources_to_index = []
    if getattr(config, 'media_source_override', None):
        from dataclasses import dataclass
        @dataclass
        class OverrideSource:
            name: str
            path: str
            enabled: bool = True
        sources_to_index = [OverrideSource(name="cli-override", path=str(config.media_source_override))]
        logger.info(f"Using CLI media_source_override: {config.media_source_override}")
    elif hasattr(config, 'media_sources') and config.media_sources:
        sources_to_index = [s for s in config.media_sources if getattr(s, 'enabled', True)]
        if not sources_to_index:
            logger.error("No enabled media_sources found in config; aborting.")
            return
        logger.info(f"Found {len(sources_to_index)} enabled media sources: {[s.name for s in sources_to_index]}")
    else:
        logger.error("No media_sources defined and no --media-source-override provided; aborting.")
        return
    
    # Validate source paths
    valid_sources = []
    for s in sources_to_index:
        try:
            rp = Path(resolve_for_access(str(s.path)))
            if rp.exists() and rp.is_dir():
                valid_sources.append(s)
            else:
                logger.warning(f"Source path does not exist or is not a directory, skipping: {s.path}")
        except Exception as e:
            logger.warning(f"Invalid source path '{getattr(s, 'path', None)}': {e}")
    if not valid_sources:
        logger.error("No valid source paths to scan; aborting.")
        return
    sources_to_index = valid_sources
    
    # Size thresholds (in bytes)
    # Small: < 1MB, Medium: 1MB-10MB, Large: 10MB-100MB, Extra Large: >= 100MB
    IMG_SIZE_SMALL = 1024 * 1024  # 1MB
    IMG_SIZE_MEDIUM = 10 * 1024 * 1024  # 10MB
    IMG_SIZE_LARGE = 100 * 1024 * 1024  # 100MB
    
    VID_SIZE_SMALL = 10 * 1024 * 1024  # 10MB
    VID_SIZE_MEDIUM = 100 * 1024 * 1024  # 100MB
    VID_SIZE_LARGE = 1024 * 1024 * 1024  # 1GB
    
    # Duration thresholds (in seconds)
    # Small: < 1min, Medium: 1-5min, Large: 5-30min, Extra Large: >= 30min
    VID_DUR_SMALL = 60  # 1 minute
    VID_DUR_MEDIUM = 300  # 5 minutes
    VID_DUR_LARGE = 1800  # 30 minutes
    
    # Statistics
    total_files = 0
    total_images = 0
    total_videos = 0
    
    img_small = img_medium = img_large = img_xl = 0
    vid_small_size = vid_medium_size = vid_large_size = vid_xl_size = 0
    vid_small_dur = vid_medium_dur = vid_large_dur = vid_xl_dur = 0
    
    total_image_size = 0
    total_video_size = 0
    total_video_duration = 0.0
    
    # Average performance metrics (from typical runs - can be made configurable)
    # These are defaults; in a real implementation, you might load from previous run stats
    avg_img_time = 1.0  # seconds per image
    avg_vid_time = 30.0  # seconds per video
    avg_vid_time_per_min = 5.0  # seconds per minute of video
    
    # Determine media type filter based on config flags
    media_type_filter = None
    if getattr(config, 'image_only', False):
        media_type_filter = "image"
    elif getattr(config, 'video_only', False):
        media_type_filter = "video"
    
    scan_start = time.perf_counter()
    
    # Scan all sources
    for source in sources_to_index:
        root = Path(source.path)
        source_name = source.name
        logger.info(f"📂 Scanning source: {source_name} | path: {root}")
        
        for p in iter_media(root, media_type=media_type_filter):
            ext = p.suffix.lower()
            
            total_files += 1
            
            try:
                file_size = p.stat().st_size
                
                if ext in IMAGE_EXT:
                    total_images += 1
                    total_image_size += file_size
                    
                    # Categorize by size
                    if file_size < IMG_SIZE_SMALL:
                        img_small += 1
                    elif file_size < IMG_SIZE_MEDIUM:
                        img_medium += 1
                    elif file_size < IMG_SIZE_LARGE:
                        img_large += 1
                    else:
                        img_xl += 1
                
                elif ext in VIDEO_EXT:
                    total_videos += 1
                    total_video_size += file_size
                    
                    # Categorize by size
                    if file_size < VID_SIZE_SMALL:
                        vid_small_size += 1
                    elif file_size < VID_SIZE_MEDIUM:
                        vid_medium_size += 1
                    elif file_size < VID_SIZE_LARGE:
                        vid_large_size += 1
                    else:
                        vid_xl_size += 1
                    
                    # Try to get duration (lightweight - just metadata read)
                    duration = None
                    try:
                        meta = get_video_meta(p)
                        duration = meta.get("duration")
                        if duration:
                            total_video_duration += duration
                            
                            # Categorize by duration
                            if duration < VID_DUR_SMALL:
                                vid_small_dur += 1
                            elif duration < VID_DUR_MEDIUM:
                                vid_medium_dur += 1
                            elif duration < VID_DUR_LARGE:
                                vid_large_dur += 1
                            else:
                                vid_xl_dur += 1
                    except Exception:
                        # If we can't read duration, skip duration categorization
                        pass
                        
            except Exception as e:
                logger.warning(f"Error scanning file {p}: {e}")
                continue
    
    scan_elapsed = time.perf_counter() - scan_start
    
    # Calculate expected processing time
    expected_img_time = total_images * avg_img_time
    expected_vid_time = total_videos * avg_vid_time
    if total_video_duration > 0:
        expected_vid_time_by_duration = (total_video_duration / 60) * avg_vid_time_per_min
        # Use the higher estimate (per file or per duration)
        expected_vid_time = max(expected_vid_time, expected_vid_time_by_duration)
    expected_total_time = expected_img_time + expected_vid_time
    
    # Format expected time
    if expected_total_time >= 3600:
        expected_time_str = f"{expected_total_time/3600:.2f}h"
    elif expected_total_time >= 60:
        expected_time_str = f"{expected_total_time/60:.2f}m"
    else:
        expected_time_str = f"{expected_total_time:.2f}s"
    
    # Format total sizes
    def format_size(size_bytes):
        if size_bytes >= 1024 * 1024 * 1024:
            return f"{size_bytes / (1024 * 1024 * 1024):.2f}GB"
        elif size_bytes >= 1024 * 1024:
            return f"{size_bytes / (1024 * 1024):.2f}MB"
        elif size_bytes >= 1024:
            return f"{size_bytes / 1024:.2f}KB"
        else:
            return f"{size_bytes}B"
    
    def format_duration(seconds):
        if seconds >= 3600:
            return f"{seconds/3600:.2f}h"
        elif seconds >= 60:
            return f"{seconds/60:.2f}m"
        else:
            return f"{seconds:.1f}s"
    
    # Print statistics
    logger.info("=" * 80)
    logger.info("DRY-RUN SCAN RESULTS")
    logger.info("=" * 80)
    
    # Count stats
    logger.info(f"Count stats: files={total_files} images={total_images} videos={total_videos}")
    
    # Image file stats by size
    logger.info("Image file stats (by size):")
    logger.info(f"  Small (<1MB): {img_small}")
    logger.info(f"  Medium (1-10MB): {img_medium}")
    logger.info(f"  Large (10-100MB): {img_large}")
    logger.info(f"  Extra Large (>=100MB): {img_xl}")
    if total_images > 0:
        avg_img_size = total_image_size / total_images
        logger.info(f"  Total size: {format_size(total_image_size)} | Avg size: {format_size(int(avg_img_size))}")
    
    # Video file stats by size
    logger.info("Video file stats (by size):")
    logger.info(f"  Small (<10MB): {vid_small_size}")
    logger.info(f"  Medium (10-100MB): {vid_medium_size}")
    logger.info(f"  Large (100MB-1GB): {vid_large_size}")
    logger.info(f"  Extra Large (>=1GB): {vid_xl_size}")
    if total_videos > 0:
        avg_vid_size = total_video_size / total_videos
        logger.info(f"  Total size: {format_size(total_video_size)} | Avg size: {format_size(int(avg_vid_size))}")
    
    # Video file stats by duration
    logger.info("Video file stats (by duration):")
    logger.info(f"  Small (<1min): {vid_small_dur}")
    logger.info(f"  Medium (1-5min): {vid_medium_dur}")
    logger.info(f"  Large (5-30min): {vid_large_dur}")
    logger.info(f"  Extra Large (>=30min): {vid_xl_dur}")
    if total_video_duration > 0:
        avg_vid_dur = total_video_duration / total_videos
        logger.info(f"  Total duration: {format_duration(total_video_duration)} | Avg duration: {format_duration(avg_vid_dur)}")
    
    # Expected performance
    logger.info("Expected performance (based on average processing times):")
    logger.info(f"  Expected image processing time: {format_duration(expected_img_time)}")
    logger.info(f"  Expected video processing time: {format_duration(expected_vid_time)}")
    logger.info(f"  Expected total processing time: {expected_time_str}")
    logger.info(f"  Scan time: {scan_elapsed:.2f}s")


def run_export_dry_run(config):
    """Dry-run mode for export: analyze what would be exported and identify missing embeddings.

    This mode does not:
    - Connect to Qdrant
    - Export any data
    - Modify any collections
    - **Modify the SQLite schema in any way** — the analysis bails out
      with a clear message rather than running migrations on a pre-
      Stage-3 DB. Run ``msa index run`` (which does invoke
      ``init_schema``) before re-running the dry-run.

    It only:
    - Checks SQLite for what should be exported
    - Reports missing embeddings (via JOINs)
    - Reports counts and statistics
    """
    from pathlib import Path
    from .db.sqlite_store import SQLiteStore

    logger.info("Starting export dry-run (no Qdrant connection, no exports)")
    logger.info("")

    sqlite_path = Path(config.sqlite_path)
    if not sqlite_path.exists():
        logger.error(f"SQLite database not found at {sqlite_path}. Run 'msa index run' first to index your media.")
        return

    meta = SQLiteStore(sqlite_path)
    # Probe whether the Stage-3 embedding tables exist without running
    # init_schema (which is *not* read-only — it includes the legacy
    # face-table drop/recreate migration). On a pre-Stage-3 DB we bail
    # out early with a clear message rather than mutate the schema in
    # what's documented as a non-mutating mode.
    missing_tables = []
    for tbl in ("image_embedding", "keyframe_embedding", "face_embedding"):
        row = meta.conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
            (tbl,),
        ).fetchone()
        if row is None:
            missing_tables.append(tbl)
    if missing_tables:
        meta.close()
        logger.error(
            "Cannot run export dry-run: SQLite is missing embedding tables "
            f"{missing_tables}. This database hasn't been touched by the Stage-3 "
            "migration yet. Run 'msa index run' once first (which creates the "
            "tables and backfills embeddings on existing media) before retrying "
            "the dry-run."
        )
        return

    try:
        logger.info("Analyzing export readiness...")
        logger.info("")

        # ========== IMAGES ==========
        logger.info("=" * 80)
        logger.info("IMAGE EXPORT ANALYSIS")
        logger.info("=" * 80)

        sqlite_image_count = meta.conn.execute(
            "SELECT COUNT(*) FROM media WHERE deleted=0 AND mime LIKE 'image/%'"
        ).fetchone()[0]
        logger.info(f"SQLite media (images): {sqlite_image_count:,} records")

        # Images with embeddings: JOIN through image_embedding
        images_with_vectors = meta.conn.execute(
            """
            SELECT COUNT(*) FROM image_embedding ie
            JOIN media m ON m.media_id = ie.media_id
            WHERE m.deleted = 0 AND m.mime LIKE 'image/%'
            """
        ).fetchone()[0]
        missing_images_count = sqlite_image_count - images_with_vectors

        sample_missing = [
            r[0] for r in meta.conn.execute(
                """
                SELECT m.media_id FROM media m
                LEFT JOIN image_embedding ie ON ie.media_id = m.media_id
                WHERE m.deleted = 0 AND m.mime LIKE 'image/%' AND ie.media_id IS NULL
                LIMIT 10
                """
            ).fetchall()
        ]

        logger.info(f"  ✓ Images with embeddings: {images_with_vectors:,}")
        logger.info(f"  ✗ Images missing embeddings: {missing_images_count:,}")
        if sample_missing:
            logger.warning(f"  Sample missing IDs (first 10): {sample_missing}")

        # ========== VIDEO KEYFRAMES ==========
        logger.info("")
        logger.info("=" * 80)
        logger.info("VIDEO KEYFRAME EXPORT ANALYSIS")
        logger.info("=" * 80)

        sqlite_video_count = meta.conn.execute(
            "SELECT COUNT(*) FROM video_keyframes"
        ).fetchone()[0]
        logger.info(f"SQLite video_keyframes: {sqlite_video_count:,} records")

        keyframes_with_vectors = meta.conn.execute(
            """
            SELECT COUNT(*) FROM keyframe_embedding ke
            JOIN video_keyframes vk ON vk.id = ke.keyframe_id
            """
        ).fetchone()[0]
        missing_keyframes_count = sqlite_video_count - keyframes_with_vectors

        sample_missing_video = [
            (r[0], r[1], r[2]) for r in meta.conn.execute(
                """
                SELECT vk.video_id, vk.shot_index, vk.kf_index FROM video_keyframes vk
                LEFT JOIN keyframe_embedding ke ON ke.keyframe_id = vk.id
                WHERE ke.keyframe_id IS NULL
                LIMIT 10
                """
            ).fetchall()
        ]

        logger.info(f"  ✓ Video keyframes with embeddings: {keyframes_with_vectors:,}")
        logger.info(f"  ✗ Video keyframes missing embeddings: {missing_keyframes_count:,}")
        if sample_missing_video:
            logger.warning(f"  Sample missing (first 10):")
            for vid, s_idx, k_idx in sample_missing_video:
                logger.warning(f"    - {vid[:16]}... s{s_idx} k{k_idx}")

        # ========== FACES ==========
        logger.info("")
        logger.info("=" * 80)
        logger.info("FACE EXPORT ANALYSIS")
        logger.info("=" * 80)

        sqlite_face_count = meta.conn.execute(
            "SELECT COUNT(*) FROM face"
        ).fetchone()[0]
        logger.info(f"SQLite face: {sqlite_face_count:,} records")

        faces_with_vectors = meta.conn.execute(
            """
            SELECT COUNT(*) FROM face_embedding fe
            JOIN face f ON f.face_id = fe.face_id
            """
        ).fetchone()[0]
        missing_faces_count = sqlite_face_count - faces_with_vectors

        sample_missing_faces = [
            r[0] for r in meta.conn.execute(
                """
                SELECT f.face_id FROM face f
                LEFT JOIN face_embedding fe ON fe.face_id = f.face_id
                WHERE fe.face_id IS NULL
                LIMIT 10
                """
            ).fetchall()
        ]

        logger.info(f"  ✓ Faces with embeddings: {faces_with_vectors:,}")
        logger.info(f"  ✗ Faces missing embeddings: {missing_faces_count:,}")
        if sample_missing_faces:
            logger.warning(f"  Sample missing IDs (first 10): {sample_missing_faces}")

        # ========== SUMMARY ==========
        logger.info("")
        logger.info("=" * 80)
        logger.info("EXPORT DRY-RUN SUMMARY")
        logger.info("=" * 80)
        logger.info("")
        logger.info("What would be exported:")
        logger.info(f"  Images: {images_with_vectors:,} (would skip {missing_images_count:,} missing embeddings)")
        logger.info(f"  Video Keyframes: {keyframes_with_vectors:,} (would skip {missing_keyframes_count:,} missing embeddings)")
        logger.info(f"  Faces: {faces_with_vectors:,} (would skip {missing_faces_count:,} missing embeddings)")
        logger.info("")

        total_missing = missing_images_count + missing_keyframes_count + missing_faces_count
        if total_missing > 0:
            logger.warning(f"⚠️  Total missing embeddings: {total_missing:,}")
            logger.warning("  These items will be skipped during export.")
            logger.warning("  Consider re-indexing to generate missing embeddings.")
        else:
            logger.info("✓ All embeddings present - export would succeed for all items")

        logger.info("")
        logger.info("Note: This is a dry-run. No data was exported to Qdrant.")
        logger.info("Run 'msa index export' to perform the actual export.")
    finally:
        meta.close()
