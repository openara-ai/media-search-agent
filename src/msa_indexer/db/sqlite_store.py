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


def _media_path_unique_index_names(conn: sqlite3.Connection) -> List[str]:
    """Names of UNIQUE-constraint indexes on media that cover exactly (path).

    A column-level ``UNIQUE`` shows up in ``PRAGMA index_list`` with
    origin ``'u'`` (constraint-created), typically named
    ``sqlite_autoindex_media_N``. Detecting it via the index list is
    robust against DDL formatting differences across historical DBs.
    """
    names: List[str] = []
    try:
        for row in conn.execute("PRAGMA index_list(media)").fetchall():
            # (seq, name, unique, origin, partial)
            name, unique, origin = row[1], row[2], row[3]
            if not unique or origin != "u":
                continue
            cols = [r[2] for r in conn.execute(f'PRAGMA index_info("{name}")').fetchall()]
            if cols == ["path"]:
                names.append(name)
    except sqlite3.Error:
        return []
    return names


def media_path_rebuild_pending(sqlite_path) -> bool:
    """True if the DB at ``sqlite_path`` still carries UNIQUE(media.path).

    Used by the indexer entry point to decide whether to take the
    pre-migration backup BEFORE opening the main store connection
    (§3.1a, R4). Opens (and closes) its own throwaway connection;
    a missing file or missing media table means no rebuild is pending.
    """
    p = Path(sqlite_path)
    if not p.exists():
        return False
    try:
        conn = sqlite3.connect(str(p))
    except sqlite3.Error:
        return False
    try:
        row = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='media'"
        ).fetchone()
        if row is None:
            return False
        return bool(_media_path_unique_index_names(conn))
    except sqlite3.Error:
        return False
    finally:
        conn.close()


# media columns whose write is payload-relevant (M-8/S-3 §4.1): they feed at
# least one Qdrant payload builder (see payload_columns.PAYLOAD_SOURCES).
# gps_lat/gps_lon accompany place refreshes and are included conservatively.
_MEDIA_PAYLOAD_FIELDS = frozenset(
    {"path", "source_name", "rel_path", "place", "ts_utc", "added_at", "gps_lat", "gps_lon"}
)


