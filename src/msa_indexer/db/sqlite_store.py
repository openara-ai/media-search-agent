from pathlib import Path
import sqlite3
from typing import Iterable, Any, Dict, Optional, List
import uuid
from datetime import datetime, timezone

# For resolving absolute paths from (source_name, rel_path)
try:
    from msa_settings.config import load_config as load_global_config
except Exception:
    load_global_config = None  # Fallback if settings are unavailable at import time


class LegacyFaceSchemaError(RuntimeError):
    """Raised when a pre-Stage-1 face table is detected in a context
    that's not allowed to perform the destructive face-table migration
    (export-only, porter). The user must run ``msa index run`` first.
    """


class SQLiteStore:
    def __init__(self, path: Path, autocommit: bool = True):
        self.path = path
        self.conn = sqlite3.connect(str(path))
        self.conn.execute("PRAGMA foreign_keys=ON")
        # synchronous=NORMAL is the appropriate companion to journal_mode=WAL
        # for a single-host workload — it keeps fsync only at WAL checkpoints
        # rather than every COMMIT, which is what makes per-batch commits
        # cheap. The power-loss risk is bounded to "lose the last batch"
        # which the indexer's resume logic handles cleanly.
        # journal_mode=WAL is already persistent in the DB header (set by
        # schema.sql) so no need to reassert it here.
        self.conn.execute("PRAGMA synchronous=NORMAL")
        self.autocommit = autocommit
        # Lazy-initialized map: source_name -> base Path from config.yaml
        self._source_map: Optional[dict[str, Path]] = None

    def _maybe_commit(self):
        if self.autocommit:
            self.commit()

    # ---------------- Internal helpers: source/path resolution ----------------
    def _load_source_map(self) -> dict[str, Path]:
        """Load mapping of source_name -> base path from global config.yaml (cached)."""
        if self._source_map is not None:
            return self._source_map
        mapping: dict[str, Path] = {}
        try:
            if load_global_config is not None:
                cfg = load_global_config(None)
                if getattr(cfg, "media_sources", None):
                    for s in cfg.media_sources:
                        try:
                            if getattr(s, "enabled", True) and getattr(s, "path", None):
                                mapping[s.name] = Path(s.path)
                        except Exception:
                            continue
        except Exception:
            # If config fails to load, keep empty mapping and fall back to stored absolute path
            mapping = {}
        self._source_map = mapping
        return mapping

    def _resolve_abs_path(self, db_path: Optional[str], source_name: Optional[str], rel_path: Optional[str]) -> Optional[str]:
        """Reconstruct absolute path from (source_name, rel_path) if possible.

        Falls back to stored db_path when mapping is unavailable or incomplete.
        """
        if source_name and rel_path:
            base = self._load_source_map().get(source_name)
            if base is not None:
                try:
                    return str((base / rel_path).resolve())
                except Exception:
                    # In case of any resolution issues, fall back to db_path
                    pass
        return db_path

    def init_schema(self, schema_path: Path):
        """Full schema init including the destructive legacy face-table migration.

        The legacy migration drops the ``face`` table if a pre-Stage-1
        schema (no ``person_id`` column) is detected. That's destructive
        and must only run from contexts where the user has opted into a
        full re-process — i.e. ``msa index run``. Other entry points
        (export-only mode, porter, dry-runs) must call
        :meth:`init_schema_no_migrations` instead.
        """
        self._maybe_drop_legacy_face_table()
        self._apply_schema_and_additive_migrations(schema_path)

    def init_schema_no_migrations(self, schema_path: Path):
        """Idempotent schema init without the destructive face-table drop.

        Runs the full ``schema.sql`` (every CREATE uses ``IF NOT EXISTS``,
        so it never modifies an existing table) plus the additive
        ALTER TABLE ADD COLUMN migrations (each gated on whether the
        column already exists, so they're idempotent and never destructive).

        Skips the pre-Stage-1 ``DROP TABLE face`` migration that
        :meth:`init_schema` runs. Safe to call from non-indexer entry
        points (``run_export``, the FAISS→SQLite porter) where surprise
        face-data loss would be unacceptable.

        Raises:
            LegacyFaceSchemaError: if a pre-Stage-1 face table (no
                ``person_id`` column) is detected. We refuse to either
                silently drop it or create indexes that depend on the
                missing column. The caller must surface a clear message
                and direct the user to ``msa index run`` (the only path
                that's allowed to perform the destructive migration).
        """
        self._fail_on_legacy_face_schema()
        self._apply_schema_and_additive_migrations(schema_path)

    def _fail_on_legacy_face_schema(self):
        """Raise if the ``face`` table exists in a pre-Stage-1 shape."""
        cursor = self.conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='face'"
        )
        if cursor.fetchone() is None:
            return  # no face table — fresh DB, fine
        cols = [c[1] for c in self.conn.execute("PRAGMA table_info(face)").fetchall()]
        if "person_id" not in cols:
            raise LegacyFaceSchemaError(
                "Legacy face table detected (missing person_id column). "
                "This database hasn't been migrated yet. Run 'msa index run' "
                "first — that path applies the destructive Stage-1 face-table "
                "migration via the user's opt-in re-process flow. Back up the "
                "database before doing so if face data is irreplaceable."
            )

    def _maybe_drop_legacy_face_table(self):
        """Pre-Stage-1 face-table migration. Destructive — drops the
        legacy ``face`` table if its schema lacks ``person_id``.
        """
        try:
            cursor = self.conn.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name='face'")
            row = cursor.fetchone()
            if row:
                cursor = self.conn.execute("PRAGMA table_info(face)")
                columns = [col[1] for col in cursor.fetchall()]
                if 'person_id' not in columns:
                    print("⚠️  Detected old face table schema - dropping and recreating with new columns...")
                    self.conn.execute("DROP TABLE IF EXISTS face")
                    self.conn.commit()
        except Exception:
            # If any error checking, just continue with schema creation
            pass

    def _apply_schema_and_additive_migrations(self, schema_path: Path):
        """Run schema.sql + the safe ALTER TABLE ADD COLUMN migrations."""
        with open(schema_path, "r") as f:
            schema_sql = f.read()
        self.conn.executescript(schema_sql)
        self.conn.execute(
            """
            INSERT OR IGNORE INTO index_state(
                singleton_id,
                index_version_seq,
                index_version_ts
            ) VALUES (1, 0, NULL)
            """
        )
        self.conn.commit()

        # Migration: Add face_detection_done column to media table if missing
        try:
            cursor = self.conn.execute("PRAGMA table_info(media)")
            columns = [col[1] for col in cursor.fetchall()]
            if 'face_detection_done' not in columns:
                print("⚠️  Adding face_detection_done column to media table...")
                self.conn.execute("ALTER TABLE media ADD COLUMN face_detection_done INTEGER DEFAULT 0")
                self.conn.commit()
        except Exception as e:
            # If any error, continue
            pass
        
        # Migration: Add gps_processed column if missing
        try:
            cursor = self.conn.execute("PRAGMA table_info(media)")
            columns = [col[1] for col in cursor.fetchall()]
            if 'gps_processed' not in columns:
                print("⚠️  Adding gps_processed column to media table...")
                self.conn.execute("ALTER TABLE media ADD COLUMN gps_processed INTEGER DEFAULT 0")
                self.conn.commit()
        except Exception as e:
            pass
        
        # Migration: Add embeddings_version column if missing
        try:
            cursor = self.conn.execute("PRAGMA table_info(media)")
            columns = [col[1] for col in cursor.fetchall()]
            if 'embeddings_version' not in columns:
                print("⚠️  Adding embeddings_version column to media table...")
                self.conn.execute("ALTER TABLE media ADD COLUMN embeddings_version TEXT")
                self.conn.commit()
        except Exception as e:
            pass
        
        # Migration: Add object_detection_done column if missing
        try:
            cursor = self.conn.execute("PRAGMA table_info(media)")
            columns = [col[1] for col in cursor.fetchall()]
            if 'object_detection_done' not in columns:
                print("⚠️  Adding object_detection_done column to media table...")
                self.conn.execute("ALTER TABLE media ADD COLUMN object_detection_done INTEGER DEFAULT 0")
                self.conn.commit()
        except Exception as e:
            pass

        # Migration: Add source_name and rel_path columns for multi-source relative paths
        try:
            cursor = self.conn.execute("PRAGMA table_info(media)")
            columns = [col[1] for col in cursor.fetchall()]
            added = False
            if 'source_name' not in columns:
                print("⚠️  Adding source_name column to media table...")
                self.conn.execute("ALTER TABLE media ADD COLUMN source_name TEXT")
                added = True
            if 'rel_path' not in columns:
                print("⚠️  Adding rel_path column to media table...")
                self.conn.execute("ALTER TABLE media ADD COLUMN rel_path TEXT")
                added = True
            if added:
                self.conn.commit()
                # Create index if not exists
                self.conn.execute("CREATE INDEX IF NOT EXISTS idx_media_source_rel ON media(source_name, rel_path)")
                self.conn.commit()
        except Exception:
            pass

    def upsert_media(self, row: Dict[str, Any]):
        cols = ",".join(row.keys())
        placeholders = ",".join([":" + k for k in row.keys()])
        sql = f"INSERT INTO media ({cols}) VALUES ({placeholders}) " \
              f"ON CONFLICT(media_id) DO UPDATE SET " + ",".join([f"{k}=excluded.{k}" for k in row.keys() if k!="media_id"])
        self.conn.execute(sql, row)
        self._maybe_commit()

    def update_media_fields(self, media_id: str, fields: Dict[str, Any]):
        if not fields:
            return
        assignments = ",".join([f"{key}=?" for key in fields.keys()])
        params = list(fields.values()) + [media_id]
        self.conn.execute(f"UPDATE media SET {assignments} WHERE media_id = ?", params)
        self._maybe_commit()

    def add_tags(self, media_id: str, tags: Iterable[str]):
        for t in tags:
            self.conn.execute("INSERT OR IGNORE INTO tag(name) VALUES (?)", (t,))
            tag_id = self.conn.execute("SELECT tag_id FROM tag WHERE name=?", (t,)).fetchone()[0]
            self.conn.execute("INSERT OR IGNORE INTO media_tag(media_id, tag_id) VALUES (?,?)", (media_id, tag_id))
        self._maybe_commit()

    # ── Embedding storage (Stage 3 of SQLITE_INCREMENTAL_VISIBILITY_PLAN) ──
    #
    # Embeddings are stored as raw float32 BLOBs in three sibling tables
    # joined by primary key to media / face / video_keyframes. Keeping
    # them out of the parent rows means browse paths can never accidentally
    # pull a 3 KB BLOB into a result set, and dropping any embedding
    # table forces a re-embed without disturbing labels or metadata.

    def upsert_image_embedding(self, media_id: str, embedding, model: str) -> None:
        """Insert or replace the CLIP embedding for an image-type media row."""
        blob, dim = self._serialize_embedding(embedding)
        self.conn.execute(
            """
            INSERT INTO image_embedding(media_id, embedding, embedding_dim, embedding_model)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(media_id) DO UPDATE SET
              embedding=excluded.embedding,
              embedding_dim=excluded.embedding_dim,
              embedding_model=excluded.embedding_model
            """,
            (media_id, blob, dim, model),
        )
        self._maybe_commit()

    def upsert_keyframe_embedding(self, keyframe_id: int, embedding, model: str) -> None:
        """Insert or replace the CLIP embedding for a video keyframe row."""
        blob, dim = self._serialize_embedding(embedding)
        self.conn.execute(
            """
            INSERT INTO keyframe_embedding(keyframe_id, embedding, embedding_dim, embedding_model)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(keyframe_id) DO UPDATE SET
              embedding=excluded.embedding,
              embedding_dim=excluded.embedding_dim,
              embedding_model=excluded.embedding_model
            """,
            (int(keyframe_id), blob, dim, model),
        )
        self._maybe_commit()

    def upsert_face_embedding(self, face_id: str, embedding, model: str) -> None:
        """Insert or replace the face embedding for a detection row."""
        blob, dim = self._serialize_embedding(embedding)
        self.conn.execute(
            """
            INSERT INTO face_embedding(face_id, embedding, embedding_dim, embedding_model)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(face_id) DO UPDATE SET
              embedding=excluded.embedding,
              embedding_dim=excluded.embedding_dim,
              embedding_model=excluded.embedding_model
            """,
            (face_id, blob, dim, model),
        )
        self._maybe_commit()

    def get_face_embedding(self, face_id: str) -> Optional[bytes]:
        """Return the raw float32 BLOB for a face_id, or None if missing."""
        row = self.conn.execute(
            "SELECT embedding FROM face_embedding WHERE face_id = ?",
            (face_id,),
        ).fetchone()
        return row[0] if row is not None else None

    def get_image_embedding(self, media_id: str) -> Optional[bytes]:
        """Return the raw float32 BLOB for an image media_id, or None if missing."""
        row = self.conn.execute(
            "SELECT embedding FROM image_embedding WHERE media_id = ?",
            (media_id,),
        ).fetchone()
        return row[0] if row is not None else None

    def get_keyframe_embedding(self, keyframe_id: int) -> Optional[bytes]:
        """Return the raw float32 BLOB for a keyframe row id, or None if missing."""
        row = self.conn.execute(
            "SELECT embedding FROM keyframe_embedding WHERE keyframe_id = ?",
            (int(keyframe_id),),
        ).fetchone()
        return row[0] if row is not None else None

    # ── Embedding presence checks (Stage 3 upgrade-path support) ──────────
    #
    # These let the indexer detect a pre-Stage-3 DB where metadata is fully
    # populated (embeddings_version, face_detection_done) but the new
    # *_embedding tables are still empty, and trigger a backfill rather
    # than silently fast-pathing those files.

    def has_image_embedding(self, media_id: str) -> bool:
        row = self.conn.execute(
            "SELECT 1 FROM image_embedding WHERE media_id = ? LIMIT 1",
            (media_id,),
        ).fetchone()
        return row is not None

    def media_has_unembedded_keyframes(self, video_id: str) -> bool:
        """True if at least one video_keyframes row for this media lacks
        a corresponding keyframe_embedding row.
        """
        row = self.conn.execute(
            """
            SELECT 1 FROM video_keyframes vk
            LEFT JOIN keyframe_embedding ke ON ke.keyframe_id = vk.id
            WHERE vk.video_id = ? AND ke.keyframe_id IS NULL
            LIMIT 1
            """,
            (video_id,),
        ).fetchone()
        return row is not None

    def media_has_unembedded_faces(self, media_id: str) -> bool:
        """True if at least one face row for this media lacks a
        corresponding face_embedding row.
        """
        row = self.conn.execute(
            """
            SELECT 1 FROM face f
            LEFT JOIN face_embedding fe ON fe.face_id = f.face_id
            WHERE f.media_id = ? AND fe.face_id IS NULL
            LIMIT 1
            """,
            (media_id,),
        ).fetchone()
        return row is not None

    def count_orphan_face_embeddings(self) -> int:
        """Number of face rows lacking a face_embedding row across the DB.

        Used at indexer-run startup to surface a pre-Stage-3 upgrade
        situation: face metadata exists but the new face_embedding table
        is empty. Re-running face detection would risk overwriting
        manual labels (face.person_id), so we report rather than fix.
        """
        row = self.conn.execute(
            """
            SELECT COUNT(*) FROM face f
            LEFT JOIN face_embedding fe ON fe.face_id = f.face_id
            WHERE fe.face_id IS NULL
            """
        ).fetchone()
        return int(row[0]) if row is not None else 0

    def get_keyframe_id(
        self, video_id: str, shot_index: int, kf_index: int
    ) -> Optional[int]:
        """Look up the auto-id of a video_keyframes row by its natural key.

        Used after ``add_keyframes`` to associate freshly-inserted keyframe
        rows with their per-keyframe embeddings, since the natural key
        ``(video_id, shot_index, kf_index)`` is what the pipeline knows
        but ``keyframe_embedding.keyframe_id`` is the surrogate auto-id.
        """
        row = self.conn.execute(
            """
            SELECT id FROM video_keyframes
            WHERE video_id = ? AND shot_index = ? AND kf_index = ?
            """,
            (video_id, int(shot_index), int(kf_index)),
        ).fetchone()
        return int(row[0]) if row is not None else None

    @staticmethod
    def _serialize_embedding(vec) -> tuple[bytes, int]:
        """Convert a vector (numpy array, list, tuple) to a fixed-shape
        float32 BLOB plus its dimensionality.

        Stored as raw bytes (no per-row format header) — the
        ``embedding_dim`` and ``embedding_model`` columns carry the
        shape and provenance metadata.
        """
        import numpy as np
        arr = np.asarray(vec, dtype=np.float32).reshape(-1)
        return arr.tobytes(), int(arr.shape[0])

    def commit(self): self.conn.commit()

    def rollback(self): self.conn.rollback()

    def close(self): self.conn.close()

    def __enter__(self): return self

    def __exit__(self, *_): self.close()

    def media_exists(self, media_id: str) -> bool:
        """Check if media item already exists in database."""
        result = self.conn.execute("SELECT 1 FROM media WHERE media_id = ? LIMIT 1", (media_id,)).fetchone()
        return result is not None
    
    def get_processing_status(self, media_id: str) -> dict:
        """Get processing status flags for a media item.
        
        Returns dict with keys: gps_processed, object_detection_done, face_detection_done, embeddings_version
        Returns empty dict if media not found.
        """
        result = self.conn.execute(
            "SELECT gps_processed, object_detection_done, face_detection_done, embeddings_version FROM media WHERE media_id = ?",
            (media_id,)
        ).fetchone()
        if not result:
            return {}
        return {
            "gps_processed": bool(result[0]),
            "object_detection_done": bool(result[1]),
            "face_detection_done": bool(result[2]),
            "embeddings_version": result[3],
        }
    
    def mark_gps_processed(self, media_id: str):
        """Mark that GPS metadata has been extracted for this media."""
        self.conn.execute("UPDATE media SET gps_processed = 1 WHERE media_id = ?", (media_id,))
        self._maybe_commit()
    
    def mark_embeddings_done(self, media_id: str, version: str):
        """Mark that embeddings have been computed for this media with given model version."""
        self.conn.execute("UPDATE media SET embeddings_version = ? WHERE media_id = ?", (version, media_id))
        self._maybe_commit()
    
    def face_detection_done(self, media_id: str) -> bool:
        """Check if face detection has been run for this media."""
        result = self.conn.execute(
            "SELECT face_detection_done FROM media WHERE media_id = ?", 
            (media_id,)
        ).fetchone()
        return result is not None and result[0] == 1
    
    def mark_face_detection_done(self, media_id: str):
        """Mark that face detection has been completed for this media."""
        self.conn.execute(
            "UPDATE media SET face_detection_done = 1 WHERE media_id = ?",
            (media_id,)
        )
        self._maybe_commit()
    
    def mark_object_detection_done(self, media_id: str):
        """Mark that object detection has been completed for this media."""
        self.conn.execute(
            "UPDATE media SET object_detection_done = 1 WHERE media_id = ?",
            (media_id,)
        )
        self._maybe_commit()

    def get_total_changes(self) -> int:
        return int(self.conn.total_changes)

    def get_index_state(self) -> Dict[str, Any]:
        row = self.conn.execute(
            """
            SELECT index_version_seq, index_version_ts
            FROM index_state
            WHERE singleton_id = 1
            """
        ).fetchone()
        if row is None:
            self.conn.execute(
                """
                INSERT INTO index_state(
                    singleton_id,
                    index_version_seq,
                    index_version_ts
                ) VALUES (1, 0, NULL)
                """
            )
            self._maybe_commit()
            return {
                "index_version_seq": 0,
                "index_version_ts": None,
            }
        return {
            "index_version_seq": int(row[0] or 0),
            "index_version_ts": row[1],
        }

    def bump_index_version(self) -> Dict[str, Any]:
        state = self.get_index_state()
        next_seq = state["index_version_seq"] + 1
        next_ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        self.conn.execute(
            """
            UPDATE index_state
            SET index_version_seq = ?, index_version_ts = ?
            WHERE singleton_id = 1
            """,
            (next_seq, next_ts),
        )
        self._maybe_commit()
        return {
            "index_version_seq": next_seq,
            "index_version_ts": next_ts,
        }

    # --- Helpers expected by qdrant_export.py ---
    def iter_items(self):
        """Yield dicts for each image media item (videos excluded).

        Videos are handled separately via iter_video_keyframes(). The
        SQL-level ``mime LIKE 'image/%'`` filter is the primary guard;
        the path-extension fallback below catches legacy rows where
        ``mime`` was never populated.
        """
        VIDEO_EXTENSIONS = ('.mp4', '.mov', '.avi', '.mkv', '.m4v', '.wmv', '.flv', '.webm')
        cur = self.conn.execute(
            """
            SELECT media_id, path, source_name, rel_path, place, ts_utc, added_at
            FROM media
            WHERE deleted = 0 AND (mime LIKE 'image/%' OR mime IS NULL)
            """
        )
        for media_id, path, source_name, rel_path, place, ts_utc, added_at in cur:
            resolved_path = self._resolve_abs_path(path, source_name, rel_path)
            # Belt-and-braces: filter by extension too, in case a legacy
            # row has NULL mime but a video extension.
            if resolved_path and resolved_path.lower().endswith(VIDEO_EXTENSIONS):
                continue
                
            # Fetch tags for this media item
            tags_cur = self.conn.execute("""
                SELECT t.name FROM tag t
                JOIN media_tag mt ON t.tag_id = mt.tag_id
                WHERE mt.media_id = ?
            """, (media_id,))
            tags = [row[0] for row in tags_cur.fetchall()]
            
            yield {
                "id": media_id,
                "path": resolved_path,
                "people": [],
                "place": place,
                "ts": ts_utc,
                "added_at": added_at,
                "tags": tags,
            }

    def count_items(self) -> int:
        cur = self.conn.execute("SELECT COUNT(1) FROM media WHERE deleted=0")
        return cur.fetchone()[0]

    def delete_faces_for_media(self, media_id: str) -> int:
        """Remove all face rows for one media item.

        ``face_embedding`` rows reference ``face`` via ``ON DELETE
        CASCADE``, so the per-face embeddings are dropped automatically.
        Returns the number of face rows removed (useful for callers that
        want to log the cleanup).

        Called by the indexer when ``reprocess_faces=True`` to clear
        stale detections before re-running face detection on a media
        item. Manual labels (``face.person_id``) are necessarily lost
        in this path — the user opted in via the reprocessing flag.
        """
        cur = self.conn.execute("DELETE FROM face WHERE media_id = ?", (media_id,))
        self._maybe_commit()
        return int(cur.rowcount or 0)

    # --- Shots management for video-semantic search ---
    def delete_shots_for_video(self, video_id: str):
        self.conn.execute("DELETE FROM shots WHERE video_id=?", (video_id,))

    def add_shots(self, video_id: str, shots: list[tuple[float, float]], is_synthetic: bool = False):
        """
        Insert a list of (t_start, t_end) for a given video_id.
        Overwrites existing shots for that video.
        
        Args:
            video_id: The media_id of the video
            shots: List of (t_start, t_end) tuples in seconds
            is_synthetic: True if this is a fallback full-video shot, False if detected
        """
        self.delete_shots_for_video(video_id)
        synthetic_flag = 1 if is_synthetic else 0
        for idx, (t0, t1) in enumerate(shots):
            self.conn.execute(
                "INSERT INTO shots(video_id, shot_index, t_start, t_end, is_synthetic) VALUES (?,?,?,?,?)",
                (video_id, idx, float(t0), float(t1), synthetic_flag),
            )
        self._maybe_commit()

    def get_shots_for_video(self, video_id: str) -> list[dict]:
        cur = self.conn.execute(
            "SELECT id, shot_index, t_start, t_end FROM shots WHERE video_id=? ORDER BY shot_index ASC",
            (video_id,),
        )
        out = []
        for row in cur.fetchall():
            sid, idx, t0, t1 = row
            out.append({"id": sid, "shot_index": idx, "t_start": t0, "t_end": t1})
        return out
    
    def has_keyframes_for_video(self, video_id: str) -> bool:
        """Check if keyframes already exist for a video."""
        cur = self.conn.execute(
            "SELECT COUNT(*) FROM video_keyframes WHERE video_id=?",
            (video_id,),
        )
        count = cur.fetchone()[0]
        return count > 0

    # --- Keyframes metadata for video-semantic search ---
    def add_keyframes(self, video_id: str, entries: list[dict]):
        """
        Insert keyframe metadata rows.
        entries: list of dicts with keys: shot_index, kf_index, timestamp, shot_start, shot_end, tags (optional)
        Uses INSERT OR REPLACE to keep (video_id, shot_index, kf_index) unique.
        """
        if not entries:
            return
        import json
        for e in entries:
            tags_json = json.dumps(e.get("tags", [])) if e.get("tags") else None
            self.conn.execute(
                """
                INSERT INTO video_keyframes(
                  video_id, shot_index, kf_index, timestamp, shot_start, shot_end, tags,
                  gps_lat, gps_lon, gps_alt, gps_datetime_utc, gps_fix, gps_source, place
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(video_id, shot_index, kf_index) DO UPDATE SET
                  timestamp=excluded.timestamp,
                  shot_start=excluded.shot_start,
                  shot_end=excluded.shot_end,
                  tags=excluded.tags,
                  gps_lat=excluded.gps_lat,
                  gps_lon=excluded.gps_lon,
                  gps_alt=excluded.gps_alt,
                  gps_datetime_utc=excluded.gps_datetime_utc,
                  gps_fix=excluded.gps_fix,
                  gps_source=excluded.gps_source,
                  place=excluded.place
                """,
                (
                    video_id,
                    int(e["shot_index"]),
                    int(e["kf_index"]),
                    float(e["timestamp"]),
                    float(e["shot_start"]),
                    float(e["shot_end"]),
                    tags_json,
                    e.get("gps_lat"),
                    e.get("gps_lon"),
                    e.get("gps_alt"),
                    e.get("gps_datetime_utc"),
                    e.get("gps_fix"),
                    e.get("gps_source"),
                    e.get("place"),
                ),
            )
        self._maybe_commit()

    def iter_video_keyframes(self):
        """
        Yield keyframe rows joined with media path and tags for export.
        Returns dicts with: video_id, path, shot_index, kf_index, timestamp, shot_start, shot_end, tags, place, people
        """
        import json
        cur = self.conn.execute(
            """
            SELECT vk.video_id, m.path, m.source_name, m.rel_path, m.place,
                   vk.shot_index, vk.kf_index, vk.timestamp, vk.shot_start, vk.shot_end, vk.tags,
                   vk.gps_lat, vk.gps_lon, vk.gps_alt, vk.gps_datetime_utc, vk.gps_fix, vk.gps_source, vk.place
            FROM video_keyframes vk
            JOIN media m ON m.media_id = vk.video_id
            WHERE m.deleted = 0
            ORDER BY vk.video_id, vk.shot_index, vk.kf_index
            """
        )
        for video_id, path, source_name, rel_path, media_place, s_idx, k_idx, ts, s_start, s_end, tags_json, gps_lat, gps_lon, gps_alt, gps_dt, gps_fix, gps_source, keyframe_place in cur:
            resolved_path = self._resolve_abs_path(path, source_name, rel_path)
            # Parse keyframe-level tags from JSON (if available)
            keyframe_tags = json.loads(tags_json) if tags_json else []
            
            # Also collect media-level tags (for backwards compatibility and aggregation)
            tags_cur = self.conn.execute(
                """
                SELECT t.name FROM tag t
                JOIN media_tag mt ON t.tag_id = mt.tag_id
                WHERE mt.media_id = ?
                """,
                (video_id,),
            )
            media_tags = [row[0] for row in tags_cur.fetchall()]
            
            # Merge keyframe-specific tags with media-level tags (keyframe takes precedence)
            tags = keyframe_tags if keyframe_tags else media_tags
            
            # Fetch people (person names) associated with this video
            # Get unique person names from faces in this video
            people_cur = self.conn.execute(
                """
                SELECT DISTINCT p.name
                FROM face f
                JOIN person p ON f.person_id = p.person_id
                WHERE f.media_id = ? AND p.name IS NOT NULL
                ORDER BY p.name
                """,
                (video_id,),
            )
            people = [row[0] for row in people_cur.fetchall()]
            
            yield {
                "video_id": video_id,
                "path": resolved_path,
                "shot_index": s_idx,
                "kf_index": k_idx,
                "timestamp": ts,
                "shot_start": s_start,
                "shot_end": s_end,
                "tags": tags,
                "gps_lat": gps_lat,
                "gps_lon": gps_lon,
                "gps_alt": gps_alt,
                "gps_datetime_utc": gps_dt,
                "gps_fix": gps_fix,
                "gps_source": gps_source,
                "place": keyframe_place or media_place,
                "people": people,
            }

    # --- Face detection and recognition ---
    def add_faces(self, media_id: str, faces: list[dict]):
        """
        Insert face detection records for a media item.
        
        Args:
            media_id: The media_id of the photo/video
            faces: List of dicts with keys:
                - face_id: Unique identifier for this face
                - bbox: Tuple of (x, y, w, h) normalized 0-1
                - confidence: Detection confidence
                - person_id: (optional) Person identifier if recognized
                - gender: (optional) 'M' or 'F'
                - age: (optional) Estimated age
                - shot_index: (optional) For videos
                - kf_index: (optional) For videos
        """
        if not faces:
            return
        
        for face in faces:
            x, y, w, h = face["bbox"]
            self.conn.execute(
                """
                INSERT INTO face(face_id, media_id, x, y, w, h, confidence, person_id, gender, age, shot_index, kf_index)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(face_id) DO UPDATE SET
                  confidence=excluded.confidence,
                  person_id=excluded.person_id,
                  gender=excluded.gender,
                  age=excluded.age
                """,
                (
                    face["face_id"],
                    media_id,
                    float(x),
                    float(y),
                    float(w),
                    float(h),
                    float(face.get("confidence", 1.0)),
                    face.get("person_id"),
                    face.get("gender"),
                    face.get("age"),
                    face.get("shot_index"),
                    face.get("kf_index"),
                ),
            )
        self._maybe_commit()

    def get_media_faces(self, media_id: str) -> list[dict]:
        """
        Get all faces detected in a specific media item.
        
        Returns:
            List of dicts with face metadata including bbox, confidence, person info
        """
        cur = self.conn.execute(
            """
            SELECT f.face_id, f.x, f.y, f.w, f.h, f.confidence, 
                   f.person_id, p.name as person_name, f.gender, f.age,
                   f.shot_index, f.kf_index
            FROM face f
            LEFT JOIN person p ON f.person_id = p.person_id
            WHERE f.media_id = ?
            ORDER BY f.confidence DESC
            """,
            (media_id,),
        )
        
        faces = []
        for row in cur.fetchall():
            face_id, x, y, w, h, conf, person_id, person_name, gender, age, shot_idx, kf_idx = row
            faces.append({
                "face_id": face_id,
                "bbox": [x, y, w, h],
                "confidence": conf,
                "person_id": person_id,
                "person_name": person_name,
                "gender": gender,
                "age": age,
                "shot_index": shot_idx,
                "kf_index": kf_idx,
            })
        
        return faces

    def iter_faces(self, order_by='default'):
        """
        Yield all face detections with associated media information for export to vector DB.
        
        Args:
            order_by: Ordering method - 'default' (media_id, face_id), 
                     'labeled_first' (labeled faces first, then unknown)
        
        Returns dicts with: face_id, media_id, path, bbox, confidence, person_id, person_name, etc.
        """
        # Choose ORDER BY clause based on order_by parameter
        if order_by == 'labeled_first':
            # Sort by: labeled faces first (person_id IS NOT NULL), then by person_name, then face_id
            order_clause = "ORDER BY (f.person_id IS NULL), p.name, f.face_id"
        else:
            # Default ordering
            order_clause = "ORDER BY f.media_id, f.face_id"
        
        cur = self.conn.execute(
            f"""
            SELECT f.face_id, f.media_id, m.path, m.source_name, m.rel_path, m.ts_utc,
                   f.x, f.y, f.w, f.h, f.confidence,
                   f.person_id, p.name as person_name,
                   f.gender, f.age, f.shot_index, f.kf_index
            FROM face f
            JOIN media m ON f.media_id = m.media_id
            LEFT JOIN person p ON f.person_id = p.person_id
            WHERE m.deleted = 0
            {order_clause}
            """
        )
        
        for row in cur.fetchall():
            (
                face_id, media_id, path, source_name, rel_path, ts_utc,
                x, y, w, h, confidence,
                person_id, person_name,
                gender, age, shot_idx, kf_idx
            ) = row
            resolved_path = self._resolve_abs_path(path, source_name, rel_path)
            
            # Determine media type from path
            media_type = "video" if resolved_path and any(resolved_path.lower().endswith(ext) for ext in ['.mp4', '.mov', '.avi', '.mkv', '.m4v']) else "image"
            
            yield {
                "face_id": face_id,
                "media_id": media_id,
                "path": resolved_path,
                "date": ts_utc,
                "bbox": [x, y, w, h],
                "confidence": confidence,
                "person_id": person_id,
                "person_name": person_name,
                "gender": gender,
                "age": age,
                "type": media_type,
                "shot_index": shot_idx,
                "kf_index": kf_idx,
            }

    def get_unique_faces_per_person(self, limit: int = 10000, offset: int = 0, labeled: Optional[str] = None):
        """
        Get one representative face per person (for labeled faces) and all unknown faces.
        Labeled faces are grouped by person_id, returning the face with highest confidence.
        Unknown faces are returned individually.

        Args:
            limit: Maximum number of faces to return
            offset: Number of faces to skip (pagination)
            labeled: Filter — "known" (labeled only), "unknown" (unlabeled only), or None/"all" (both)

        Returns list of dicts with: face_id, media_id, path, person_id, person_name, thumbnail info, etc.
        """
        faces = []
        want_known   = labeled in (None, "all", "known")
        want_unknown = labeled in (None, "all", "unknown")

        # ── Known (labeled) faces: one representative per person ─────────────
        if want_known:
            cur = self.conn.execute(
                """
                SELECT f.face_id, f.media_id, m.path, m.source_name, m.rel_path,
                       f.x, f.y, f.w, f.h, f.confidence,
                       f.person_id, p.name as person_name,
                       f.gender, f.age, f.shot_index, f.kf_index
                FROM face f
                JOIN media m ON f.media_id = m.media_id
                LEFT JOIN person p ON f.person_id = p.person_id
                WHERE m.deleted = 0 AND f.person_id IS NOT NULL
                ORDER BY p.name, f.confidence DESC
                """
            )
            seen_persons: set = set()
            for row in cur.fetchall():
                (
                    face_id, media_id, path, source_name, rel_path,
                    x, y, w, h, confidence,
                    person_id, person_name,
                    gender, age, shot_idx, kf_idx
                ) = row
                if person_id not in seen_persons:
                    seen_persons.add(person_id)
                    resolved_path = self._resolve_abs_path(path, source_name, rel_path)
                    media_type = "video" if resolved_path and any(
                        resolved_path.lower().endswith(ext)
                        for ext in ['.mp4', '.mov', '.avi', '.mkv', '.m4v']
                    ) else "image"
                    faces.append({
                        "face_id": face_id,
                        "media_id": media_id,
                        "path": resolved_path,
                        "bbox": [x, y, w, h],
                        "confidence": confidence,
                        "person_id": person_id,
                        "person_name": person_name,
                        "gender": gender,
                        "age": age,
                        "type": media_type,
                        "shot_index": shot_idx,
                        "kf_index": kf_idx,
                    })

        # ── Apply pagination across the combined result ───────────────────────
        if want_unknown:
            labeled_count = len(faces)
            if offset < labeled_count:
                faces = faces[offset:offset + limit]
                remaining_limit = limit - len(faces)
                unknown_offset = 0
            else:
                faces = []
                unknown_offset = offset - labeled_count
                remaining_limit = limit
        else:
            # Known-only: simple Python slice
            faces = faces[offset: offset + limit]
            remaining_limit = 0
            unknown_offset = 0

        # ── Unknown (unlabeled) faces ─────────────────────────────────────────
        if want_unknown and remaining_limit > 0:
            cur = self.conn.execute(
                """
                SELECT f.face_id, f.media_id, m.path, m.source_name, m.rel_path,
                       f.x, f.y, f.w, f.h, f.confidence,
                       f.person_id, f.gender, f.age, f.shot_index, f.kf_index
                FROM face f
                JOIN media m ON f.media_id = m.media_id
                WHERE m.deleted = 0 AND f.person_id IS NULL
                ORDER BY f.confidence DESC
                LIMIT ? OFFSET ?
                """,
                (remaining_limit, unknown_offset)
            )
            for row in cur.fetchall():
                (
                    face_id, media_id, path, source_name, rel_path,
                    x, y, w, h, confidence,
                    person_id, gender, age, shot_idx, kf_idx
                ) = row
                resolved_path = self._resolve_abs_path(path, source_name, rel_path)
                media_type = "video" if resolved_path and any(
                    resolved_path.lower().endswith(ext)
                    for ext in ['.mp4', '.mov', '.avi', '.mkv', '.m4v']
                ) else "image"
                faces.append({
                    "face_id": face_id,
                    "media_id": media_id,
                    "path": resolved_path,
                    "bbox": [x, y, w, h],
                    "confidence": confidence,
                    "person_id": person_id,
                    "person_name": None,
                    "gender": gender,
                    "age": age,
                    "type": media_type,
                    "shot_index": shot_idx,
                    "kf_index": kf_idx,
                })

        return faces

    def count_unique_faces_per_person(self, labeled: Optional[str] = None) -> int:
        """Count list_faces() results without pagination.

        Known faces count as one representative face per labeled person.
        Unknown faces count individually.
        """
        want_known = labeled in (None, "all", "known")
        want_unknown = labeled in (None, "all", "unknown")

        total = 0

        if want_known:
            cur = self.conn.execute(
                """
                SELECT COUNT(DISTINCT f.person_id)
                FROM face f
                JOIN media m ON f.media_id = m.media_id
                WHERE m.deleted = 0 AND f.person_id IS NOT NULL
                """
            )
            total += int(cur.fetchone()[0] or 0)

        if want_unknown:
            cur = self.conn.execute(
                """
                SELECT COUNT(*)
                FROM face f
                JOIN media m ON f.media_id = m.media_id
                WHERE m.deleted = 0 AND f.person_id IS NULL
                """
            )
            total += int(cur.fetchone()[0] or 0)

        return total


    def update_face_person(self, face_id: str, person_id: str):
        """Update the person_id for a face (used in labeling/clustering)."""
        self.conn.execute(
            "UPDATE face SET person_id=? WHERE face_id=?",
            (person_id, face_id),
        )
        self.commit()

    def update_faces_person_batch(self, face_ids: list[str], person_id: str) -> int:
        """Label multiple faces with the same person in one transaction. Returns count updated."""
        unique_ids = list(dict.fromkeys(face_ids))  # deduplicate, preserve order
        before = self.conn.total_changes
        self.conn.executemany(
            "UPDATE face SET person_id=? WHERE face_id=?",
            [(person_id, fid) for fid in unique_ids],
        )
        self.commit()
        return self.conn.total_changes - before

    def clear_face_person(self, face_id: str):
        """Remove person assignment from a face (set person_id=NULL)."""
        self.conn.execute(
            "UPDATE face SET person_id=NULL WHERE face_id=?",
            (face_id,),
        )
        self.commit()

    def get_unassigned_faces(self) -> list[dict]:
        """Get all faces without a person_id (for clustering)."""
        cur = self.conn.execute(
            """
            SELECT face_id, media_id, x, y, w, h, confidence
            FROM face
            WHERE person_id IS NULL
            ORDER BY confidence DESC
            """
        )
        
        faces = []
        for row in cur.fetchall():
            face_id, media_id, x, y, w, h, confidence = row
            faces.append({
                "face_id": face_id,
                "media_id": media_id,
                "bbox": [x, y, w, h],
                "confidence": confidence,
            })
        
        return faces

    # ---------------- People management ----------------
    def create_person(self, name: str) -> Dict[str, Any]:
        """Create a new person with a unique name. Returns {person_id, name}."""
        pid = str(uuid.uuid4())
        try:
            self.conn.execute(
                "INSERT INTO person(person_id, name, is_labeled) VALUES (?, ?, 1)",
                (pid, name),
            )
            self.commit()
        except sqlite3.IntegrityError as e:
            # Likely UNIQUE(name) violation
            raise
        return {"person_id": pid, "name": name}

    def list_people(self) -> List[Dict[str, Any]]:
        """List people with live face counts and a representative face thumbnail.
        Only counts faces from non-deleted media."""
        cur = self.conn.execute(
            """
            SELECT p.person_id, p.name,
                   (SELECT COUNT(*) FROM face f JOIN media m ON f.media_id = m.media_id
                    WHERE f.person_id = p.person_id AND m.deleted = 0) as face_count,
                   (SELECT f2.face_id FROM face f2 JOIN media m2 ON f2.media_id = m2.media_id
                    WHERE f2.person_id = p.person_id AND m2.deleted = 0
                    ORDER BY f2.confidence DESC LIMIT 1) as thumbnail_face_id
            FROM person p
            ORDER BY p.name COLLATE NOCASE ASC
            """
        )
        out: List[Dict[str, Any]] = []
        for pid, name, count, thumb_face_id in cur.fetchall():
            out.append({
                "person_id": pid,
                "name": name,
                "face_count": int(count or 0),
                "thumbnail_face_id": thumb_face_id,
            })
        return out

    def rename_person(self, person_id: str, new_name: str):
        """Rename a person to a unique new name. Raises IntegrityError on conflict."""
        try:
            self.conn.execute(
                "UPDATE person SET name=? WHERE person_id=?",
                (new_name, person_id),
            )
            self.commit()
        except sqlite3.IntegrityError:
            raise

    def merge_people(self, source_id: str, target_id: str) -> int:
        """Merge source person into target person by reassigning faces and deleting source.

        Returns the number of faces reassigned.
        """
        if source_id == target_id:
            raise ValueError("Cannot merge a person into itself")
        # Reassign faces
        cur = self.conn.execute(
            "UPDATE face SET person_id=? WHERE person_id=?",
            (target_id, source_id),
        )
        reassigned = cur.rowcount if cur is not None else 0
        # Remove source person row
        self.conn.execute("DELETE FROM person WHERE person_id=?", (source_id,))
        self.commit()
        return int(reassigned)

    def get_person(self, person_id: str) -> Optional[Dict[str, Any]]:
        cur = self.conn.execute(
            "SELECT person_id, name FROM person WHERE person_id=?",
            (person_id,),
        )
        row = cur.fetchone()
        if not row:
            return None
        return {"person_id": row[0], "name": row[1]}

    def find_person_by_name(self, name: str) -> Optional[Dict[str, Any]]:
        cur = self.conn.execute(
            "SELECT person_id, name FROM person WHERE name=?",
            (name,),
        )
        row = cur.fetchone()
        if not row:
            return None
        return {"person_id": row[0], "name": row[1]}