class SQLiteStore:
    def __init__(self, path: Path, autocommit: bool = True):
        self.path = path
        # timeout (busy wait) raised from the sqlite3 default of 5s: the API's
        # §4 payload-write guard (M-8/S-2) holds its deferred-commit write
        # transaction across the Qdrant payload sync — bounded but multi-second
        # for a large person merge — and a concurrent indexer batch commit
        # should queue behind it rather than error with "database is locked".
        # WAL keeps readers unaffected; this only changes behavior under
        # writer-writer contention, where waiting is strictly more robust
        # than failing.
        self.conn = sqlite3.connect(str(path), timeout=30.0)
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
        # M-8/S-3 §4.1: run-scoped stamp. The indexer pipeline sets this to
        # pending_seq (index_version_seq + 1 at run start) so every
        # payload-relevant write of the run stamps its embedding rows with
        # the seq the end-of-run bump will commit. When None (API-side /
        # out-of-band writers), _stamp() allocates index_version_seq + 1
        # transactionally on this connection — committed or rolled back
        # atomically with the payload write it stamps.
        self.stamp_seq: Optional[int] = None

    def _maybe_commit(self):
        if self.autocommit:
            self.commit()

    # ── M-8/S-3 §4.1: per-row dirty stamping ──────────────────────────────
    #
    # Every payload-relevant write stamps the embedding rows whose Qdrant
    # payload embeds the written cell, so delta export (`updated_seq >
    # exported_seq`) can never miss a payload-only change (R7). The
    # write→stamp mapping is declared in payload_columns.py and locked by
    # the coverage test in tests/test_delta_export.py. Media-level writes
    # use the SUPERSET stamp (image row + all keyframe rows + all face rows
    # of the media): coarser than strictly needed per column, but correct by
    # construction and immune to per-column-precision drift.

    def _stamp(self) -> int:
        """The seq to stamp writes with.

        Inside an indexer run: the run's pending_seq (set by the pipeline).
        Outside a run (API label/rename/merge): index_version_seq + 1, read
        on THIS connection so the allocation shares the caller's deferred
        transaction — the S-2 commit-gated endpoints roll the stamp back
        together with the payload write (no orphan stamps).
        """
        if self.stamp_seq is not None:
            return int(self.stamp_seq)
        row = self.conn.execute(
            "SELECT index_version_seq FROM index_state WHERE singleton_id = 1"
        ).fetchone()
        return (int(row[0]) if row is not None and row[0] is not None else 0) + 1

    def _stamp_media_payload_rows(self, media_id: str, seq: Optional[int] = None) -> None:
        """Superset stamp for a media-level payload write: the media's
        image_embedding row, all its keyframe_embedding rows, and its
        faces' face_embedding rows. No-ops harmlessly for rows that don't
        exist (e.g. brand-new media whose embeddings land later — their
        upserts stamp themselves)."""
        if seq is None:
            seq = self._stamp()
        self.conn.execute(
            "UPDATE image_embedding SET updated_seq = ? WHERE media_id = ?",
            (int(seq), media_id),
        )
        self.conn.execute(
            """
            UPDATE keyframe_embedding SET updated_seq = ?
            WHERE keyframe_id IN (SELECT id FROM video_keyframes WHERE video_id = ?)
            """,
            (int(seq), media_id),
        )
        self.conn.execute(
            """
            UPDATE face_embedding SET updated_seq = ?
            WHERE face_id IN (SELECT face_id FROM face WHERE media_id = ?)
            """,
            (int(seq), media_id),
        )

    def _stamp_face_rows(self, face_ids: List[str], seq: Optional[int] = None) -> None:
        """Stamp specific faces' embedding rows PLUS their owning videos'
        keyframe rows (video payloads embed people names). Chunked to stay
        under SQLite's bound-parameter limit."""
        if not face_ids:
            return
        if seq is None:
            seq = self._stamp()
        CHUNK = 500
        for i in range(0, len(face_ids), CHUNK):
            chunk = face_ids[i:i + CHUNK]
            ph = ",".join(["?"] * len(chunk))
            self.conn.execute(
                f"UPDATE face_embedding SET updated_seq = ? WHERE face_id IN ({ph})",
                [int(seq), *chunk],
            )
            self.conn.execute(
                f"""
                UPDATE keyframe_embedding SET updated_seq = ?
                WHERE keyframe_id IN (
                    SELECT vk.id FROM video_keyframes vk
                    WHERE vk.video_id IN (
                        SELECT DISTINCT media_id FROM face WHERE face_id IN ({ph})
                    )
                )
                """,
                [int(seq), *chunk],
            )

    def _stamp_person_rows(self, person_id: str, seq: Optional[int] = None) -> None:
        """Stamp every face of a person + every keyframe row of videos that
        contain those faces (face payloads carry person_name; keyframe
        payloads carry people names)."""
        if seq is None:
            seq = self._stamp()
        self.conn.execute(
            """
            UPDATE face_embedding SET updated_seq = ?
            WHERE face_id IN (SELECT face_id FROM face WHERE person_id = ?)
            """,
            (int(seq), person_id),
        )
        self.conn.execute(
            """
            UPDATE keyframe_embedding SET updated_seq = ?
            WHERE keyframe_id IN (
                SELECT vk.id FROM video_keyframes vk
                WHERE vk.video_id IN (
                    SELECT DISTINCT media_id FROM face WHERE person_id = ?
                )
            )
            """,
            (int(seq), person_id),
        )

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

        Also performs the one-time §3.1a path-authority rebuild (drop
        ``UNIQUE(media.path)``) when a pre-M-8 media table is detected.
        Like the face migration, the rebuild is indexer-entry-only:
        :meth:`init_schema_no_migrations` stays non-mutating for existing
        tables. The indexer takes a pre-migration backup first, gated on
        :func:`media_path_rebuild_pending`.
        """
        self._maybe_drop_legacy_face_table()
        self._apply_schema_and_additive_migrations(schema_path)
        self._maybe_rebuild_media_drop_unique_path(schema_path)

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

        # Migration (M-8): Add missing_since_scan_id column for the deletion
        # sweep's legacy-orphan grace state. Fresh installs get it from
        # schema.sql; upgraded DBs need the additive ALTER.
        try:
            cursor = self.conn.execute("PRAGMA table_info(media)")
            columns = [col[1] for col in cursor.fetchall()]
            if 'missing_since_scan_id' not in columns:
                print("⚠️  Adding missing_since_scan_id column to media table...")
                self.conn.execute("ALTER TABLE media ADD COLUMN missing_since_scan_id INTEGER")
                self.conn.commit()
        except Exception:
            pass

        # Migration (M-8/S-3, R10): per-row dirty stamps for delta Qdrant
        # export. Additive only (ADD COLUMN / CREATE INDEX IF NOT EXISTS).
        # Unlike the legacy blocks above, failures here RAISE (after
        # rollback) instead of being swallowed: a silently-skipped stamp
        # column or tombstone seed would mean permanently stale / dangling
        # Qdrant points — the exact silent-divergence class S-3 closes.
        # Each ALTER + its seed commit atomically (SQLite DDL is
        # transactional), so a partial migration cannot persist.
        try:
            # updated_seq on the three embedding tables + covering indexes.
            # The indexes are created HERE (not schema.sql) because on a
            # pre-S-3 DB schema.sql runs before the column exists; this
            # path runs for fresh DBs too, so parity holds.
            for _table, _index in (
                ("image_embedding", "idx_image_emb_updated"),
                ("keyframe_embedding", "idx_kf_emb_updated"),
                ("face_embedding", "idx_face_emb_updated"),
            ):
                cols = [c[1] for c in self.conn.execute(f"PRAGMA table_info({_table})").fetchall()]
                if 'updated_seq' not in cols:
                    print(f"⚠️  Adding updated_seq column to {_table} table (M-8/S-3)...")
                    self.conn.execute(
                        f"ALTER TABLE {_table} ADD COLUMN updated_seq INTEGER NOT NULL DEFAULT 0"
                    )
                self.conn.execute(
                    f"CREATE INDEX IF NOT EXISTS {_index} ON {_table}(updated_seq)"
                )
            self.conn.commit()

            # media.deleted_seq + pre-existing tombstone seeding. Seeded at
            # index_version_seq + 1 — ABOVE the exported watermark, not at
            # it (plan §4.1, P2): when SQLite and Qdrant are in sync at
            # migration, a stamp at the current seq would never satisfy
            # `deleted_seq > since_seq` and the dangling points would
            # survive forever. One seq ahead, the §4.2 dirty trigger picks
            # them up on the next run. Same-transaction as the ALTER so the
            # column can never exist unseeded.
            cols = [c[1] for c in self.conn.execute("PRAGMA table_info(media)").fetchall()]
            if 'deleted_seq' not in cols:
                print("⚠️  Adding deleted_seq column to media table (M-8/S-3)...")
                self.conn.execute("ALTER TABLE media ADD COLUMN deleted_seq INTEGER")
                self.conn.execute(
                    """
                    UPDATE media SET deleted_seq = (
                        SELECT index_version_seq + 1 FROM index_state WHERE singleton_id = 1
                    )
                    WHERE deleted = 1 AND deleted_seq IS NULL
                    """
                )
            self.conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_media_deleted_seq ON media(deleted_seq)"
            )
            self.conn.commit()

            # index_state: s3_migration_seq (first-delta-run guard input,
            # seeded ONCE to the migration-time seq — never re-seeded) and
            # the durable face_recreate_required marker.
            cols = [c[1] for c in self.conn.execute("PRAGMA table_info(index_state)").fetchall()]
            if 's3_migration_seq' not in cols:
                print("⚠️  Adding s3_migration_seq column to index_state table (M-8/S-3)...")
                self.conn.execute(
                    "ALTER TABLE index_state ADD COLUMN s3_migration_seq INTEGER NOT NULL DEFAULT 0"
                )
                self.conn.execute(
                    "UPDATE index_state SET s3_migration_seq = index_version_seq WHERE singleton_id = 1"
                )
            if 'face_recreate_required' not in cols:
                print("⚠️  Adding face_recreate_required column to index_state table (M-8/S-3)...")
                self.conn.execute(
                    "ALTER TABLE index_state ADD COLUMN face_recreate_required INTEGER NOT NULL DEFAULT 0"
                )
            self.conn.commit()
        except Exception:
            self.conn.rollback()
            raise

    def _maybe_rebuild_media_drop_unique_path(self, schema_path: Path):
        """§3.1a one-time table rebuild: drop ``UNIQUE`` on ``media.path``.

        SQLite cannot drop a constraint in place, so: disable foreign keys
        (BEFORE any transaction — the pragma is silently ignored while a
        transaction is active), then in one transaction create the
        replacement table from the canonical schema.sql media DDL (which
        post-M-8 has no UNIQUE on path), copy all rows, drop the old
        table, rename, recreate the media indexes, verify row count and
        ``PRAGMA foreign_key_check``, commit. ``media_id`` is untouched, so
        child tables (FK on media_id) need no changes. No-op when the
        constraint is already gone.
        """
        if not _media_path_unique_index_names(self.conn):
            return

        with open(schema_path, "r") as f:
            schema_sql = f.read()
        # Extract the media DDL by letting SQLite itself parse schema.sql in
        # a scratch in-memory DB, then rename the table so SQLite rewrites
        # the CREATE statement authoritatively under the rebuild name. (A
        # regex over the raw SQL would silently truncate at the first ");"
        # inside a future column comment or CHECK constraint.)
        scratch = sqlite3.connect(":memory:")
        try:
            scratch.executescript(schema_sql)
            scratch.execute("ALTER TABLE media RENAME TO media_rebuild_m8")
            ddl_row = scratch.execute(
                "SELECT sql FROM sqlite_master WHERE type='table' AND name='media_rebuild_m8'"
            ).fetchone()
        except sqlite3.Error as e:
            raise RuntimeError(
                f"media DDL not found in schema.sql — cannot run the §3.1a rebuild ({e})"
            )
        finally:
            scratch.close()
        if ddl_row is None or not ddl_row[0]:
            raise RuntimeError(
                "media DDL not found in schema.sql — cannot run the §3.1a rebuild"
            )
        create_rebuild_sql = ddl_row[0]

        print("⚠️  Rebuilding media table to drop UNIQUE(path) (M-8 §3.1a)...")

        # Close any open transaction so PRAGMA foreign_keys takes effect,
        # then take explicit transaction control for the rebuild.
        self.conn.commit()
        old_isolation = self.conn.isolation_level
        self.conn.isolation_level = None  # autocommit; we BEGIN explicitly
        try:
            self.conn.execute("PRAGMA foreign_keys=OFF")
            self.conn.execute("BEGIN IMMEDIATE")
            try:
                self.conn.execute("DROP TABLE IF EXISTS media_rebuild_m8")
                self.conn.execute(create_rebuild_sql)

                old_cols = [
                    r[1]
                    for r in self.conn.execute("PRAGMA table_info(media)").fetchall()
                ]
                new_cols = [
                    r[1]
                    for r in self.conn.execute(
                        "PRAGMA table_info(media_rebuild_m8)"
                    ).fetchall()
                ]
                copy_cols = [c for c in old_cols if c in new_cols]
                col_list = ", ".join(copy_cols)
                self.conn.execute(
                    f"INSERT INTO media_rebuild_m8 ({col_list}) SELECT {col_list} FROM media"
                )

                old_count = self.conn.execute("SELECT COUNT(*) FROM media").fetchone()[0]
                new_count = self.conn.execute(
                    "SELECT COUNT(*) FROM media_rebuild_m8"
                ).fetchone()[0]
                if old_count != new_count:
                    raise RuntimeError(
                        f"media rebuild row-count mismatch: {old_count} != {new_count}"
                    )

                self.conn.execute("DROP TABLE media")
                self.conn.execute("ALTER TABLE media_rebuild_m8 RENAME TO media")

                # Recreate the media indexes (the UNIQUE autoindex doubled as
                # the path lookup — idx_media_path replaces it, non-unique).
                self.conn.execute(
                    "CREATE INDEX IF NOT EXISTS idx_media_source_rel ON media(source_name, rel_path)"
                )
                self.conn.execute(
                    "CREATE INDEX IF NOT EXISTS idx_media_ts ON media(ts_utc)"
                )
                self.conn.execute(
                    "CREATE INDEX IF NOT EXISTS idx_media_path ON media(path)"
                )
                self.conn.execute(
                    "CREATE INDEX IF NOT EXISTS idx_media_deleted_seq ON media(deleted_seq)"
                )

                fk_issues = self.conn.execute("PRAGMA foreign_key_check").fetchall()
                if fk_issues:
                    raise RuntimeError(
                        f"media rebuild foreign_key_check failed: {fk_issues[:5]}"
                    )
                self.conn.execute("COMMIT")
            except Exception:
                self.conn.execute("ROLLBACK")
                raise
        finally:
            self.conn.execute("PRAGMA foreign_keys=ON")
            self.conn.isolation_level = old_isolation

    def upsert_media(self, row: Dict[str, Any]):
        cols = ",".join(row.keys())
        placeholders = ",".join([":" + k for k in row.keys()])
        sql = f"INSERT INTO media ({cols}) VALUES ({placeholders}) " \
              f"ON CONFLICT(media_id) DO UPDATE SET " + ",".join([f"{k}=excluded.{k}" for k in row.keys() if k!="media_id"])
        self.conn.execute(sql, row)
        # §4.1 (media_superset rule): every upsert carries payload columns
        # (path/place/ts_utc). New media has no embedding rows yet (no-op);
        # a metadata refresh stamps the existing ones.
        self._stamp_media_payload_rows(row["media_id"])
        self._maybe_commit()

    def update_media_fields(self, media_id: str, fields: Dict[str, Any]):
        if not fields:
            return
        assignments = ",".join([f"{key}=?" for key in fields.keys()])
        params = list(fields.values()) + [media_id]
        self.conn.execute(f"UPDATE media SET {assignments} WHERE media_id = ?", params)
        # §4.1 (media_superset rule): moves/path-promotion, place/timestamp
        # refreshes — all three collections embed these cells. Non-payload
        # bookkeeping (gps_data_mode, flags) stays stamp-free.
        if not _MEDIA_PAYLOAD_FIELDS.isdisjoint(fields):
            self._stamp_media_payload_rows(media_id)
        self._maybe_commit()

    def add_tags(self, media_id: str, tags: Iterable[str]):
        stamped_any = False
        for t in tags:
            self.conn.execute("INSERT OR IGNORE INTO tag(name) VALUES (?)", (t,))
            tag_id = self.conn.execute("SELECT tag_id FROM tag WHERE name=?", (t,)).fetchone()[0]
            self.conn.execute("INSERT OR IGNORE INTO media_tag(media_id, tag_id) VALUES (?,?)", (media_id, tag_id))
            stamped_any = True
        # §4.1 (media_superset rule): image payloads carry tags; video
        # payloads fall back to media-level tags.
        if stamped_any:
            self._stamp_media_payload_rows(media_id)
        self._maybe_commit()

    # ── Embedding storage (Stage 3 of SQLITE_INCREMENTAL_VISIBILITY_PLAN) ──
    #
    # Embeddings are stored as raw float32 BLOBs in three sibling tables
    # joined by primary key to media / face / video_keyframes. Keeping
    # them out of the parent rows means browse paths can never accidentally
    # pull a 3 KB BLOB into a result set, and dropping any embedding
    # table forces a re-embed without disturbing labels or metadata.

    def upsert_image_embedding(self, media_id: str, embedding, model: str) -> None:
        """Insert or replace the CLIP embedding for an image-type media row.

        Stamps updated_seq on BOTH the insert and the conflict-update arm
        (§4.1): a re-embed of an existing row must re-export it too.
        """
        blob, dim = self._serialize_embedding(embedding)
        self.conn.execute(
            """
            INSERT INTO image_embedding(media_id, embedding, embedding_dim, embedding_model, updated_seq)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(media_id) DO UPDATE SET
              embedding=excluded.embedding,
              embedding_dim=excluded.embedding_dim,
              embedding_model=excluded.embedding_model,
              updated_seq=excluded.updated_seq
            """,
            (media_id, blob, dim, model, self._stamp()),
        )
        self._maybe_commit()

    def upsert_keyframe_embedding(self, keyframe_id: int, embedding, model: str) -> None:
        """Insert or replace the CLIP embedding for a video keyframe row.

        Stamps updated_seq on both arms (§4.1).
        """
        blob, dim = self._serialize_embedding(embedding)
        self.conn.execute(
            """
            INSERT INTO keyframe_embedding(keyframe_id, embedding, embedding_dim, embedding_model, updated_seq)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(keyframe_id) DO UPDATE SET
              embedding=excluded.embedding,
              embedding_dim=excluded.embedding_dim,
              embedding_model=excluded.embedding_model,
              updated_seq=excluded.updated_seq
            """,
            (int(keyframe_id), blob, dim, model, self._stamp()),
        )
        self._maybe_commit()

    def upsert_face_embedding(self, face_id: str, embedding, model: str) -> None:
        """Insert or replace the face embedding for a detection row.

        Stamps updated_seq on both arms (§4.1).
        """
        blob, dim = self._serialize_embedding(embedding)
        self.conn.execute(
            """
            INSERT INTO face_embedding(face_id, embedding, embedding_dim, embedding_model, updated_seq)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(face_id) DO UPDATE SET
              embedding=excluded.embedding,
              embedding_dim=excluded.embedding_dim,
              embedding_model=excluded.embedding_model,
              updated_seq=excluded.updated_seq
            """,
            (face_id, blob, dim, model, self._stamp()),
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

    # ── M-8 fingerprint fast-path: scan_run / file_fingerprint helpers ────
    #
    # file_fingerprint is the authoritative path -> content mapping
    # (M8_INCREMENTAL_INDEXING_PLAN.md §3.1). scan_run rows give the
    # deletion sweep its "completed scan" boundary. None of these writes
    # count as index-content changes — the pipeline's content_changed
    # flag (§3.5) is deliberately NOT set by fingerprint bookkeeping.

    def begin_scan_run(self) -> int:
        """Open a scan_run row and return its scan_id."""
        cur = self.conn.execute("INSERT INTO scan_run DEFAULT VALUES")
        self._maybe_commit()
        return int(cur.lastrowid)

    def complete_scan_run(self, scan_id: int, sources_json: str) -> None:
        """Mark a scan_run as cleanly completed (all sources fully walked)."""
        self.conn.execute(
            "UPDATE scan_run SET completed_at = datetime('now'), sources_json = ? WHERE scan_id = ?",
            (sources_json, int(scan_id)),
        )
        self._maybe_commit()

    def get_fingerprint(self, source_name: str, rel_path: str) -> Optional[Dict[str, Any]]:
        """Look up the fingerprint row for one (source_name, rel_path) key."""
        row = self.conn.execute(
            """
            SELECT size_bytes, mtime_ns, media_id, last_seen_scan_id, missing_since_scan_id
            FROM file_fingerprint WHERE source_name = ? AND rel_path = ?
            """,
            (source_name, rel_path),
        ).fetchone()
        if row is None:
            return None
        return {
            "source_name": source_name,
            "rel_path": rel_path,
            "size_bytes": int(row[0]),
            "mtime_ns": int(row[1]),
            "media_id": row[2],
            "last_seen_scan_id": row[3],
            "missing_since_scan_id": row[4],
        }

    def get_fingerprints_for_source(self, source_name: str) -> Dict[str, tuple]:
        """Bulk-load one source's fingerprints as
        ``{rel_path: (size, mtime_ns, media_id)}``.

        The count-phase ETA (#208) counts expected fast-path skips without a
        per-file query — it compares each walked file's on-disk stat against
        the stored ``(size_bytes, mtime_ns)`` here (the exact fast-path skip
        condition) and, on a match, resolves the processing snapshot by the
        row's ``media_id`` — NOT its rel_path — so every live duplicate path of
        one complete ``media_id`` is recognized as a free skip, exactly as the
        main loop does (it keys the skip off ``fp["media_id"]``); a rel_path
        lookup would miss non-canonical duplicate paths, which have no ``media``
        row of their own (#208 review, Codex P2). Read-only; safe on any DB
        where ``file_fingerprint`` exists (an OperationalError on a pre-M-8 DB
        is the caller's cue to fall back to the full-library estimate).
        """
        cur = self.conn.execute(
            "SELECT rel_path, size_bytes, mtime_ns, media_id FROM file_fingerprint WHERE source_name = ?",
            (source_name,),
        )
        return {row[0]: (int(row[1]), int(row[2]), row[3]) for row in cur.fetchall()}

    def get_processing_snapshot_for_media_ids(self, media_ids) -> Dict[str, dict]:
        """Bulk-load live media rows keyed by ``media_id`` with the inputs the
        count-phase ETA needs to decide whether a fingerprint stat-match is a
        *truly-free* skip (#208 review, Codex P2) — without a per-file query.

        Loaded by the SET of ``media_id`` values a source's fingerprint rows
        reference — NOT filtered on ``media.source_name`` — so a *cross-source
        duplicate* resolves correctly (#208 review, Codex P2): content first
        indexed under source A owns a single ``media`` row whose canonical
        ``source_name`` is A; when the same bytes later appear under source B,
        B's fingerprint row points at A's ``media_id``. The main loop reuses
        ``fp["media_id"]`` and skips it, so the count phase must resolve that
        media_id too even though the row's ``source_name`` is A — a
        ``source_name = B`` filter would drop it and re-charge the duplicate as
        work, re-inflating the very ETA #208 fixes. Keying by ``media_id`` (not
        rel_path) likewise lets every live duplicate PATH of one complete
        media_id count as a free skip.

        A stat-match only means the main loop won't re-hash the file; it still
        runs ``needs_*`` afterward, so the file is free ONLY if it is already
        fully processed under the current config. Each value carries the same
        keys as :meth:`get_processing_status` (``gps_processed`` /
        ``object_detection_done`` / ``face_detection_done`` /
        ``embeddings_version``) PLUS ``has_image_embedding`` and — for videos,
        which are never stamped complete via ``embeddings_version`` the way
        images are — the keyframe/shot signals the runtime video branch skips
        on: ``has_shots`` (a ``shots`` row exists), ``has_keyframes`` (a
        ``video_keyframes`` row exists), and ``has_unembedded_keyframes`` (a
        keyframe lacks its ``keyframe_embedding``). The count phase feeds those
        into the SHARED :func:`msa_indexer.pipeline._video_skip_predicate` — the
        same helper the main loop's video branch uses — so the two can't
        diverge. Tombstoned rows (``deleted``) are excluded: a stat-match on
        deleted content resurrects + reprocesses, so it is not free. Read-only;
        an OperationalError on a pre-M-8 DB is the caller's cue to fall back to
        the full-library estimate.
        """
        out: Dict[str, dict] = {}
        ids = [mid for mid in {m for m in media_ids if m is not None}]
        if not ids:
            return out
        # Chunk to stay under SQLite's default host-parameter limit (999) on a
        # duplicate-heavy source with thousands of distinct media_ids.
        for start in range(0, len(ids), 900):
            chunk = ids[start:start + 900]
            placeholders = ",".join("?" * len(chunk))
            cur = self.conn.execute(
                f"""
                SELECT
                    m.media_id,
                    m.gps_processed,
                    m.object_detection_done,
                    m.face_detection_done,
                    m.embeddings_version,
                    EXISTS(
                        SELECT 1 FROM image_embedding ie WHERE ie.media_id = m.media_id
                    ),
                    EXISTS(
                        SELECT 1 FROM shots s WHERE s.video_id = m.media_id
                    ),
                    EXISTS(
                        SELECT 1 FROM video_keyframes vk WHERE vk.video_id = m.media_id
                    ),
                    EXISTS(
                        SELECT 1 FROM video_keyframes vk2
                        LEFT JOIN keyframe_embedding ke ON ke.keyframe_id = vk2.id
                        WHERE vk2.video_id = m.media_id AND ke.keyframe_id IS NULL
                    )
                FROM media m
                WHERE m.media_id IN ({placeholders}) AND COALESCE(m.deleted, 0) = 0
                """,
                chunk,
            )
            for row in cur.fetchall():
                media_id = row[0]
                if media_id is None:
                    continue
                out[media_id] = {
                    "gps_processed": bool(row[1]),
                    "object_detection_done": bool(row[2]),
                    "face_detection_done": bool(row[3]),
                    "embeddings_version": row[4],
                    "has_image_embedding": bool(row[5]),
                    "has_shots": bool(row[6]),
                    "has_keyframes": bool(row[7]),
                    "has_unembedded_keyframes": bool(row[8]),
                }
        return out

    def upsert_fingerprint(
        self,
        source_name: str,
        rel_path: str,
        size_bytes: int,
        mtime_ns: int,
        media_id: str,
        scan_id: int,
    ) -> None:
        """Insert or update (incl. repoint) a fingerprint row.

        Always stamps last_seen_scan_id and clears missing_since_scan_id —
        an upserted path was, by definition, just seen on disk.
        """
        self.conn.execute(
            """
            INSERT INTO file_fingerprint(
                source_name, rel_path, size_bytes, mtime_ns, media_id,
                last_seen_scan_id, missing_since_scan_id
            ) VALUES (?, ?, ?, ?, ?, ?, NULL)
            ON CONFLICT(source_name, rel_path) DO UPDATE SET
              size_bytes=excluded.size_bytes,
              mtime_ns=excluded.mtime_ns,
              media_id=excluded.media_id,
              last_seen_scan_id=excluded.last_seen_scan_id,
              missing_since_scan_id=NULL
            """,
            (source_name, rel_path, int(size_bytes), int(mtime_ns), media_id, int(scan_id)),
        )
        self._maybe_commit()

    def mark_fingerprints_seen(self, scan_id: int, keys: List[tuple]) -> None:
        """Batch-stamp last_seen_scan_id (and clear grace) for fast-path hits.

        keys: [(source_name, rel_path), ...] — batched in memory by the
        pipeline and flushed with the per-batch commits.
        """
        if not keys:
            return
        self.conn.executemany(
            """
            UPDATE file_fingerprint
            SET last_seen_scan_id = ?, missing_since_scan_id = NULL
            WHERE source_name = ? AND rel_path = ?
            """,
            [(int(scan_id), src, rel) for (src, rel) in keys],
        )
        self._maybe_commit()

    def delete_fingerprint(self, source_name: str, rel_path: str) -> None:
        self.conn.execute(
            "DELETE FROM file_fingerprint WHERE source_name = ? AND rel_path = ?",
            (source_name, rel_path),
        )
        self._maybe_commit()

    def count_fingerprints_for_media(self, media_id: str) -> int:
        row = self.conn.execute(
            "SELECT COUNT(*) FROM file_fingerprint WHERE media_id = ?",
            (media_id,),
        ).fetchone()
        return int(row[0]) if row is not None else 0

    def get_live_fingerprints_for_media(self, media_id: str) -> List[Dict[str, Any]]:
        """All fingerprint rows for a media_id (used for path promotion, R5)."""
        cur = self.conn.execute(
            """
            SELECT source_name, rel_path, size_bytes, mtime_ns
            FROM file_fingerprint WHERE media_id = ?
            ORDER BY source_name, rel_path
            """,
            (media_id,),
        )
        return [
            {
                "source_name": r[0],
                "rel_path": r[1],
                "size_bytes": int(r[2]),
                "mtime_ns": int(r[3]),
            }
            for r in cur.fetchall()
        ]

    def find_media_by_id_any(self, media_id: str) -> Optional[Dict[str, Any]]:
        """Media row lookup that does NOT filter on deleted.

        The fast path needs tombstoned rows too (resurrection check,
        §3.3 step 4).
        """
        row = self.conn.execute(
            """
            SELECT media_id, path, source_name, rel_path, deleted, missing_since_scan_id
            FROM media WHERE media_id = ?
            """,
            (media_id,),
        ).fetchone()
        if row is None:
            return None
        return {
            "media_id": row[0],
            "path": row[1],
            "source_name": row[2],
            "rel_path": row[3],
            "deleted": int(row[4] or 0),
            "missing_since_scan_id": row[5],
        }

    def iter_stale_fingerprints(self, source_name: str, scan_id: int) -> List[Dict[str, Any]]:
        """Fingerprint rows of a source not seen by the given scan (§3.4 sweep)."""
        cur = self.conn.execute(
            """
            SELECT rel_path, media_id, last_seen_scan_id, missing_since_scan_id
            FROM file_fingerprint
            WHERE source_name = ? AND last_seen_scan_id < ?
            ORDER BY rel_path
            """,
            (source_name, int(scan_id)),
        )
        return [
            {
                "source_name": source_name,
                "rel_path": r[0],
                "media_id": r[1],
                "last_seen_scan_id": r[2],
                "missing_since_scan_id": r[3],
            }
            for r in cur.fetchall()
        ]

    def set_fingerprint_missing(self, source_name: str, rel_path: str, scan_id: int) -> None:
        """First-miss grace stamp (no visible effect; §3.4)."""
        self.conn.execute(
            """
            UPDATE file_fingerprint SET missing_since_scan_id = ?
            WHERE source_name = ? AND rel_path = ?
            """,
            (int(scan_id), source_name, rel_path),
        )
        self._maybe_commit()

    def iter_legacy_orphan_media(self, source_name: str) -> List[Dict[str, Any]]:
        """Non-deleted media of a source with zero fingerprint rows.

        These are rows whose files were deleted before the M-8 upgrade —
        the lazy backfill never fingerprints them, so the fingerprint
        sweep never examines them (§3.4 legacy orphan reconcile).
        Self-liquidating after two completed post-upgrade scans.
        """
        cur = self.conn.execute(
            """
            SELECT m.media_id, m.missing_since_scan_id
            FROM media m
            WHERE m.deleted = 0 AND m.source_name = ?
              AND NOT EXISTS (
                SELECT 1 FROM file_fingerprint fp WHERE fp.media_id = m.media_id
              )
            ORDER BY m.media_id
            """,
            (source_name,),
        )
        return [
            {"media_id": r[0], "missing_since_scan_id": r[1]} for r in cur.fetchall()
        ]

    def set_media_missing_since(self, media_id: str, scan_id: int) -> None:
        self.conn.execute(
            "UPDATE media SET missing_since_scan_id = ? WHERE media_id = ?",
            (int(scan_id), media_id),
        )
        self._maybe_commit()

    def tombstone_media(self, media_id: str) -> None:
        """Soft-delete a media row (its last on-disk copy is gone).

        §4.1 tombstone rule: EVERY transition to deleted=1 carries a
        deleted_seq stamp — the sweep, the fast path's content-replaced
        supersede, and the legacy-orphan reconcile all route through this
        one method. A tombstone without its stamp would leave its Qdrant
        points dangling while the watermark advances past them.
        """
        self.conn.execute(
            """
            UPDATE media SET deleted = 1, missing_since_scan_id = NULL, deleted_seq = ?
            WHERE media_id = ?
            """,
            (self._stamp(), media_id),
        )
        self._maybe_commit()

    def resurrect_media(self, media_id: str) -> None:
        """Clear the tombstone and grace state (content reappeared on disk).

        A true resurrection (deleted was 1) also stamps the media's
        image/keyframe/face embedding rows — the reactivation re-upsert of
        plan §3.3 step 4: an earlier export's deletion pass may already have
        removed the points, so the next delta export must send them again.
        deleted_seq is cleared in the same statement so the deletion pass
        can never remove the re-upserted points. A grace-only clear
        (missing_since_scan_id set, deleted still 0) stays pure bookkeeping —
        no stamps, or every grace clear would dirty a no-op run (R1).
        """
        row = self.conn.execute(
            "SELECT deleted FROM media WHERE media_id = ?", (media_id,)
        ).fetchone()
        was_deleted = bool(row is not None and row[0])
        self.conn.execute(
            """
            UPDATE media SET deleted = 0, missing_since_scan_id = NULL, deleted_seq = NULL
            WHERE media_id = ?
            """,
            (media_id,),
        )
        if was_deleted:
            self._stamp_media_payload_rows(media_id)
        self._maybe_commit()

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

    # ── M-8/S-3: export-decision state (dirty trigger, first-delta guard) ──

    def get_s3_state(self) -> Dict[str, Any]:
        """The S-3 columns of index_state (kept out of get_index_state so
        legacy callers stay byte-identical)."""
        row = self.conn.execute(
            """
            SELECT s3_migration_seq, face_recreate_required
            FROM index_state WHERE singleton_id = 1
            """
        ).fetchone()
        if row is None:
            return {"s3_migration_seq": 0, "face_recreate_required": False}
        return {
            "s3_migration_seq": int(row[0] or 0),
            "face_recreate_required": bool(row[1]),
        }

    def clear_face_recreate_required(self) -> None:
        """Called ONLY after a recreate-mode face export succeeded (§4.1)."""
        self.conn.execute(
            "UPDATE index_state SET face_recreate_required = 0 WHERE singleton_id = 1"
        )
        self._maybe_commit()

    def dirty_rows_exist(self, exported_seq: int) -> bool:
        """§4.2 dirty-row trigger: cheap EXISTS probes per stamped table for
        rows above the exported watermark, plus stamped tombstones. Pass -1
        when no export record exists (everything is dirty)."""
        floor = int(exported_seq)
        probes = (
            "SELECT 1 FROM image_embedding WHERE updated_seq > ? LIMIT 1",
            "SELECT 1 FROM keyframe_embedding WHERE updated_seq > ? LIMIT 1",
            "SELECT 1 FROM face_embedding WHERE updated_seq > ? LIMIT 1",
            "SELECT 1 FROM media WHERE deleted = 1 AND deleted_seq IS NOT NULL"
            " AND deleted_seq > ? LIMIT 1",
        )
        for sql in probes:
            if self.conn.execute(sql, (floor,)).fetchone() is not None:
                return True
        return False

    def max_stamped_seq(self) -> int:
        """Highest stamp anywhere (updated_seq tables + tombstones). Drives
        the §4.2 watermark-advance rule for the crashed-bump case."""
        row = self.conn.execute(
            """
            SELECT MAX(s) FROM (
                SELECT COALESCE(MAX(updated_seq), 0) AS s FROM image_embedding
                UNION ALL SELECT COALESCE(MAX(updated_seq), 0) FROM keyframe_embedding
                UNION ALL SELECT COALESCE(MAX(updated_seq), 0) FROM face_embedding
                UNION ALL SELECT COALESCE(MAX(deleted_seq), 0) FROM media WHERE deleted = 1
            )
            """
        ).fetchone()
        return int(row[0] or 0)

    def advance_index_version_to(self, seq: int) -> Dict[str, Any]:
        """Durably complete an interrupted version bump (§4.2 watermark
        rule): stamps found ABOVE index_version_seq mean a run crashed
        between its batch commits and bump_index_version (or an out-of-band
        write allocated ahead). Recording the stale lower seq would repeat
        the recovery export on every subsequent no-op run."""
        next_ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        self.conn.execute(
            """
            UPDATE index_state SET index_version_seq = ?, index_version_ts = ?
            WHERE singleton_id = 1
            """,
            (int(seq), next_ts),
        )
        self._maybe_commit()
        return {"index_version_seq": int(seq), "index_version_ts": next_ts}

    def iter_stamped_tombstones(self, since_seq: Optional[int] = None) -> List[Dict[str, Any]]:
        """Tombstoned media whose Qdrant points the §4.2 deletion pass must
        remove: deleted = 1 AND deleted_seq > since_seq (None → every
        stamped tombstone — the full-export case). Keyframe triples and
        face ids survive soft-delete, so every point id is computable."""
        floor = -1 if since_seq is None else int(since_seq)
        cur = self.conn.execute(
            """
            SELECT media_id, deleted_seq FROM media
            WHERE deleted = 1 AND deleted_seq IS NOT NULL AND deleted_seq > ?
            ORDER BY media_id
            """,
            (floor,),
        )
        out: List[Dict[str, Any]] = []
        for media_id, dseq in cur.fetchall():
            keyframes = [
                (int(r[0]), int(r[1]))
                for r in self.conn.execute(
                    "SELECT shot_index, kf_index FROM video_keyframes WHERE video_id = ?",
                    (media_id,),
                ).fetchall()
            ]
            face_ids = [
                r[0]
                for r in self.conn.execute(
                    "SELECT face_id FROM face WHERE media_id = ?", (media_id,)
                ).fetchall()
            ]
            out.append(
                {
                    "media_id": media_id,
                    "deleted_seq": int(dseq),
                    "keyframes": keyframes,
                    "face_ids": face_ids,
                }
            )
        return out

    # --- Helpers expected by qdrant_export.py ---
    def iter_items(self, since_seq: Optional[int] = None):
        """Yield dicts for each image media item (videos excluded).

        Videos are handled separately via iter_video_keyframes(). The
        SQL-level ``mime LIKE 'image/%'`` filter is the primary guard;
        the path-extension fallback below catches legacy rows where
        ``mime`` was never populated.

        since_seq (M-8/S-3 §4.2): when set, delta mode — only media whose
        image_embedding row is stamped ABOVE the exported watermark
        (``updated_seq > since_seq``; the covering index makes this cheap).
        None = full export, unchanged behavior.
        """
        VIDEO_EXTENSIONS = ('.mp4', '.mov', '.avi', '.mkv', '.m4v', '.wmv', '.flv', '.webm')
        if since_seq is None:
            cur = self.conn.execute(
                """
                SELECT media_id, path, source_name, rel_path, place, ts_utc, added_at
                FROM media
                WHERE deleted = 0 AND (mime LIKE 'image/%' OR mime IS NULL)
                """
            )
        else:
            cur = self.conn.execute(
                """
                SELECT m.media_id, m.path, m.source_name, m.rel_path, m.place, m.ts_utc, m.added_at
                FROM media m
                JOIN image_embedding ie ON ie.media_id = m.media_id
                    AND ie.updated_seq > ?
                WHERE m.deleted = 0 AND (m.mime LIKE 'image/%' OR m.mime IS NULL)
                """,
                (int(since_seq),),
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

        §4.1 (M-8/S-3): the cascade removes the ``face_embedding`` rows,
        so nothing stamped survives to drive Qdrant point deletion while
        the media itself stays live. When rows were actually deleted, set
        the durable ``face_recreate_required`` marker in the SAME
        transaction — the next face export runs in recreate mode
        regardless of that run's flags, and the marker is cleared only
        after that recreate export succeeds (crash-safe). The media's
        keyframe rows are stamped too: video payloads embed people names,
        which just changed.
        """
        cur = self.conn.execute("DELETE FROM face WHERE media_id = ?", (media_id,))
        deleted = int(cur.rowcount or 0)
        if deleted:
            self.conn.execute(
                "UPDATE index_state SET face_recreate_required = 1 WHERE singleton_id = 1"
            )
            self.conn.execute(
                """
                UPDATE keyframe_embedding SET updated_seq = ?
                WHERE keyframe_id IN (SELECT id FROM video_keyframes WHERE video_id = ?)
                """,
                (self._stamp(), media_id),
            )
        self._maybe_commit()
        return deleted

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
        # §4.1 (keyframe_rows rule): the conflict arm refreshes payload
        # cells (tags/gps/place) of existing keyframes — stamp the video's
        # keyframe embedding rows. Fresh keyframes have no embedding row
        # yet (no-op); their upsert stamps itself.
        self.conn.execute(
            """
            UPDATE keyframe_embedding SET updated_seq = ?
            WHERE keyframe_id IN (SELECT id FROM video_keyframes WHERE video_id = ?)
            """,
            (self._stamp(), video_id),
        )
        self._maybe_commit()

    def iter_video_keyframes(self, since_seq: Optional[int] = None):
        """
        Yield keyframe rows joined with media path and tags for export.
        Returns dicts with: video_id, path, shot_index, kf_index, timestamp, shot_start, shot_end, tags, place, people

        since_seq (M-8/S-3 §4.2): when set, delta mode — only keyframes
        whose embedding row is stamped above the watermark. None = full.
        """
        import json
        if since_seq is None:
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
        else:
            cur = self.conn.execute(
                """
                SELECT vk.video_id, m.path, m.source_name, m.rel_path, m.place,
                       vk.shot_index, vk.kf_index, vk.timestamp, vk.shot_start, vk.shot_end, vk.tags,
                       vk.gps_lat, vk.gps_lon, vk.gps_alt, vk.gps_datetime_utc, vk.gps_fix, vk.gps_source, vk.place
                FROM video_keyframes vk
                JOIN media m ON m.media_id = vk.video_id
                JOIN keyframe_embedding ke ON ke.keyframe_id = vk.id
                    AND ke.updated_seq > ?
                WHERE m.deleted = 0
                ORDER BY vk.video_id, vk.shot_index, vk.kf_index
                """,
                (int(since_seq),),
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
        # §4.1 (face_rows rule): the conflict arm refreshes payload cells
        # (confidence/person_id/gender/age) of existing faces — stamp the
        # media's face embedding rows, plus its keyframe rows because video
        # payloads embed people names. Fresh faces' embedding rows don't
        # exist yet (no-op); their upsert stamps itself.
        seq = self._stamp()
        self.conn.execute(
            """
            UPDATE face_embedding SET updated_seq = ?
            WHERE face_id IN (SELECT face_id FROM face WHERE media_id = ?)
            """,
            (seq, media_id),
        )
        self.conn.execute(
            """
            UPDATE keyframe_embedding SET updated_seq = ?
            WHERE keyframe_id IN (SELECT id FROM video_keyframes WHERE video_id = ?)
            """,
            (seq, media_id),
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

    def iter_faces(self, order_by='default', since_seq: Optional[int] = None):
        """
        Yield all face detections with associated media information for export to vector DB.

        Args:
            order_by: Ordering method - 'default' (media_id, face_id),
                     'labeled_first' (labeled faces first, then unknown)
            since_seq: (M-8/S-3 §4.2) when set, delta mode — only faces
                     whose embedding row is stamped above the watermark.
                     None = full.

        Returns dicts with: face_id, media_id, path, bbox, confidence, person_id, person_name, etc.
        """
        # Choose ORDER BY clause based on order_by parameter
        if order_by == 'labeled_first':
            # Sort by: labeled faces first (person_id IS NOT NULL), then by person_name, then face_id
            order_clause = "ORDER BY (f.person_id IS NULL), p.name, f.face_id"
        else:
            # Default ordering
            order_clause = "ORDER BY f.media_id, f.face_id"

        delta_join = ""
        params: tuple = ()
        if since_seq is not None:
            delta_join = (
                "JOIN face_embedding fe ON fe.face_id = f.face_id"
                " AND fe.updated_seq > ?"
            )
            params = (int(since_seq),)

        cur = self.conn.execute(
            f"""
            SELECT f.face_id, f.media_id, m.path, m.source_name, m.rel_path, m.ts_utc,
                   f.x, f.y, f.w, f.h, f.confidence,
                   f.person_id, p.name as person_name,
                   f.gender, f.age, f.shot_index, f.kf_index
            FROM face f
            JOIN media m ON f.media_id = m.media_id
            {delta_join}
            LEFT JOIN person p ON f.person_id = p.person_id
            WHERE m.deleted = 0
            {order_clause}
            """,
            params,
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


    # NOTE (M-8/S-2 §4 rollback semantics): the labeling/person mutation
    # methods below commit via _maybe_commit(), not commit(), so a caller
    # constructing SQLiteStore(path, autocommit=False) — the API's Qdrant
    # payload-write guard — can defer the commit until the Qdrant sync
    # outcome is known and ROLL BACK when the shared client was closed
    # mid-sync (drain-ceiling residual). Every existing caller uses the
    # default autocommit=True and is byte-identical in behavior.

    def update_face_person(self, face_id: str, person_id: str):
        """Update the person_id for a face (used in labeling/clustering).

        §4.1 (face_label rule): stamps the face's embedding row + the
        owning video's keyframe rows. The live qdrant_sync payload patch
        stays the immediate path; the stamp guarantees convergence via the
        next delta export if the patch was skipped or failed (#204).
        """
        self.conn.execute(
            "UPDATE face SET person_id=? WHERE face_id=?",
            (person_id, face_id),
        )
        self._stamp_face_rows([face_id])
        self._maybe_commit()

    def update_faces_person_batch(self, face_ids: list[str], person_id: str) -> int:
        """Label multiple faces with the same person in one transaction. Returns count updated.

        §4.1 (face_label rule): stamps every labeled face's embedding row +
        the owning videos' keyframe rows.
        """
        unique_ids = list(dict.fromkeys(face_ids))  # deduplicate, preserve order
        before = self.conn.total_changes
        self.conn.executemany(
            "UPDATE face SET person_id=? WHERE face_id=?",
            [(person_id, fid) for fid in unique_ids],
        )
        changed = self.conn.total_changes - before
        self._stamp_face_rows(unique_ids)
        self._maybe_commit()
        return changed

    def clear_face_person(self, face_id: str):
        """Remove person assignment from a face (set person_id=NULL).

        §4.1 (face_label rule): stamps like update_face_person.
        """
        self.conn.execute(
            "UPDATE face SET person_id=NULL WHERE face_id=?",
            (face_id,),
        )
        self._stamp_face_rows([face_id])
        self._maybe_commit()

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
            self._maybe_commit()
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
        """Rename a person to a unique new name. Raises IntegrityError on conflict.

        §4.1 (person_rows rule): face payloads carry person_name and
        keyframe payloads carry people names, so EVERY face of the person
        (plus affected videos' keyframe rows) is stamped — a rename whose
        live payload patch failed would otherwise stay stale under delta
        export forever.
        """
        try:
            self.conn.execute(
                "UPDATE person SET name=? WHERE person_id=?",
                (new_name, person_id),
            )
            self._stamp_person_rows(person_id)
            self._maybe_commit()
        except sqlite3.IntegrityError:
            raise

    def merge_people(self, source_id: str, target_id: str) -> int:
        """Merge source person into target person by reassigning faces and deleting source.

        Returns the number of faces reassigned.

        §4.1 (person_rows rule): after the reassignment every former-source
        face carries the target's name — stamp all of the target's faces
        (superset: includes the reassigned ones) + affected keyframe rows.
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
        self._stamp_person_rows(target_id)
        self._maybe_commit()
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
