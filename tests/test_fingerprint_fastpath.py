"""M-8/S-1 — fingerprint fast-path, move handling, deletion sweep.

Covers the §6.1 S-1 test list of
internal/docs/indexer/M8_INCREMENTAL_INDEXING_PLAN.md:

- store-level scan_run / file_fingerprint helpers (§3.1)
- pre-M-8 DB migration + lazy backfill expectation (R10)
- §3.1a path-authority rebuild (constraint gone, rows/FKs preserved,
  backup taken — R4)
- pipeline fast path: hit / stat-mismatch / supersede / move / copy /
  resurrect (§3.3)
- deletion sweep gates + grace + promotion + legacy orphan reconcile (§3.4,
  R3/R5)
- the R1 no-op regression (no index_version_seq bump, no export — §3.5)

Store-level tests use a real SQLite DB in tmp_path. Pipeline-level tests
follow the tests/test_pipeline_batch_commit.py pattern (monkeypatched
sha256_of_file / ClipEmbedder / _do_qdrant_export, SimpleNamespace config,
real tmp media files) but with a REAL SQLiteStore underneath.
"""
from __future__ import annotations

import os
import sqlite3
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest
from PIL import Image

from msa_indexer.db.sqlite_store import (
    SQLiteStore,
    media_path_rebuild_pending,
)

SCHEMA_PATH = (
    Path(__file__).parent.parent / "src" / "msa_indexer" / "db" / "schema.sql"
)


# ---------------------------------------------------------------------------
# Frozen pre-M-8 schema (R10 fixture)
#
# A verbatim snapshot of schema.sql as it stood before M-8/S-1: media.path
# still carries UNIQUE, there is no missing_since_scan_id column, and the
# scan_run / file_fingerprint tables do not exist. Used to build a
# pre-upgrade DB in-test and assert the migration path.
# ---------------------------------------------------------------------------

_PRE_M8_SCHEMA = """
PRAGMA journal_mode=WAL;
CREATE TABLE IF NOT EXISTS media (
  media_id TEXT PRIMARY KEY,
  path TEXT NOT NULL UNIQUE,
  source_name TEXT,
  rel_path TEXT,
  size_bytes INTEGER,
  mime TEXT,
  ts_utc TEXT,
  gps_lat REAL, gps_lon REAL,
  place TEXT,
  camera TEXT, lens TEXT,
  width INTEGER, height INTEGER, duration REAL,
  hash_blake3 TEXT,
  added_at TEXT DEFAULT (datetime('now')),
  model_version TEXT DEFAULT 'clip-0.1',
  deleted INTEGER DEFAULT 0,
  face_detection_done INTEGER DEFAULT 0,
  object_detection_done INTEGER DEFAULT 0,
  gps_processed INTEGER DEFAULT 0,
  gps_data_mode TEXT,
  embeddings_version TEXT
);
CREATE INDEX IF NOT EXISTS idx_media_source_rel ON media(source_name, rel_path);
CREATE INDEX IF NOT EXISTS idx_media_ts ON media(ts_utc);
CREATE TABLE IF NOT EXISTS tag (
  tag_id INTEGER PRIMARY KEY AUTOINCREMENT,
  name TEXT UNIQUE
);
CREATE TABLE IF NOT EXISTS media_tag (
  media_id TEXT,
  tag_id INTEGER,
  PRIMARY KEY (media_id, tag_id),
  FOREIGN KEY (media_id) REFERENCES media(media_id),
  FOREIGN KEY (tag_id) REFERENCES tag(tag_id)
);
CREATE TABLE IF NOT EXISTS person (
  person_id TEXT PRIMARY KEY,
  name TEXT UNIQUE,
  is_labeled INTEGER DEFAULT 0,
  cluster_id INTEGER,
  representative_face_id TEXT,
  face_count INTEGER DEFAULT 0,
  created_at TEXT DEFAULT (datetime('now'))
);
CREATE TABLE IF NOT EXISTS face (
  face_id TEXT PRIMARY KEY,
  media_id TEXT NOT NULL,
  x REAL, y REAL, w REAL, h REAL,
  confidence REAL,
  person_id TEXT,
  gender TEXT,
  age INTEGER,
  shot_index INTEGER,
  kf_index INTEGER,
  created_at TEXT DEFAULT (datetime('now')),
  FOREIGN KEY (media_id) REFERENCES media(media_id),
  FOREIGN KEY (person_id) REFERENCES person(person_id)
);
CREATE INDEX IF NOT EXISTS idx_face_person ON face(person_id);
CREATE INDEX IF NOT EXISTS idx_face_media ON face(media_id);
CREATE TABLE IF NOT EXISTS image_embedding (
  media_id        TEXT PRIMARY KEY,
  embedding       BLOB NOT NULL,
  embedding_dim   INTEGER NOT NULL,
  embedding_model TEXT NOT NULL,
  created_at      TEXT DEFAULT (datetime('now')),
  FOREIGN KEY (media_id) REFERENCES media(media_id) ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS face_embedding (
  face_id         TEXT PRIMARY KEY,
  embedding       BLOB NOT NULL,
  embedding_dim   INTEGER NOT NULL,
  embedding_model TEXT NOT NULL,
  created_at      TEXT DEFAULT (datetime('now')),
  FOREIGN KEY (face_id) REFERENCES face(face_id) ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS keyframe_embedding (
  keyframe_id     INTEGER PRIMARY KEY,
  embedding       BLOB NOT NULL,
  embedding_dim   INTEGER NOT NULL,
  embedding_model TEXT NOT NULL,
  created_at      TEXT DEFAULT (datetime('now')),
  FOREIGN KEY (keyframe_id) REFERENCES video_keyframes(id) ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS shots (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  video_id TEXT NOT NULL,
  shot_index INTEGER NOT NULL,
  t_start REAL NOT NULL,
  t_end REAL NOT NULL,
  duration REAL GENERATED ALWAYS AS (t_end - t_start) VIRTUAL,
  keyframe_count INTEGER DEFAULT 1,
  is_synthetic INTEGER DEFAULT 0,
  FOREIGN KEY (video_id) REFERENCES media(media_id)
);
CREATE INDEX IF NOT EXISTS idx_shots_video ON shots(video_id);
CREATE TABLE IF NOT EXISTS video_keyframes (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  video_id TEXT NOT NULL,
  shot_index INTEGER NOT NULL,
  kf_index INTEGER NOT NULL,
  timestamp REAL NOT NULL,
  shot_start REAL NOT NULL,
  shot_end REAL NOT NULL,
  tags TEXT,
  gps_lat REAL,
  gps_lon REAL,
  gps_alt REAL,
  gps_datetime_utc TEXT,
  gps_fix INTEGER,
  gps_source TEXT,
  place TEXT,
  UNIQUE (video_id, shot_index, kf_index),
  FOREIGN KEY (video_id) REFERENCES media(media_id)
);
CREATE INDEX IF NOT EXISTS idx_vkf_video ON video_keyframes(video_id);
CREATE TABLE IF NOT EXISTS index_state (
  singleton_id INTEGER PRIMARY KEY CHECK (singleton_id = 1),
  index_version_seq INTEGER NOT NULL DEFAULT 0,
  index_version_ts TEXT
);
"""


def _build_pre_m8_db(db_path: Path) -> None:
    """Create a populated pre-M-8 database from the frozen DDL string."""
    conn = sqlite3.connect(str(db_path))
    try:
        conn.executescript(_PRE_M8_SCHEMA)
        conn.execute(
            "INSERT INTO media(media_id, path, source_name, rel_path, size_bytes, mime, deleted)"
            " VALUES ('m-live', '/photos/a.jpg', 'photos', 'a.jpg', 10, 'image/jpg', 0)"
        )
        conn.execute(
            "INSERT INTO media(media_id, path, source_name, rel_path, size_bytes, mime, deleted)"
            " VALUES ('m-gone', '/photos/gone.jpg', 'photos', 'gone.jpg', 11, 'image/jpg', 0)"
        )
        conn.execute(
            "INSERT INTO image_embedding(media_id, embedding, embedding_dim, embedding_model)"
            " VALUES ('m-live', x'00000000', 1, 'clip-test')"
        )
        conn.execute(
            "INSERT INTO face(face_id, media_id, x, y, w, h, confidence, person_id)"
            " VALUES ('m-live:f0', 'm-live', 0.1, 0.1, 0.2, 0.2, 0.99, NULL)"
        )
        conn.execute(
            "INSERT INTO face_embedding(face_id, embedding, embedding_dim, embedding_model)"
            " VALUES ('m-live:f0', x'00000000', 1, 'face-test')"
        )
        conn.commit()
    finally:
        conn.close()


@pytest.fixture
def store(tmp_path: Path):
    db = SQLiteStore(tmp_path / "media.sqlite", autocommit=True)
    db.init_schema(SCHEMA_PATH)
    yield db
    db.close()


def _insert_media(db: SQLiteStore, media_id: str, path: str, source: str, rel: str, deleted: int = 0):
    db.upsert_media(
        {
            "media_id": media_id,
            "path": path,
            "source_name": source,
            "rel_path": rel,
            "size_bytes": 1,
            "mime": "image/jpg",
            "deleted": deleted,
        }
    )


# ---------------------------------------------------------------------------
# Store-level: scan_run
# ---------------------------------------------------------------------------


class TestScanRun:
    def test_begin_returns_monotonic_ids(self, store):
        first = store.begin_scan_run()
        second = store.begin_scan_run()
        assert second > first

    def test_complete_writes_completed_at_and_sources_json(self, store):
        scan_id = store.begin_scan_run()
        store.complete_scan_run(scan_id, '{"photos": {"walked_to_completion": true, "walk_errors": 0}}')
        row = store.conn.execute(
            "SELECT completed_at, sources_json FROM scan_run WHERE scan_id = ?",
            (scan_id,),
        ).fetchone()
        assert row[0] is not None
        assert "walked_to_completion" in row[1]

    def test_incomplete_scan_has_null_completed_at(self, store):
        scan_id = store.begin_scan_run()
        row = store.conn.execute(
            "SELECT completed_at FROM scan_run WHERE scan_id = ?", (scan_id,)
        ).fetchone()
        assert row[0] is None


# ---------------------------------------------------------------------------
# Store-level: file_fingerprint helpers
# ---------------------------------------------------------------------------


class TestFingerprintHelpers:
    def test_upsert_and_get_roundtrip(self, store):
        _insert_media(store, "m1", "/p/a.jpg", "photos", "a.jpg")
        scan_id = store.begin_scan_run()
        store.upsert_fingerprint("photos", "a.jpg", 100, 12345, "m1", scan_id)
        fp = store.get_fingerprint("photos", "a.jpg")
        assert fp == {
            "source_name": "photos",
            "rel_path": "a.jpg",
            "size_bytes": 100,
            "mtime_ns": 12345,
            "media_id": "m1",
            "last_seen_scan_id": scan_id,
            "missing_since_scan_id": None,
        }

    def test_get_missing_returns_none(self, store):
        assert store.get_fingerprint("photos", "nope.jpg") is None

    def test_upsert_repoints_media_id_and_clears_grace(self, store):
        _insert_media(store, "m1", "/p/a.jpg", "photos", "a.jpg")
        _insert_media(store, "m2", "/p/a2.jpg", "photos", "a2.jpg")
        s1 = store.begin_scan_run()
        store.upsert_fingerprint("photos", "a.jpg", 100, 1, "m1", s1)
        store.set_fingerprint_missing("photos", "a.jpg", s1)
        s2 = store.begin_scan_run()
        store.upsert_fingerprint("photos", "a.jpg", 200, 2, "m2", s2)
        fp = store.get_fingerprint("photos", "a.jpg")
        assert fp["media_id"] == "m2"
        assert fp["size_bytes"] == 200
        assert fp["last_seen_scan_id"] == s2
        assert fp["missing_since_scan_id"] is None

    def test_mark_fingerprints_seen_batch_clears_grace(self, store):
        _insert_media(store, "m1", "/p/a.jpg", "photos", "a.jpg")
        _insert_media(store, "m2", "/p/b.jpg", "photos", "b.jpg")
        s1 = store.begin_scan_run()
        store.upsert_fingerprint("photos", "a.jpg", 1, 1, "m1", s1)
        store.upsert_fingerprint("photos", "b.jpg", 2, 2, "m2", s1)
        store.set_fingerprint_missing("photos", "a.jpg", s1)
        s2 = store.begin_scan_run()
        store.mark_fingerprints_seen(s2, [("photos", "a.jpg"), ("photos", "b.jpg")])
        for rel in ("a.jpg", "b.jpg"):
            fp = store.get_fingerprint("photos", rel)
            assert fp["last_seen_scan_id"] == s2
            assert fp["missing_since_scan_id"] is None

    def test_stale_query_returns_only_unseen_rows_of_source(self, store):
        _insert_media(store, "m1", "/p/a.jpg", "photos", "a.jpg")
        _insert_media(store, "m2", "/p/b.jpg", "photos", "b.jpg")
        _insert_media(store, "m3", "/q/c.jpg", "other", "c.jpg")
        s1 = store.begin_scan_run()
        store.upsert_fingerprint("photos", "a.jpg", 1, 1, "m1", s1)
        store.upsert_fingerprint("photos", "b.jpg", 2, 2, "m2", s1)
        store.upsert_fingerprint("other", "c.jpg", 3, 3, "m3", s1)
        s2 = store.begin_scan_run()
        store.mark_fingerprints_seen(s2, [("photos", "a.jpg")])
        stale = store.iter_stale_fingerprints("photos", s2)
        assert [r["rel_path"] for r in stale] == ["b.jpg"]

    def test_delete_and_count_for_media(self, store):
        _insert_media(store, "m1", "/p/a.jpg", "photos", "a.jpg")
        s1 = store.begin_scan_run()
        store.upsert_fingerprint("photos", "a.jpg", 1, 1, "m1", s1)
        store.upsert_fingerprint("photos", "copy/a.jpg", 1, 1, "m1", s1)
        assert store.count_fingerprints_for_media("m1") == 2
        store.delete_fingerprint("photos", "a.jpg")
        assert store.count_fingerprints_for_media("m1") == 1
        rows = store.get_live_fingerprints_for_media("m1")
        assert [r["rel_path"] for r in rows] == ["copy/a.jpg"]

    def test_fingerprint_cascades_on_media_delete(self, store):
        _insert_media(store, "m1", "/p/a.jpg", "photos", "a.jpg")
        s1 = store.begin_scan_run()
        store.upsert_fingerprint("photos", "a.jpg", 1, 1, "m1", s1)
        store.conn.execute("DELETE FROM media WHERE media_id = 'm1'")
        store.commit()
        assert store.count_fingerprints_for_media("m1") == 0

    def test_find_media_by_id_any_includes_tombstoned(self, store):
        _insert_media(store, "m1", "/p/a.jpg", "photos", "a.jpg", deleted=1)
        row = store.find_media_by_id_any("m1")
        assert row is not None
        assert row["deleted"] == 1
        assert row["rel_path"] == "a.jpg"
        assert store.find_media_by_id_any("nope") is None

    def test_tombstone_and_resurrect_media(self, store):
        _insert_media(store, "m1", "/p/a.jpg", "photos", "a.jpg")
        store.set_media_missing_since("m1", 7)
        assert store.find_media_by_id_any("m1")["missing_since_scan_id"] == 7
        store.tombstone_media("m1")
        row = store.find_media_by_id_any("m1")
        assert row["deleted"] == 1
        assert row["missing_since_scan_id"] is None
        store.resurrect_media("m1")
        row = store.find_media_by_id_any("m1")
        assert row["deleted"] == 0
        assert row["missing_since_scan_id"] is None

    def test_legacy_orphan_query_finds_media_without_fingerprints(self, store):
        _insert_media(store, "m1", "/p/a.jpg", "photos", "a.jpg")
        _insert_media(store, "m2", "/p/b.jpg", "photos", "b.jpg")
        _insert_media(store, "m3", "/p/c.jpg", "photos", "c.jpg", deleted=1)
        s1 = store.begin_scan_run()
        store.upsert_fingerprint("photos", "a.jpg", 1, 1, "m1", s1)
        orphans = store.iter_legacy_orphan_media("photos")
        # m1 has a fingerprint, m3 is already tombstoned — only m2 qualifies.
        assert [o["media_id"] for o in orphans] == ["m2"]


# ---------------------------------------------------------------------------
# R10: pre-M-8 DB migrates cleanly; fingerprint table starts empty
# ---------------------------------------------------------------------------


class TestPreM8Migration:
    def test_pre_m8_db_migrates_and_fingerprint_table_is_empty(self, tmp_path):
        db_path = tmp_path / "media.sqlite"
        _build_pre_m8_db(db_path)

        db = SQLiteStore(db_path, autocommit=True)
        try:
            db.init_schema(SCHEMA_PATH)

            # New tables exist.
            names = {
                r[0]
                for r in db.conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                ).fetchall()
            }
            assert {"scan_run", "file_fingerprint"} <= names

            # Lazy backfill expectation: no fingerprint rows after migration —
            # the first post-upgrade run hashes everything and populates them.
            count = db.conn.execute(
                "SELECT COUNT(*) FROM file_fingerprint"
            ).fetchone()[0]
            assert count == 0

            # media gained missing_since_scan_id.
            cols = [c[1] for c in db.conn.execute("PRAGMA table_info(media)").fetchall()]
            assert "missing_since_scan_id" in cols

            # Pre-existing rows and FK children survived.
            assert db.conn.execute("SELECT COUNT(*) FROM media").fetchone()[0] == 2
            assert db.conn.execute(
                "SELECT COUNT(*) FROM face_embedding"
            ).fetchone()[0] == 1
            assert db.conn.execute("PRAGMA foreign_key_check").fetchall() == []
        finally:
            db.close()

    def test_migration_is_idempotent(self, tmp_path):
        db_path = tmp_path / "media.sqlite"
        _build_pre_m8_db(db_path)
        for _ in range(2):
            db = SQLiteStore(db_path, autocommit=True)
            try:
                db.init_schema(SCHEMA_PATH)
            finally:
                db.close()
        conn = sqlite3.connect(str(db_path))
        try:
            assert conn.execute("SELECT COUNT(*) FROM media").fetchone()[0] == 2
        finally:
            conn.close()


# ---------------------------------------------------------------------------
# §3.1a path-authority rebuild (R4): drop UNIQUE(media.path)
# ---------------------------------------------------------------------------


def _media_has_unique_path(conn: sqlite3.Connection) -> bool:
    for row in conn.execute("PRAGMA index_list(media)").fetchall():
        name, unique, origin = row[1], row[2], row[3]
        if unique and origin == "u":
            cols = [r[2] for r in conn.execute(f'PRAGMA index_info("{name}")').fetchall()]
            if cols == ["path"]:
                return True
    return False


class TestPathAuthorityRebuild:
    def test_rebuild_drops_unique_and_preserves_rows_and_fks(self, tmp_path):
        db_path = tmp_path / "media.sqlite"
        _build_pre_m8_db(db_path)
        assert media_path_rebuild_pending(db_path) is True

        db = SQLiteStore(db_path, autocommit=True)
        try:
            db.init_schema(SCHEMA_PATH)
            assert not _media_has_unique_path(db.conn)
            # replacement path index exists
            idx_names = [r[1] for r in db.conn.execute("PRAGMA index_list(media)").fetchall()]
            assert "idx_media_path" in idx_names
            assert "idx_media_source_rel" in idx_names
            assert "idx_media_ts" in idx_names
            # rows + children preserved
            assert db.conn.execute("SELECT COUNT(*) FROM media").fetchone()[0] == 2
            assert db.conn.execute("SELECT COUNT(*) FROM image_embedding").fetchone()[0] == 1
            assert db.conn.execute("SELECT COUNT(*) FROM face_embedding").fetchone()[0] == 1
            assert db.conn.execute("PRAGMA foreign_key_check").fetchall() == []
            # FK enforcement is back on after the rebuild
            assert db.conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        finally:
            db.close()
        assert media_path_rebuild_pending(db_path) is False

    def test_duplicate_path_tombstoned_plus_live_both_readable(self, tmp_path):
        db_path = tmp_path / "media.sqlite"
        _build_pre_m8_db(db_path)
        db = SQLiteStore(db_path, autocommit=True)
        try:
            db.init_schema(SCHEMA_PATH)
            # tombstone the existing row for /photos/a.jpg, insert a live
            # successor at the SAME path (content replaced in place)
            db.tombstone_media("m-live")
            _insert_media(db, "m-successor", "/photos/a.jpg", "photos", "a.jpg")
            rows = db.conn.execute(
                "SELECT media_id, deleted FROM media WHERE path='/photos/a.jpg' ORDER BY media_id"
            ).fetchall()
            assert rows == [("m-live", 1), ("m-successor", 0)]
            live = db.conn.execute(
                "SELECT media_id FROM media WHERE path='/photos/a.jpg' AND deleted=0"
            ).fetchall()
            assert live == [("m-successor",)]
        finally:
            db.close()

    def test_fresh_schema_has_no_unique_and_no_rebuild_pending(self, store, tmp_path):
        assert not _media_has_unique_path(store.conn)
        assert media_path_rebuild_pending(store.path) is False

    def test_init_schema_no_migrations_does_not_rebuild(self, tmp_path):
        db_path = tmp_path / "media.sqlite"
        _build_pre_m8_db(db_path)
        db = SQLiteStore(db_path, autocommit=True)
        try:
            db.init_schema_no_migrations(SCHEMA_PATH)
            # non-mutating entry point: constraint still present
            assert _media_has_unique_path(db.conn)
        finally:
            db.close()
        assert media_path_rebuild_pending(db_path) is True

    def test_run_index_takes_backup_before_rebuild(self, tmp_path, monkeypatch):
        """Pipeline-level: a pre-M-8 DB triggers the R4-pattern backup in
        run_index before init_schema performs the rebuild."""
        from msa_indexer import pipeline

        db_path = tmp_path / "media.sqlite"
        _build_pre_m8_db(db_path)

        media_dir = tmp_path / "photos"
        media_dir.mkdir()

        monkeypatch.setattr(pipeline, "resolve_for_access", lambda p: p)
        monkeypatch.setattr(pipeline, "ClipEmbedder", _FakeClipEmbedder)
        monkeypatch.setattr(pipeline, "_load_historical_perf", lambda _p: (1.0, 30.0, 5.0))
        monkeypatch.setattr(pipeline, "_emit_indexer_summary", lambda **_p: None)
        monkeypatch.setattr(pipeline, "get_qdrant_export_version", lambda: None)
        monkeypatch.setattr(pipeline, "record_qdrant_export_version", lambda *_a, **_k: None)
        monkeypatch.setattr(pipeline, "_do_qdrant_export", lambda *_a, **_k: False)

        config = _pipeline_config(tmp_path, media_dir, sqlite_path=db_path)
        pipeline.run_index(config)

        backups = list(tmp_path.glob("media.backup.*.sqlite"))
        assert len(backups) == 1, "expected exactly one pre-migration backup"
        # backup still carries the OLD schema (taken before the rebuild)
        bconn = sqlite3.connect(str(backups[0]))
        try:
            assert _media_has_unique_path(bconn)
            assert bconn.execute("SELECT COUNT(*) FROM media").fetchone()[0] == 2
        finally:
            bconn.close()
        # live DB was rebuilt
        assert media_path_rebuild_pending(db_path) is False

    def test_second_run_takes_no_further_backup(self, tmp_path, monkeypatch):
        from msa_indexer import pipeline

        db_path = tmp_path / "media.sqlite"
        _build_pre_m8_db(db_path)
        media_dir = tmp_path / "photos"
        media_dir.mkdir()

        monkeypatch.setattr(pipeline, "resolve_for_access", lambda p: p)
        monkeypatch.setattr(pipeline, "ClipEmbedder", _FakeClipEmbedder)
        monkeypatch.setattr(pipeline, "_load_historical_perf", lambda _p: (1.0, 30.0, 5.0))
        monkeypatch.setattr(pipeline, "_emit_indexer_summary", lambda **_p: None)
        monkeypatch.setattr(pipeline, "get_qdrant_export_version", lambda: None)
        monkeypatch.setattr(pipeline, "record_qdrant_export_version", lambda *_a, **_k: None)
        monkeypatch.setattr(pipeline, "_do_qdrant_export", lambda *_a, **_k: False)

        config = _pipeline_config(tmp_path, media_dir, sqlite_path=db_path)
        pipeline.run_index(config)
        pipeline.run_index(config)

        backups = list(tmp_path.glob("media.backup.*.sqlite"))
        assert len(backups) == 1, "second run must not take another backup"


# ---------------------------------------------------------------------------
# Pipeline harness (shared by §3.1a / §3.3 / §3.4 / R1 tests)
#
# Same shape as tests/test_pipeline_batch_commit.py — monkeypatched
# embedder/export, SimpleNamespace config, real tmp media files — but with a
# REAL SQLiteStore on a tmp DB underneath.
# ---------------------------------------------------------------------------


class _FakeClipEmbedder:
    dim = 8
    calls = 0  # class-level: counts image_embed invocations across instances

    def __init__(self, *_args, **_kwargs):
        pass

    def image_embed(self, images):
        type(self).calls += 1
        return [np.zeros(self.dim, dtype=np.float32) for _ in images]


@pytest.fixture(autouse=True)
def _reset_embedder_calls():
    _FakeClipEmbedder.calls = 0
    yield
    _FakeClipEmbedder.calls = 0


def _pipeline_config(tmp_path: Path, media_dir: Path, sqlite_path=None, **overrides):
    cfg = SimpleNamespace(
        sqlite_path=str(sqlite_path or (tmp_path / "media.sqlite")),
        thumb_dir=tmp_path / "thumbs",
        face_thumb_dir=tmp_path / "face-thumbs",
        log_dir=tmp_path / "logs",
        model_name="ViT-L-14",
        pretrained="openai",
        model_version="test-v1",
        device="cpu",
        enable_object_detection=False,
        enable_face_recognition=False,
        media_sources=[SimpleNamespace(name="photos", path=str(media_dir), enabled=True)],
    )
    for k, v in overrides.items():
        setattr(cfg, k, v)
    return cfg


class _FakeQdrant:
    """Records exports and mirrors the recorded version like real Qdrant."""

    def __init__(self):
        self.export_calls = 0
        self.recorded: dict | None = None

    def do_export(self, *_args, **_kwargs):
        self.export_calls += 1
        return True

    def record_version(self, seq, ts):
        self.recorded = {"index_version_seq": seq, "index_version_ts": ts}

    def get_version(self):
        return dict(self.recorded) if self.recorded else None


def _patch_pipeline_common(monkeypatch, pipeline, qdrant: _FakeQdrant):
    monkeypatch.setattr(pipeline, "resolve_for_access", lambda p: p)
    monkeypatch.setattr(pipeline, "get_exif_basic", lambda _p: {})
    monkeypatch.setattr(pipeline, "write_thumbnail", lambda *_a, **_k: None)
    monkeypatch.setattr(pipeline, "ClipEmbedder", _FakeClipEmbedder)
    monkeypatch.setattr(pipeline, "_load_historical_perf", lambda _p: (1.0, 30.0, 5.0))
    monkeypatch.setattr(pipeline, "_emit_indexer_summary", lambda **_p: None)
    monkeypatch.setattr(pipeline, "get_qdrant_export_version", qdrant.get_version)
    monkeypatch.setattr(pipeline, "record_qdrant_export_version", qdrant.record_version)
    monkeypatch.setattr(pipeline, "_do_qdrant_export", qdrant.do_export)


class _CountingSha:
    """Real content hashing (so copies share media_id) with call counting."""

    def __init__(self):
        self.calls: list[str] = []

    def __call__(self, p):
        from msa_indexer.utils.hashes import sha256_of_file as real_sha

        self.calls.append(str(p))
        return real_sha(p)


def _make_images(media_dir: Path, count: int, prefix: str = "img") -> list[Path]:
    paths = []
    for i in range(count):
        p = media_dir / f"{prefix}_{i:03d}.jpg"
        # distinct pixel content per file → distinct sha256
        Image.new("RGB", (8, 8), color=(i % 256, (i * 7) % 256, 200)).save(p)
        paths.append(p)
    return paths


def _index_seq(sqlite_path) -> int:
    conn = sqlite3.connect(str(sqlite_path))
    try:
        row = conn.execute(
            "SELECT index_version_seq FROM index_state WHERE singleton_id=1"
        ).fetchone()
        return int(row[0]) if row else 0
    finally:
        conn.close()


@pytest.fixture
def captured_log_messages():
    """Capture loguru INFO+ messages into a list for the duration of the test."""
    from loguru import logger

    messages: list[str] = []
    handler_id = logger.add(
        lambda msg: messages.append(str(msg)), level="INFO", format="{level}|{message}"
    )
    yield messages
    logger.remove(handler_id)


# ---------------------------------------------------------------------------
# R1 (§3.5, P1): a no-op run must not bump index_version_seq and must not
# export — even though bookkeeping writes happen on the connection.
# ---------------------------------------------------------------------------


class TestNoOpRunRegression:
    def test_noop_run_keeps_version_and_skips_export(
        self, tmp_path, monkeypatch, captured_log_messages
    ):
        from msa_indexer import pipeline

        media_dir = tmp_path / "photos"
        media_dir.mkdir()
        _make_images(media_dir, 3)

        qdrant = _FakeQdrant()
        _patch_pipeline_common(monkeypatch, pipeline, qdrant)
        sha = _CountingSha()
        monkeypatch.setattr(pipeline, "sha256_of_file", sha)

        config = _pipeline_config(tmp_path, media_dir)

        # First run: everything is new → content changed, version bumped,
        # export fired and its version recorded.
        pipeline.run_index(config)
        seq_after_first = _index_seq(config.sqlite_path)
        assert seq_after_first == 1
        assert qdrant.export_calls == 1
        assert qdrant.recorded["index_version_seq"] == 1

        # Second run: nothing changed → seq unchanged, no export, and the
        # explicit skip line is logged.
        captured_log_messages.clear()
        pipeline.run_index(config)
        assert _index_seq(config.sqlite_path) == seq_after_first
        assert qdrant.export_calls == 1
        assert any(
            "Skipping Qdrant export (no index changes detected" in m
            for m in captured_log_messages
        ), "expected the no-op skip log line"


# ---------------------------------------------------------------------------
# §3.3 fast path: hit / mismatch / supersede / move / copy / resurrect
# ---------------------------------------------------------------------------


def _media_rows(sqlite_path):
    conn = sqlite3.connect(str(sqlite_path))
    try:
        cur = conn.execute(
            "SELECT media_id, path, source_name, rel_path, deleted, missing_since_scan_id"
            " FROM media ORDER BY media_id"
        )
        return [
            {
                "media_id": r[0],
                "path": r[1],
                "source_name": r[2],
                "rel_path": r[3],
                "deleted": int(r[4] or 0),
                "missing_since_scan_id": r[5],
            }
            for r in cur.fetchall()
        ]
    finally:
        conn.close()


def _fp_rows(sqlite_path):
    conn = sqlite3.connect(str(sqlite_path))
    try:
        cur = conn.execute(
            "SELECT source_name, rel_path, media_id, last_seen_scan_id, missing_since_scan_id"
            " FROM file_fingerprint ORDER BY source_name, rel_path"
        )
        return [
            {
                "source_name": r[0],
                "rel_path": r[1],
                "media_id": r[2],
                "last_seen_scan_id": r[3],
                "missing_since_scan_id": r[4],
            }
            for r in cur.fetchall()
        ]
    finally:
        conn.close()


def _table_count(sqlite_path, table: str) -> int:
    conn = sqlite3.connect(str(sqlite_path))
    try:
        return conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
    finally:
        conn.close()


def _ordered_walk(monkeypatch, pipeline):
    """Force deterministic (alphabetical) walk order in the main loop."""
    from msa_indexer.io.scanner import iter_media_entries as real_ime

    monkeypatch.setattr(
        pipeline,
        "iter_media_entries",
        lambda root, **kw: sorted(real_ime(root, **kw), key=lambda t: str(t[0])),
    )


def _bump_mtime(p: Path, delta_s: float = 5.0):
    st = os.stat(p)
    os.utime(p, ns=(st.st_atime_ns, st.st_mtime_ns + int(delta_s * 1e9)))


class TestFastPath:
    def _setup(self, tmp_path, monkeypatch, n_images=2):
        from msa_indexer import pipeline

        media_dir = tmp_path / "photos"
        media_dir.mkdir()
        paths = _make_images(media_dir, n_images)
        qdrant = _FakeQdrant()
        _patch_pipeline_common(monkeypatch, pipeline, qdrant)
        _ordered_walk(monkeypatch, pipeline)
        sha = _CountingSha()
        monkeypatch.setattr(pipeline, "sha256_of_file", sha)
        config = _pipeline_config(tmp_path, media_dir)
        return pipeline, config, media_dir, paths, sha, qdrant

    def test_hit_skips_hashing(self, tmp_path, monkeypatch):
        pipeline, config, _media_dir, paths, sha, _q = self._setup(tmp_path, monkeypatch)
        pipeline.run_index(config)
        assert len(sha.calls) == len(paths)
        assert len(_fp_rows(config.sqlite_path)) == len(paths)

        sha.calls.clear()
        embed_before = _FakeClipEmbedder.calls
        pipeline.run_index(config)
        assert sha.calls == [], "fast-path hit must not hash"
        assert _FakeClipEmbedder.calls == embed_before, "hit must not re-embed"

    def test_stat_mismatch_same_hash_updates_without_reprocess(self, tmp_path, monkeypatch):
        pipeline, config, _media_dir, paths, sha, _q = self._setup(tmp_path, monkeypatch)
        pipeline.run_index(config)
        fp_before = {r["rel_path"]: r for r in _fp_rows(config.sqlite_path)}

        _bump_mtime(paths[0])
        sha.calls.clear()
        embed_before = _FakeClipEmbedder.calls
        pipeline.run_index(config)

        assert len(sha.calls) == 1, "only the touched file is hashed"
        assert _FakeClipEmbedder.calls == embed_before, "no reprocessing"
        fp_after = {r["rel_path"]: r for r in _fp_rows(config.sqlite_path)}
        rel0 = paths[0].name
        assert fp_after[rel0]["media_id"] == fp_before[rel0]["media_id"]
        # stat fields were refreshed (mtime_ns column)
        conn = sqlite3.connect(config.sqlite_path)
        try:
            mtime_db = conn.execute(
                "SELECT mtime_ns FROM file_fingerprint WHERE rel_path=?", (rel0,)
            ).fetchone()[0]
        finally:
            conn.close()
        assert mtime_db == os.stat(paths[0]).st_mtime_ns
        # not a content change: version seq unchanged
        assert _index_seq(config.sqlite_path) == 1

    def test_content_replaced_supersede_shares_path(self, tmp_path, monkeypatch):
        pipeline, config, _media_dir, paths, _sha, _q = self._setup(tmp_path, monkeypatch)
        pipeline.run_index(config)
        rows = _media_rows(config.sqlite_path)
        old_id = next(r["media_id"] for r in rows if r["rel_path"] == paths[0].name)

        # Replace content in place with a brand-new image
        Image.new("RGB", (8, 8), color=(1, 2, 3)).save(paths[0])
        _bump_mtime(paths[0])
        pipeline.run_index(config)

        rows = _media_rows(config.sqlite_path)
        same_path_rows = [r for r in rows if r["path"] == str(paths[0])]
        assert len(same_path_rows) == 2, "tombstoned + live successor share the path"
        old_row = next(r for r in same_path_rows if r["media_id"] == old_id)
        new_row = next(r for r in same_path_rows if r["media_id"] != old_id)
        assert old_row["deleted"] == 1
        assert new_row["deleted"] == 0
        # fingerprint now points at the successor
        fp = {r["rel_path"]: r for r in _fp_rows(config.sqlite_path)}
        assert fp[paths[0].name]["media_id"] == new_row["media_id"]
        # the successor got processed (embedding exists)
        conn = sqlite3.connect(config.sqlite_path)
        try:
            has_emb = conn.execute(
                "SELECT 1 FROM image_embedding WHERE media_id=?",
                (new_row["media_id"],),
            ).fetchone()
        finally:
            conn.close()
        assert has_emb is not None

    def test_replacing_canonical_copy_promotes_surviving_duplicate(
        self, tmp_path, monkeypatch
    ):
        import shutil

        pipeline, config, media_dir, _paths, _sha, _q = self._setup(
            tmp_path, monkeypatch, n_images=0
        )
        orig = media_dir / "aaa_orig.jpg"
        Image.new("RGB", (8, 8), color=(9, 9, 9)).save(orig)
        dup = media_dir / "zzz_copy.jpg"
        shutil.copy2(orig, dup)

        pipeline.run_index(config)
        rows = _media_rows(config.sqlite_path)
        assert len(rows) == 1, "duplicate copies collapse to one media row"
        mid = rows[0]["media_id"]
        assert rows[0]["rel_path"] == "aaa_orig.jpg", "first path wins"

        # Replace the CANONICAL copy in place with brand-new content while
        # the duplicate still holds the old content elsewhere.
        Image.new("RGB", (8, 8), color=(1, 2, 3)).save(orig)
        _bump_mtime(orig)
        pipeline.run_index(config)

        rows = {r["media_id"]: r for r in _media_rows(config.sqlite_path)}
        old_row = rows[mid]
        assert old_row["deleted"] == 0, (
            "old content surviving at the duplicate must NOT be tombstoned (R5)"
        )
        assert old_row["rel_path"] == "zzz_copy.jpg", (
            "canonical path must be promoted to the surviving duplicate — the "
            "replaced path now holds different content"
        )
        assert old_row["path"] == str(dup)
        new_row = next(r for r in rows.values() if r["media_id"] != mid)
        assert new_row["deleted"] == 0
        assert new_row["rel_path"] == "aaa_orig.jpg"
        # Fingerprints: replaced path → successor content, duplicate → old.
        fp = {r["rel_path"]: r for r in _fp_rows(config.sqlite_path)}
        assert fp["aaa_orig.jpg"]["media_id"] == new_row["media_id"]
        assert fp["zzz_copy.jpg"]["media_id"] == mid

    def test_move_updates_path_without_reembedding(self, tmp_path, monkeypatch):
        pipeline, config, media_dir, paths, sha, _q = self._setup(tmp_path, monkeypatch)
        pipeline.run_index(config)
        emb_count = _table_count(config.sqlite_path, "image_embedding")
        embed_before = _FakeClipEmbedder.calls

        sub = media_dir / "album"
        sub.mkdir()
        moved = sub / paths[0].name
        paths[0].rename(moved)

        sha.calls.clear()
        pipeline.run_index(config)

        rows = _media_rows(config.sqlite_path)
        moved_row = next(r for r in rows if r["rel_path"] == f"album/{moved.name}")
        assert moved_row["path"] == str(moved)
        assert moved_row["deleted"] == 0
        assert _table_count(config.sqlite_path, "image_embedding") == emb_count
        assert _FakeClipEmbedder.calls == embed_before, "move must not re-embed"
        assert len(sha.calls) == 1, "only the moved file is hashed"
        # rel_path is POSIX-form (contains forward slash)
        assert "/" in moved_row["rel_path"] and "\\" not in moved_row["rel_path"]

    def test_copy_does_not_steal_path(self, tmp_path, monkeypatch):
        import shutil

        pipeline, config, media_dir, paths, _sha, _q = self._setup(tmp_path, monkeypatch)
        pipeline.run_index(config)
        orig_row = next(
            r for r in _media_rows(config.sqlite_path) if r["rel_path"] == paths[0].name
        )

        dup = media_dir / f"zz_copy_{paths[0].name}"
        shutil.copy2(paths[0], dup)
        pipeline.run_index(config)

        rows = _media_rows(config.sqlite_path)
        row = next(r for r in rows if r["media_id"] == orig_row["media_id"])
        assert row["path"] == orig_row["path"], "copy must not steal the canonical path"
        fp = _fp_rows(config.sqlite_path)
        pointing = [r for r in fp if r["media_id"] == orig_row["media_id"]]
        assert {r["rel_path"] for r in pointing} == {paths[0].name, dup.name}

    def test_duplicate_scanned_before_original_does_not_steal_path(self, tmp_path, monkeypatch):
        import shutil

        pipeline, config, media_dir, paths, _sha, _q = self._setup(tmp_path, monkeypatch)
        pipeline.run_index(config)
        orig_row = next(
            r for r in _media_rows(config.sqlite_path) if r["rel_path"] == paths[0].name
        )

        # alphabetical walk order → "aaa_copy..." is visited BEFORE the original
        dup = media_dir / f"aaa_copy_{paths[0].name}"
        shutil.copy2(paths[0], dup)
        pipeline.run_index(config)

        row = next(
            r for r in _media_rows(config.sqlite_path)
            if r["media_id"] == orig_row["media_id"]
        )
        assert row["path"] == orig_row["path"], (
            "a duplicate scanned before its original must not look like a move"
        )

    def test_post_remap_duplicate_does_not_rewrite_canonical_path(self, tmp_path, monkeypatch):
        import shutil

        pipeline, config, media_dir, paths, _sha, _q = self._setup(tmp_path, monkeypatch)
        pipeline.run_index(config)
        orig_row = next(
            r for r in _media_rows(config.sqlite_path) if r["rel_path"] == paths[0].name
        )

        # Simulate a source-root remap: the stored absolute path is stale but
        # (source_name, rel_path) still resolves under the current root.
        stale_path = "/mnt/old-root/" + paths[0].name
        conn = sqlite3.connect(config.sqlite_path)
        try:
            conn.execute(
                "UPDATE media SET path=? WHERE media_id=?",
                (stale_path, orig_row["media_id"]),
            )
            conn.commit()
        finally:
            conn.close()

        dup = media_dir / f"aaa_copy_{paths[0].name}"
        shutil.copy2(paths[0], dup)
        pipeline.run_index(config)

        row = next(
            r for r in _media_rows(config.sqlite_path)
            if r["media_id"] == orig_row["media_id"]
        )
        assert row["path"] == stale_path, (
            "copy-vs-move must stat the RESOLVED (source_name, rel_path), not "
            "the stale stored path — the duplicate is a copy, not a move"
        )

    def test_content_exchange_repoints_both_media(self, tmp_path, monkeypatch):
        """Two already-indexed files exchange contents in one scan: after the
        run each media_id's path must point at a file whose content IS that
        media_id. Bare `current.exists()` misclassified the first visit as a
        "copy" (the canonical path exists but holds foreign, unrefreshed
        bytes), leaving media rows pointing at the wrong file."""
        pipeline, config, _media_dir, paths, sha, _q = self._setup(tmp_path, monkeypatch)
        pipeline.run_index(config)
        rows = _media_rows(config.sqlite_path)
        id_a = next(r["media_id"] for r in rows if r["rel_path"] == paths[0].name)
        id_b = next(r["media_id"] for r in rows if r["rel_path"] == paths[1].name)

        # Exchange the two files' contents in place (no rename).
        bytes_a = paths[0].read_bytes()
        bytes_b = paths[1].read_bytes()
        paths[0].write_bytes(bytes_b)
        paths[1].write_bytes(bytes_a)
        _bump_mtime(paths[0], 5.0)
        _bump_mtime(paths[1], 7.0)

        sha.calls.clear()
        embed_before = _FakeClipEmbedder.calls
        pipeline.run_index(config)

        rows = {r["media_id"]: r for r in _media_rows(config.sqlite_path)}
        assert rows[id_a]["deleted"] == 0 and rows[id_b]["deleted"] == 0, (
            "both contents still exist on disk — neither may stay tombstoned"
        )
        assert rows[id_a]["path"] == str(paths[1]), (
            "A's content now lives at B's old path — the media row must follow it"
        )
        assert rows[id_a]["rel_path"] == paths[1].name
        assert rows[id_b]["path"] == str(paths[0])
        assert rows[id_b]["rel_path"] == paths[0].name
        fp = {r["rel_path"]: r for r in _fp_rows(config.sqlite_path)}
        assert fp[paths[0].name]["media_id"] == id_b
        assert fp[paths[1].name]["media_id"] == id_a
        # Known content, already embedded: repoints only, no reprocessing.
        assert _FakeClipEmbedder.calls == embed_before
        assert len(sha.calls) == 2, "each exchanged file is hashed exactly once"

    def test_duplicate_with_unfingerprinted_canonical_does_not_steal_path(
        self, tmp_path, monkeypatch
    ):
        """Fallback leg of the canonical-bytes check: when the canonical path
        has NO fingerprint row yet (pre-M-8 rows before lazy backfill), a
        duplicate visited first must verify by content hash and classify as a
        copy — not repoint the canonical path (no migration-run path churn)."""
        import shutil

        pipeline, config, media_dir, paths, _sha, _q = self._setup(tmp_path, monkeypatch)
        # Seed media rows WITHOUT fingerprint rows (pre-M-8 upgrade shape).
        config.incremental = SimpleNamespace(fingerprint_enabled=False, deletion_sweep=True)
        pipeline.run_index(config)
        assert _fp_rows(config.sqlite_path) == []
        orig_row = next(
            r for r in _media_rows(config.sqlite_path) if r["rel_path"] == paths[0].name
        )

        # alphabetical walk order → the duplicate is visited BEFORE the
        # original on the first fingerprint-enabled run
        dup = media_dir / f"aaa_copy_{paths[0].name}"
        shutil.copy2(paths[0], dup)
        config.incremental = SimpleNamespace(fingerprint_enabled=True, deletion_sweep=True)
        pipeline.run_index(config)

        row = next(
            r for r in _media_rows(config.sqlite_path)
            if r["media_id"] == orig_row["media_id"]
        )
        assert row["path"] == orig_row["path"], (
            "content-verified canonical (no fingerprint row yet) is a copy — "
            "the duplicate must not steal the path"
        )
        fp = _fp_rows(config.sqlite_path)
        pointing = [r for r in fp if r["media_id"] == orig_row["media_id"]]
        assert {r["rel_path"] for r in pointing} == {paths[0].name, dup.name}

    def test_replacing_path_with_deleted_content_resurrects_before_skip_gate(
        self, tmp_path, monkeypatch
    ):
        pipeline, config, _media_dir, paths, _sha, _q = self._setup(
            tmp_path, monkeypatch, n_images=2
        )
        pipeline.run_index(config)
        rows = _media_rows(config.sqlite_path)
        id_a = next(r["media_id"] for r in rows if r["rel_path"] == paths[0].name)
        id_b = next(r["media_id"] for r in rows if r["rel_path"] == paths[1].name)

        # Simulate B having been swept earlier: tombstone the row, drop its
        # fingerprint, remove its file.
        b_bytes = paths[1].read_bytes()
        conn = sqlite3.connect(config.sqlite_path)
        try:
            conn.execute("UPDATE media SET deleted=1 WHERE media_id=?", (id_b,))
            conn.execute("DELETE FROM file_fingerprint WHERE media_id=?", (id_b,))
            conn.commit()
        finally:
            conn.close()
        paths[1].unlink()

        # Replace A's content with B's previously-deleted content.
        paths[0].write_bytes(b_bytes)
        _bump_mtime(paths[0])
        embed_before = _FakeClipEmbedder.calls
        pipeline.run_index(config)

        rows = {r["media_id"]: r for r in _media_rows(config.sqlite_path)}
        assert rows[id_b]["deleted"] == 0, "previously-deleted content must resurrect"
        assert rows[id_b]["path"] == str(paths[0]), "canonical path follows the live copy"
        assert rows[id_a]["deleted"] == 1, "A's content was superseded at its last path"
        # resurrection happened BEFORE the skip gate: B was fully processed
        # already, so no re-embedding was needed
        assert _FakeClipEmbedder.calls == embed_before

    def test_reprocess_flags_force_work_on_fastpath_files(self, tmp_path, monkeypatch):
        pipeline, config, _media_dir, paths, sha, _q = self._setup(tmp_path, monkeypatch)
        exif_calls = []
        monkeypatch.setattr(
            pipeline, "get_exif_basic", lambda p: (exif_calls.append(str(p)) or {})
        )
        pipeline.run_index(config)
        assert len(exif_calls) == len(paths)

        exif_calls.clear()
        sha.calls.clear()
        config.reprocess_gps = True
        pipeline.run_index(config)
        assert len(exif_calls) == len(paths), (
            "reprocess flags must still force work on fingerprint-hit files"
        )
        assert sha.calls == [], "reprocessing rides the fast path — no hashing"

    def test_verify_content_hashes_everything(self, tmp_path, monkeypatch):
        pipeline, config, _media_dir, paths, sha, _q = self._setup(tmp_path, monkeypatch)
        pipeline.run_index(config)

        sha.calls.clear()
        embed_before = _FakeClipEmbedder.calls
        config.verify_content = True
        pipeline.run_index(config)
        assert len(sha.calls) == len(paths), "--verify-content hashes every file"
        assert _FakeClipEmbedder.calls == embed_before, "unchanged content: no reprocess"
        assert _index_seq(config.sqlite_path) == 1

    def test_kill_switch_writes_zero_fingerprint_rows(self, tmp_path, monkeypatch):
        pipeline, config, _media_dir, paths, sha, _q = self._setup(tmp_path, monkeypatch)
        config.incremental = SimpleNamespace(fingerprint_enabled=False, deletion_sweep=True)
        pipeline.run_index(config)
        assert _fp_rows(config.sqlite_path) == []
        assert _table_count(config.sqlite_path, "scan_run") == 0

        # every run hashes everything (behaves byte-identically to pre-M-8)
        sha.calls.clear()
        pipeline.run_index(config)
        assert len(sha.calls) == len(paths)
        assert _fp_rows(config.sqlite_path) == []

    def test_lazy_backfill_first_run_hashes_then_hits(self, tmp_path, monkeypatch):
        """A DB with media rows but no fingerprints (the pre-M-8 upgrade
        shape) backfills lazily: first enabled run hashes everything without
        reprocessing, second run hits everything."""
        pipeline, config, _media_dir, paths, sha, _q = self._setup(tmp_path, monkeypatch)
        # Run 1 with the kill switch ON → media rows exist, no fingerprints
        config.incremental = SimpleNamespace(fingerprint_enabled=False, deletion_sweep=True)
        pipeline.run_index(config)
        assert _fp_rows(config.sqlite_path) == []
        embed_after_initial = _FakeClipEmbedder.calls

        # Run 2 (enabled): hash-only backfill, no reprocessing
        config.incremental = SimpleNamespace(fingerprint_enabled=True, deletion_sweep=True)
        sha.calls.clear()
        pipeline.run_index(config)
        assert len(sha.calls) == len(paths)
        assert len(_fp_rows(config.sqlite_path)) == len(paths)
        assert _FakeClipEmbedder.calls == embed_after_initial
        assert _index_seq(config.sqlite_path) == 1, "backfill is not a content change"

        # Run 3: all hits
        sha.calls.clear()
        pipeline.run_index(config)
        assert sha.calls == []


# ---------------------------------------------------------------------------
# §3.4 deletion sweep: grace, tombstone, gates (R3), promotion (R5),
# legacy orphan reconcile, kill switch
# ---------------------------------------------------------------------------


class _AlwaysStopped:
    def is_set(self):
        return True


class TestDeletionSweep:
    def _setup(self, tmp_path, monkeypatch, n_images=2):
        from msa_indexer import pipeline

        media_dir = tmp_path / "photos"
        media_dir.mkdir()
        paths = _make_images(media_dir, n_images)
        qdrant = _FakeQdrant()
        _patch_pipeline_common(monkeypatch, pipeline, qdrant)
        _ordered_walk(monkeypatch, pipeline)
        sha = _CountingSha()
        monkeypatch.setattr(pipeline, "sha256_of_file", sha)
        config = _pipeline_config(tmp_path, media_dir)
        return pipeline, config, media_dir, paths, sha, qdrant

    def test_miss_grace_tombstone_across_two_completed_scans(self, tmp_path, monkeypatch):
        pipeline, config, _media_dir, paths, _sha, _q = self._setup(tmp_path, monkeypatch)
        pipeline.run_index(config)
        gone_rel = paths[0].name
        gone_id = next(
            r["media_id"] for r in _media_rows(config.sqlite_path)
            if r["rel_path"] == gone_rel
        )
        paths[0].unlink()

        # Scan 2 (first miss): grace only — no visible effect
        pipeline.run_index(config)
        fp = {r["rel_path"]: r for r in _fp_rows(config.sqlite_path)}
        assert fp[gone_rel]["missing_since_scan_id"] is not None
        rows = {r["media_id"]: r for r in _media_rows(config.sqlite_path)}
        assert rows[gone_id]["deleted"] == 0
        seq_after_grace = _index_seq(config.sqlite_path)
        assert seq_after_grace == 1, "grace stamp is not a content change"

        # Scan 3 (second miss): fingerprint row deleted, media tombstoned
        pipeline.run_index(config)
        fp = {r["rel_path"]: r for r in _fp_rows(config.sqlite_path)}
        assert gone_rel not in fp
        rows = {r["media_id"]: r for r in _media_rows(config.sqlite_path)}
        assert rows[gone_id]["deleted"] == 1
        assert _index_seq(config.sqlite_path) == seq_after_grace + 1, (
            "a tombstone IS a content change"
        )

    def test_no_sweep_on_stopped_walk(self, tmp_path, monkeypatch):
        pipeline, config, _media_dir, paths, _sha, _q = self._setup(tmp_path, monkeypatch)
        pipeline.run_index(config)
        paths[0].unlink()

        pipeline.run_index(config, stop_event=_AlwaysStopped())
        fp = {r["rel_path"]: r for r in _fp_rows(config.sqlite_path)}
        assert fp[paths[0].name]["missing_since_scan_id"] is None, (
            "a stopped walk must not start the deletion grace"
        )
        assert all(r["deleted"] == 0 for r in _media_rows(config.sqlite_path))
        # the stopped scan was never marked completed
        conn = sqlite3.connect(config.sqlite_path)
        try:
            completed = conn.execute(
                "SELECT COUNT(*) FROM scan_run WHERE completed_at IS NOT NULL"
            ).fetchone()[0]
            total = conn.execute("SELECT COUNT(*) FROM scan_run").fetchone()[0]
        finally:
            conn.close()
        assert total == 2 and completed == 1

    def test_no_sweep_on_errored_walk(self, tmp_path, monkeypatch):
        pipeline, config, _media_dir, paths, _sha, _q = self._setup(tmp_path, monkeypatch)
        pipeline.run_index(config)
        paths[0].unlink()

        from msa_indexer.io.scanner import iter_media_entries as real_ime

        def _walk_with_error(root, **kw):
            yield from sorted(real_ime(root, **kw), key=lambda t: str(t[0]))
            stats = kw.get("stats")
            if stats is not None:
                stats.walk_errors += 1  # simulate a swallowed OSError

        monkeypatch.setattr(pipeline, "iter_media_entries", _walk_with_error)
        pipeline.run_index(config)
        pipeline.run_index(config)

        fp = {r["rel_path"]: r for r in _fp_rows(config.sqlite_path)}
        assert fp[paths[0].name]["missing_since_scan_id"] is None, (
            "walk errors must disqualify the source from sweeping (R3)"
        )
        assert all(r["deleted"] == 0 for r in _media_rows(config.sqlite_path))

    def test_no_sweep_for_absent_root_source(self, tmp_path, monkeypatch):
        import shutil

        from msa_indexer import pipeline

        dir_a = tmp_path / "src_a"
        dir_b = tmp_path / "src_b"
        dir_a.mkdir()
        dir_b.mkdir()
        _make_images(dir_a, 1, prefix="a")
        _make_images(dir_b, 1, prefix="b")

        qdrant = _FakeQdrant()
        _patch_pipeline_common(monkeypatch, pipeline, qdrant)
        _ordered_walk(monkeypatch, pipeline)
        monkeypatch.setattr(pipeline, "sha256_of_file", _CountingSha())
        config = _pipeline_config(tmp_path, dir_a)
        config.media_sources = [
            SimpleNamespace(name="src_a", path=str(dir_a), enabled=True),
            SimpleNamespace(name="src_b", path=str(dir_b), enabled=True),
        ]

        pipeline.run_index(config)
        assert len(_fp_rows(config.sqlite_path)) == 2

        # Unmount/remove source A entirely: its rows must be untouched even
        # after two runs that only see source B.
        shutil.rmtree(dir_a)
        pipeline.run_index(config)
        pipeline.run_index(config)

        fp_a = [r for r in _fp_rows(config.sqlite_path) if r["source_name"] == "src_a"]
        assert len(fp_a) == 1
        assert fp_a[0]["missing_since_scan_id"] is None, (
            "an absent source root must never look like mass deletion (R3)"
        )
        assert all(r["deleted"] == 0 for r in _media_rows(config.sqlite_path))

    def test_no_sweep_when_root_vanishes_after_validation(self, tmp_path, monkeypatch):
        """R3 race window: the root exists at run_index's upfront validation
        but disappears before the walk starts (transient unmount). The walk
        must count it as a walk error so the sweep is disqualified — NOT
        return cleanly with walk_errors == 0, which would grace and (on a
        second occurrence) mass-tombstone every fingerprint under the
        source."""
        import shutil

        pipeline, config, media_dir, paths, _sha, _q = self._setup(tmp_path, monkeypatch)
        pipeline.run_index(config)
        assert len(_fp_rows(config.sqlite_path)) == len(paths)

        from msa_indexer.io.scanner import iter_media_entries as real_ime

        def _vanish_then_walk(root, **kw):
            # Simulate the unmount landing between validation and walk start.
            if Path(root).exists():
                shutil.rmtree(root)
            return real_ime(root, **kw)

        monkeypatch.setattr(pipeline, "iter_media_entries", _vanish_then_walk)
        pipeline.run_index(config)

        fp = _fp_rows(config.sqlite_path)
        assert len(fp) == len(paths)
        assert all(r["missing_since_scan_id"] is None for r in fp), (
            "a vanished root must not start the deletion grace (R3)"
        )
        assert all(r["deleted"] == 0 for r in _media_rows(config.sqlite_path))

    def test_two_copy_deletion_keeps_media_alive_and_promotes_path(self, tmp_path, monkeypatch):
        import shutil

        pipeline, config, media_dir, _paths, _sha, _q = self._setup(
            tmp_path, monkeypatch, n_images=0
        )
        orig = media_dir / "aaa_orig.jpg"
        Image.new("RGB", (8, 8), color=(9, 9, 9)).save(orig)
        dup = media_dir / "zzz_copy.jpg"
        shutil.copy2(orig, dup)

        pipeline.run_index(config)
        rows = _media_rows(config.sqlite_path)
        assert len(rows) == 1, "duplicate copies collapse to one media row"
        mid = rows[0]["media_id"]
        assert rows[0]["rel_path"] == "aaa_orig.jpg", "first path wins"

        # Delete the CANONICAL copy; two completed scans age it out.
        orig.unlink()
        pipeline.run_index(config)   # grace
        pipeline.run_index(config)   # delete fp + promote surviving path

        rows = _media_rows(config.sqlite_path)
        assert len(rows) == 1
        assert rows[0]["media_id"] == mid
        assert rows[0]["deleted"] == 0, (
            "content still on disk elsewhere must NOT be tombstoned (R5)"
        )
        assert rows[0]["rel_path"] == "zzz_copy.jpg", "surviving path promoted"
        assert rows[0]["path"] == str(dup)
        fp = _fp_rows(config.sqlite_path)
        assert [r["rel_path"] for r in fp] == ["zzz_copy.jpg"]

    def test_resurrection_on_reappearance_clears_grace(self, tmp_path, monkeypatch):
        pipeline, config, _media_dir, paths, _sha, _q = self._setup(tmp_path, monkeypatch)
        pipeline.run_index(config)
        content = paths[0].read_bytes()
        paths[0].unlink()
        pipeline.run_index(config)  # grace set
        fp = {r["rel_path"]: r for r in _fp_rows(config.sqlite_path)}
        assert fp[paths[0].name]["missing_since_scan_id"] is not None

        # File reappears (same content) before the second sweep scan
        paths[0].write_bytes(content)
        pipeline.run_index(config)
        fp = {r["rel_path"]: r for r in _fp_rows(config.sqlite_path)}
        assert fp[paths[0].name]["missing_since_scan_id"] is None
        assert all(r["deleted"] == 0 for r in _media_rows(config.sqlite_path))

    def test_resurrection_after_tombstone(self, tmp_path, monkeypatch):
        pipeline, config, _media_dir, paths, _sha, _q = self._setup(tmp_path, monkeypatch)
        pipeline.run_index(config)
        content = paths[0].read_bytes()
        gone_id = next(
            r["media_id"] for r in _media_rows(config.sqlite_path)
            if r["rel_path"] == paths[0].name
        )
        paths[0].unlink()
        pipeline.run_index(config)
        pipeline.run_index(config)
        assert {r["media_id"]: r for r in _media_rows(config.sqlite_path)}[gone_id][
            "deleted"
        ] == 1

        embed_before = _FakeClipEmbedder.calls
        paths[0].write_bytes(content)
        pipeline.run_index(config)
        row = {r["media_id"]: r for r in _media_rows(config.sqlite_path)}[gone_id]
        assert row["deleted"] == 0, "reappearing content resurrects the tombstoned row"
        assert _FakeClipEmbedder.calls == embed_before, "no re-embedding on resurrection"

    def test_legacy_orphan_reconcile_tombstones_pre_upgrade_deletions(
        self, tmp_path, monkeypatch
    ):
        pipeline, config, _media_dir, paths, _sha, _q = self._setup(tmp_path, monkeypatch)
        # Pre-upgrade shape: media rows exist, no fingerprints at all
        config.incremental = SimpleNamespace(fingerprint_enabled=False, deletion_sweep=True)
        pipeline.run_index(config)
        gone_id = next(
            r["media_id"] for r in _media_rows(config.sqlite_path)
            if r["rel_path"] == paths[0].name
        )
        # ...and the file was deleted BEFORE the upgrade, so the lazy backfill
        # will never fingerprint it.
        paths[0].unlink()

        config.incremental = SimpleNamespace(fingerprint_enabled=True, deletion_sweep=True)
        pipeline.run_index(config)  # completed scan 1: grace
        rows = {r["media_id"]: r for r in _media_rows(config.sqlite_path)}
        assert rows[gone_id]["deleted"] == 0
        assert rows[gone_id]["missing_since_scan_id"] is not None

        pipeline.run_index(config)  # completed scan 2: tombstone
        rows = {r["media_id"]: r for r in _media_rows(config.sqlite_path)}
        assert rows[gone_id]["deleted"] == 1
        # the still-present file was backfilled and is NOT an orphan
        others = [r for r in rows.values() if r["media_id"] != gone_id]
        assert all(r["deleted"] == 0 and r["missing_since_scan_id"] is None for r in others)

    def test_kill_switch_run_writes_zero_tombstones(self, tmp_path, monkeypatch):
        pipeline, config, _media_dir, paths, _sha, _q = self._setup(tmp_path, monkeypatch)
        pipeline.run_index(config)
        paths[0].unlink()

        config.incremental = SimpleNamespace(fingerprint_enabled=False, deletion_sweep=True)
        pipeline.run_index(config)
        pipeline.run_index(config)

        assert all(r["deleted"] == 0 for r in _media_rows(config.sqlite_path)), (
            "fingerprint_enabled: false must hard-disable the sweep AND the "
            "legacy orphan reconcile (P1 mass-tombstone trap)"
        )
        fp = {r["rel_path"]: r for r in _fp_rows(config.sqlite_path)}
        assert fp[paths[0].name]["missing_since_scan_id"] is None

    def test_deletion_sweep_false_disables_sweep_only(self, tmp_path, monkeypatch):
        pipeline, config, _media_dir, paths, _sha, _q = self._setup(tmp_path, monkeypatch)
        pipeline.run_index(config)
        paths[0].unlink()

        config.incremental = SimpleNamespace(fingerprint_enabled=True, deletion_sweep=False)
        pipeline.run_index(config)
        pipeline.run_index(config)

        assert all(r["deleted"] == 0 for r in _media_rows(config.sqlite_path))
        fp = {r["rel_path"]: r for r in _fp_rows(config.sqlite_path)}
        assert fp[paths[0].name]["missing_since_scan_id"] is None
        # fast path still active for surviving files
        assert paths[1].name in fp

    def test_media_type_filtered_run_does_not_sweep(self, tmp_path, monkeypatch):
        pipeline, config, _media_dir, paths, _sha, _q = self._setup(tmp_path, monkeypatch)
        pipeline.run_index(config)
        paths[0].unlink()

        # A video-only run never marks image fingerprints seen — it must not
        # sweep (it would mass-tombstone every image) nor complete the scan.
        config.video_only = True
        pipeline.run_index(config)
        pipeline.run_index(config)

        fp = {r["rel_path"]: r for r in _fp_rows(config.sqlite_path)}
        assert fp[paths[0].name]["missing_since_scan_id"] is None
        assert all(r["deleted"] == 0 for r in _media_rows(config.sqlite_path))
        conn = sqlite3.connect(config.sqlite_path)
        try:
            completed = conn.execute(
                "SELECT COUNT(*) FROM scan_run WHERE completed_at IS NOT NULL"
            ).fetchone()[0]
        finally:
            conn.close()
        assert completed == 1, "filtered runs must not record scan completion"


# ---------------------------------------------------------------------------
# §3.6 observability: INDEXER_SUMMARY complete counters + fast-path log line
# ---------------------------------------------------------------------------


class TestObservability:
    def _setup(self, tmp_path, monkeypatch, n_images=2):
        from msa_indexer import pipeline

        media_dir = tmp_path / "photos"
        media_dir.mkdir()
        paths = _make_images(media_dir, n_images)
        qdrant = _FakeQdrant()
        _patch_pipeline_common(monkeypatch, pipeline, qdrant)
        _ordered_walk(monkeypatch, pipeline)
        monkeypatch.setattr(pipeline, "sha256_of_file", _CountingSha())
        summaries: list[dict] = []
        monkeypatch.setattr(
            pipeline, "_emit_indexer_summary", lambda **p: summaries.append(p)
        )
        config = _pipeline_config(tmp_path, media_dir)
        return pipeline, config, media_dir, paths, summaries

    @staticmethod
    def _complete(summaries):
        return next(s for s in reversed(summaries) if s.get("phase") == "complete")

    def test_noop_run_reports_hits_and_zero_hashed(self, tmp_path, monkeypatch):
        pipeline, config, _d, paths, summaries = self._setup(tmp_path, monkeypatch)
        pipeline.run_index(config)
        first = self._complete(summaries)
        assert first["files_hashed"] == len(paths)
        assert first["fingerprint_hits"] == 0

        summaries.clear()
        pipeline.run_index(config)
        complete = self._complete(summaries)
        assert complete["fingerprint_hits"] == len(paths)
        assert complete["files_hashed"] == 0
        assert complete["moves_detected"] == 0
        assert complete["superseded"] == 0
        assert complete["missing_marked"] == 0
        assert complete["tombstoned"] == 0
        assert complete["resurrected"] == 0

    def test_move_and_deletion_counters(self, tmp_path, monkeypatch):
        pipeline, config, media_dir, paths, summaries = self._setup(tmp_path, monkeypatch)
        pipeline.run_index(config)

        # Move one file
        sub = media_dir / "album"
        sub.mkdir()
        paths[0].rename(sub / paths[0].name)
        summaries.clear()
        pipeline.run_index(config)
        assert self._complete(summaries)["moves_detected"] == 1

        # Delete the other: grace, then tombstone
        paths[1].unlink()
        summaries.clear()
        pipeline.run_index(config)
        assert self._complete(summaries)["missing_marked"] == 1
        assert self._complete(summaries)["tombstoned"] == 0
        summaries.clear()
        pipeline.run_index(config)
        assert self._complete(summaries)["tombstoned"] == 1

    def test_supersede_and_resurrect_counters(self, tmp_path, monkeypatch):
        pipeline, config, _d, paths, summaries = self._setup(tmp_path, monkeypatch)
        pipeline.run_index(config)

        Image.new("RGB", (8, 8), color=(200, 100, 50)).save(paths[0])
        _bump_mtime(paths[0])
        summaries.clear()
        pipeline.run_index(config)
        complete = self._complete(summaries)
        assert complete["superseded"] == 1
        assert complete["files_hashed"] == 1

    def test_fastpath_log_line(self, tmp_path, monkeypatch, captured_log_messages):
        pipeline, config, _d, paths, _summaries = self._setup(tmp_path, monkeypatch)
        pipeline.run_index(config)
        captured_log_messages.clear()
        pipeline.run_index(config)
        line = next(
            (m for m in captured_log_messages if "Fingerprint fast-path:" in m), None
        )
        assert line is not None
        assert f"{len(paths)} hits, 0 hashed (0 new, 0 changed), 0 moves, 0 tombstoned" in line


# ---------------------------------------------------------------------------
# #208 — fingerprint-aware indexer ETA (M-8 follow-up).
#
# The count phase used to multiply EVERY found file by the historical
# per-item time, blind to the fingerprint fast-path that skips nearly all of
# them on an incremental run — producing library-sized ETAs (~days) on runs
# that finish in a minute. Two parts:
#   1. Count-phase estimate subtracts the files whose stored (size, mtime)
#      already matches on disk (the exact fast-path skip condition).
#   2. A rolling estimate re-projects the remainder from OBSERVED work at
#      each per-batch commit boundary.
# ---------------------------------------------------------------------------


class TestIncrementalEta:
    # per-item time comes from the patched _load_historical_perf in
    # _patch_pipeline_common: avg_image=1.0s, avg_video=30.0s.
    def _setup(self, tmp_path, monkeypatch, n_images=3):
        from msa_indexer import pipeline

        media_dir = tmp_path / "photos"
        media_dir.mkdir()
        paths = _make_images(media_dir, n_images)
        qdrant = _FakeQdrant()
        _patch_pipeline_common(monkeypatch, pipeline, qdrant)
        _ordered_walk(monkeypatch, pipeline)
        monkeypatch.setattr(pipeline, "sha256_of_file", _CountingSha())
        summaries: list[dict] = []
        monkeypatch.setattr(
            pipeline, "_emit_indexer_summary", lambda **p: summaries.append(p)
        )
        config = _pipeline_config(tmp_path, media_dir)
        return pipeline, config, media_dir, paths, summaries

    @staticmethod
    def _count_phase(summaries):
        # The count-phase processing summary is the one WITHOUT files_walked
        # (rolling re-estimates carry that key).
        return next(
            s for s in summaries
            if s.get("phase") == "processing" and "files_walked" not in s
        )

    @staticmethod
    def _rolling(summaries):
        return [
            s for s in summaries
            if s.get("phase") == "processing" and "files_walked" in s
        ]

    def test_count_phase_estimate_excludes_fingerprinted_files(self, tmp_path, monkeypatch):
        # (a) With a populated fingerprint table and every file unchanged, the
        # initial estimate collapses to ~0 — NOT found × per_item_time.
        pipeline, config, _d, paths, summaries = self._setup(tmp_path, monkeypatch)
        pipeline.run_index(config)  # first run populates fingerprints
        summaries.clear()
        pipeline.run_index(config)  # incremental re-run: all fast-path hits

        n = len(paths)
        cp = self._count_phase(summaries)
        assert cp["estimated_remaining_seconds"] == 0
        assert cp["estimated_remaining_seconds"] < n, "must NOT be found × per_item"
        assert cp["expected_to_process"] == 0
        # existing found-count keys keep their meaning (parser + BVT read them)
        assert cp["total_found"] == n
        assert cp["images_to_process"] == n

    def test_stat_mismatch_file_counts_toward_estimate(self, tmp_path, monkeypatch):
        # (b) A file whose stat no longer matches its fingerprint is NOT an
        # expected skip — it counts toward the estimate (expected re-hash).
        pipeline, config, _d, paths, summaries = self._setup(tmp_path, monkeypatch)
        pipeline.run_index(config)
        _bump_mtime(paths[0])  # size/mtime drift → fingerprint no longer matches
        summaries.clear()
        pipeline.run_index(config)

        cp = self._count_phase(summaries)
        # only the drifted file remains — hist avg_image is 1.0s
        assert cp["expected_to_process"] == 1
        assert cp["estimated_remaining_seconds"] == 1

    def test_rolling_estimate_drops_after_mostly_hit_batch(self, tmp_path, monkeypatch):
        # (c) Even when the pre-count over-estimates, the rolling re-estimate
        # corrects toward reality once fast-path hits are observed.
        from msa_indexer.db.sqlite_store import SQLiteStore

        pipeline, config, _d, paths, summaries = self._setup(
            tmp_path, monkeypatch, n_images=4
        )
        pipeline.run_index(config)  # populate fingerprints

        # Blind ONLY the count-phase pre-count so it emits the old
        # library-sized estimate; the main-loop fast path still hits on the
        # real fingerprints. This isolates the Part-2 rolling correction.
        monkeypatch.setattr(
            SQLiteStore, "get_fingerprints_for_source", lambda self, s: {}
        )
        monkeypatch.setenv("MSA_INDEXER_COMMIT_BATCH_FILES", "1")
        summaries.clear()
        pipeline.run_index(config)

        n = len(paths)
        cp = self._count_phase(summaries)
        rolling = self._rolling(summaries)
        assert cp["estimated_remaining_seconds"] == n, "blind pre-count = library-sized"
        assert rolling, "expected a rolling re-estimate at a commit boundary"
        last = rolling[-1]["estimated_remaining_seconds"]
        assert last < cp["estimated_remaining_seconds"], "rolling estimate must drop"
        assert last == 0, "a mostly-hit batch spends ~0 work per file"

    def test_fingerprint_disabled_falls_back_to_full_estimate(self, tmp_path, monkeypatch):
        # (d) Kill switch: with the fast path off, no fingerprint is consulted
        # and the estimate is the full library (correct — every file is hashed).
        pipeline, config, _d, paths, summaries = self._setup(tmp_path, monkeypatch)
        pipeline.run_index(config)  # populate fingerprints (fast path on)
        config.incremental = SimpleNamespace(
            fingerprint_enabled=False, deletion_sweep=True
        )
        summaries.clear()
        pipeline.run_index(config)

        n = len(paths)
        cp = self._count_phase(summaries)
        assert cp["estimated_remaining_seconds"] == n, "full n × per_item, not 0"
        assert cp["expected_to_process"] == n  # == total_found

    def test_reprocess_flag_falls_back_to_full_estimate(self, tmp_path, monkeypatch):
        # (e) #208 review (Codex P2): a --reprocess-* run rides the fast path
        # (stat match ⇒ no re-hash) but STILL does full GPS/object/face/
        # embedding work on every matching file. Those files are not free, so
        # the count phase must NOT subtract fingerprint hits — it falls back to
        # the full-library estimate. Otherwise an unchanged-library reprocess
        # reports ~0s remaining while about to reprocess the whole library.
        pipeline, config, _d, paths, summaries = self._setup(tmp_path, monkeypatch)
        pipeline.run_index(config)  # populate fingerprints (all would fast-path)
        config.reprocess_gps = True  # forces GPS work on every stat-matching file
        summaries.clear()
        pipeline.run_index(config)

        n = len(paths)
        cp = self._count_phase(summaries)
        assert cp["estimated_remaining_seconds"] == n, "reprocess = full n × per_item, not 0"
        assert cp["expected_to_process"] == n  # nothing subtracted as a free skip

    def test_rolling_estimate_scopes_to_media_type_filter(self, tmp_path, monkeypatch):
        # (f) #208 review (P2): under --image-only/--video-only the rolling
        # estimate must scope remaining_files to the filtered media type.
        # total_found (count phase) counts only the selected type; file_count
        # (main loop) is bumped for EVERY walked file — including the ones the
        # media-type filter skips. Subtracting raw file_count clamps remaining
        # to 0 the moment out-of-type files are walked past, so the ETA reports
        # "0s remaining" while in-type work is still queued. Here 3 videos
        # (skipped under image_only, and sorted before the images) are walked
        # first, then 2 images: the first per-image commit boundary must still
        # show a nonzero ETA because one image remains.
        from msa_indexer import pipeline

        media_dir = tmp_path / "photos"
        media_dir.mkdir()
        # Real jpgs so processing succeeds; fake mp4s are recognized by
        # extension and skipped (image_only) before any decode. "aaa_*" sorts
        # before "img_*" so file_count races ahead of the image-only
        # total_found — the exact scope mismatch under test.
        images = _make_images(media_dir, 2, prefix="img")
        for i in range(3):
            (media_dir / f"aaa_{i:03d}.mp4").write_bytes(b"\x00\x00\x00\x18ftypmp42")

        qdrant = _FakeQdrant()
        _patch_pipeline_common(monkeypatch, pipeline, qdrant)
        _ordered_walk(monkeypatch, pipeline)

        # Deterministic per-image work: a fake perf_counter clock that only
        # advances (by 10s) inside the embedder — the single call made per
        # processed image between file_start_time and file_elapsed — so each
        # image accrues exactly 10s of observed work and skipped videos accrue
        # nothing. (datetime.* is imported separately, so this only affects the
        # pipeline's perf_counter timing.)
        clock = SimpleNamespace(t=0.0)
        # run_index does a local `import time`, so patch the stdlib module the
        # local name resolves to (there is no pipeline.time attribute).
        monkeypatch.setattr("time.perf_counter", lambda: clock.t)

        class _TickEmbedder(_FakeClipEmbedder):
            def image_embed(self, imgs):
                clock.t += 10.0
                return super().image_embed(imgs)

        monkeypatch.setattr(pipeline, "ClipEmbedder", _TickEmbedder)

        summaries: list[dict] = []
        monkeypatch.setattr(
            pipeline, "_emit_indexer_summary", lambda **p: summaries.append(p)
        )
        # Commit (and thus re-estimate) after every processed file.
        monkeypatch.setenv("MSA_INDEXER_COMMIT_BATCH_FILES", "1")

        config = _pipeline_config(tmp_path, media_dir, image_only=True)
        pipeline.run_index(config)

        cp = self._count_phase(summaries)
        assert cp["total_found"] == len(images), "count phase is filtered to images"

        rolling = self._rolling(summaries)
        assert len(rolling) == len(images), "one rolling re-estimate per processed image"
        # After image #1: one image still queued × 10s observed = 10s remaining.
        # The buggy (unscoped) subtraction reports 0 here — that's the bug.
        assert rolling[0]["estimated_remaining_seconds"] == 10, (
            "rolling ETA must not clamp to 0 while in-type work remains"
        )
        # After the last image, nothing in-scope remains.
        assert rolling[-1]["estimated_remaining_seconds"] == 0

    def test_count_phase_keeps_stat_match_needing_embeddings(self, tmp_path, monkeypatch):
        # (g) #208 review (Codex P2): a fingerprint stat-match is a FREE skip
        # only when the media is already fully processed under the CURRENT
        # config. After a model_version bump every stat-matching file still
        # needs re-embedding — the fast path avoids the RE-HASH, but the embed
        # WORK still runs — so the count phase must NOT subtract it. Otherwise a
        # full re-embed of an unchanged library opens by reporting ~0s while it
        # re-embeds the whole library (the exact "misleading ETA" #208 fixes,
        # in reverse).
        pipeline, config, _d, paths, summaries = self._setup(tmp_path, monkeypatch)
        pipeline.run_index(config)  # embeds at model_version test-v1
        config.model_version = "test-v2"  # bump ⇒ needs_embeddings for every file
        summaries.clear()
        pipeline.run_index(config)

        n = len(paths)
        cp = self._count_phase(summaries)
        assert cp["expected_to_process"] == n, "stat-match needing re-embed is not free"
        assert cp["estimated_remaining_seconds"] == n, "must not collapse to 0"

    def test_count_phase_gates_free_skip_by_enabled_stage(self, tmp_path, monkeypatch):
        # (h) #208 review (Codex P2): whether an incomplete stage blocks the
        # free-skip subtraction depends on whether that stage is ENABLED this
        # session. object_detection_done stays 0 after an index run with
        # detection OFF, yet the main loop does no object work then, so the
        # stat-match is genuinely free. Flip detection ON and the same 0 now
        # means real work is queued — no longer free. The count phase must gate
        # on enablement, not on the raw *_done flag, or a library indexed
        # without detection would never subtract and re-inflate the ETA.
        from msa_indexer import pipeline

        class _NoopDetector:  # recognized detector that finds nothing
            def __init__(self, *a, **k):
                pass

            def get_labels(self, _img):
                return []

        pipeline, config, _d, paths, summaries = self._setup(tmp_path, monkeypatch)
        monkeypatch.setattr(pipeline, "ObjectDetector", _NoopDetector)
        pipeline.run_index(config)  # detection OFF ⇒ object_detection_done stays 0

        n = len(paths)

        # (i) detection still OFF — an incomplete-but-disabled object stage does
        # not block the subtraction.
        summaries.clear()
        pipeline.run_index(config)
        cp_off = self._count_phase(summaries)
        assert cp_off["expected_to_process"] == 0, "disabled stage ⇒ stat-match is free"
        assert cp_off["estimated_remaining_seconds"] == 0

        # (ii) enable detection — the same incomplete object stage now means real
        # work on every stat-matching file, so none are free.
        config.enable_object_detection = True
        summaries.clear()
        pipeline.run_index(config)
        cp_on = self._count_phase(summaries)
        assert cp_on["expected_to_process"] == n, "enabled + incomplete stage ⇒ not free"
        assert cp_on["estimated_remaining_seconds"] == n

    def test_rolling_estimate_counts_no_result_processing(self, tmp_path, monkeypatch):
        # (i) #208 review (Codex P2): a batch that spends real time producing NO
        # result parts (--reprocess-objects that finds 0 labels, --reprocess-
        # faces on faceless images) used to leave observed_work at 0 —
        # total_img_time/total_vid_time accrue only inside the non-empty result
        # `parts` block — so the rolling summary overwrote the ETA with 0 while
        # work was still running. Observed work must be true per-file wall-clock
        # over PROCESSED files, result parts or not.
        from msa_indexer import pipeline

        media_dir = tmp_path / "photos"
        media_dir.mkdir()
        images = _make_images(media_dir, 3, prefix="img")

        qdrant = _FakeQdrant()
        _patch_pipeline_common(monkeypatch, pipeline, qdrant)
        _ordered_walk(monkeypatch, pipeline)
        monkeypatch.setattr(pipeline, "sha256_of_file", _CountingSha())

        # Fake detector that finds NOTHING but ticks a fake perf_counter clock:
        # real elapsed time, zero result parts. Only get_labels advances the
        # clock, so each processed image accrues exactly 7s (the DELTA around
        # file_start_time is what the rolling ETA measures, independent of the
        # absolute clock carried over from the populate run).
        clock = SimpleNamespace(t=0.0)
        monkeypatch.setattr("time.perf_counter", lambda: clock.t)

        class _EmptyTickDetector:
            def __init__(self, *a, **k):
                pass

            def get_labels(self, _img):
                clock.t += 7.0
                return []

        monkeypatch.setattr(pipeline, "ObjectDetector", _EmptyTickDetector)

        summaries: list[dict] = []
        monkeypatch.setattr(
            pipeline, "_emit_indexer_summary", lambda **p: summaries.append(p)
        )
        monkeypatch.setenv("MSA_INDEXER_COMMIT_BATCH_FILES", "1")

        # Run 1: populate with object detection ON (marks object_detection_done).
        config = _pipeline_config(tmp_path, media_dir, enable_object_detection=True)
        pipeline.run_index(config)

        # Run 2: reprocess objects — every image is processed and finds no
        # labels ⇒ zero result parts, but 7s of real work each.
        config.reprocess_objects = True
        summaries.clear()
        pipeline.run_index(config)

        rolling = self._rolling(summaries)
        assert rolling, "expected rolling re-estimates at commit boundaries"
        # After image #1: 7s observed, 2 images remain × 7s = 14s. The bug
        # (observed_work tied to result parts) would report 0 here.
        assert rolling[0]["estimated_remaining_seconds"] == 14, (
            "no-result processing must still advance observed work"
        )
        # After the last image, nothing remains.
        assert rolling[-1]["estimated_remaining_seconds"] == 0

    def test_count_phase_subtracts_all_duplicate_paths(self, tmp_path, monkeypatch):
        # (j) #208 review (Codex P2): resolve completeness by media_id, not
        # rel_path. Byte-identical copies share ONE media_id — so ONE media row
        # (keyed on the canonical rel_path) but a fingerprint row PER path. The
        # main loop skips every copy because it keys the skip off
        # fp["media_id"]. The count phase must match: an unchanged
        # duplicate-heavy library reports ETA ~0, not a near-library-sized
        # estimate. A rel_path-keyed snapshot would find only the canonical
        # path and charge every other copy as work.
        import shutil
        from msa_indexer import pipeline

        media_dir = tmp_path / "photos"
        media_dir.mkdir()
        # One distinct image, then 3 byte-identical copies → 4 walked paths,
        # ONE media_id, ONE media row, FOUR fingerprint rows.
        base = _make_images(media_dir, 1)[0]
        all_paths = [base]
        for i in range(3):
            dup = media_dir / f"dup_{i:03d}.jpg"
            shutil.copyfile(base, dup)
            all_paths.append(dup)

        qdrant = _FakeQdrant()
        _patch_pipeline_common(monkeypatch, pipeline, qdrant)
        _ordered_walk(monkeypatch, pipeline)
        monkeypatch.setattr(pipeline, "sha256_of_file", _CountingSha())
        summaries: list[dict] = []
        monkeypatch.setattr(
            pipeline, "_emit_indexer_summary", lambda **p: summaries.append(p)
        )
        config = _pipeline_config(tmp_path, media_dir)

        pipeline.run_index(config)  # populate: 1 media row, 4 fingerprint rows

        # Sanity: exactly one live media row for the shared content, one
        # fingerprint row per walked path.
        assert len(_media_rows(config.sqlite_path)) == 1
        assert len(_fp_rows(config.sqlite_path)) == len(all_paths)

        summaries.clear()
        pipeline.run_index(config)  # incremental: every copy is a fast-path hit

        n = len(all_paths)
        cp = self._count_phase(summaries)
        assert cp["total_found"] == n, "every duplicate path is walked"
        assert cp["expected_to_process"] == 0, (
            "every live path of a complete media_id must subtract as a free skip"
        )
        assert cp["estimated_remaining_seconds"] == 0, "must NOT be library-sized"

    # -- Video count-phase completeness: SHARED with the runtime video branch --
    #
    # #208 round-4 (Codex P2), STRUCTURAL. Videos are never stamped complete via
    # media.embeddings_version (mark_embeddings_done runs ONLY in the image
    # branch), so the image-style `embeddings_version != model_version` gate
    # reads "needs embeddings" for EVERY video forever — the old count phase
    # therefore charged every stat-matched video at hist_vid_time (a library-
    # sized over-estimate). The runtime instead skips a video from its
    # keyframe/shot state, and the count phase now asks the SAME shared
    # pipeline._video_skip_predicate. These build the DB state directly (the
    # harness does not stub real video decode) and exercise both the snapshot
    # query (get_processing_snapshot_for_media_ids carries has_shots /
    # has_keyframes / has_unembedded_keyframes) and the decision helper.

    @staticmethod
    def _make_video_db(tmp_path, *, with_kf_embeddings, with_keyframes=True,
                       with_shots=True, source_name="photos", media_id="vid-001"):
        db = SQLiteStore(tmp_path / "media.sqlite", autocommit=True)
        db.init_schema(SCHEMA_PATH)
        # embeddings_version is deliberately LEFT NULL — the whole point: a video
        # is complete despite a stale/NULL version. gps + faces marked done so
        # only the keyframe/embedding state decides the free skip.
        db.upsert_media({
            "media_id": media_id,
            "path": f"/src/{media_id}.mp4",
            "source_name": source_name,
            "rel_path": f"{media_id}.mp4",
            "mime": "video/mp4",
            "gps_processed": 1,
            "face_detection_done": 1,
        })
        if with_shots:
            db.add_shots(media_id, [(0.0, 5.0)])
        if with_keyframes:
            db.add_keyframes(media_id, [{
                "shot_index": 0, "kf_index": 0, "timestamp": 1.0,
                "shot_start": 0.0, "shot_end": 5.0,
            }])
            if with_kf_embeddings:
                kf_id = db.get_keyframe_id(media_id, 0, 0)
                db.upsert_keyframe_embedding(
                    kf_id, np.zeros(8, dtype=np.float32), model="test-v1"
                )
        return db, media_id

    @staticmethod
    def _video_free_skip(db, media_id):
        from msa_indexer.pipeline import _count_phase_expects_free_skip
        snap = db.get_processing_snapshot_for_media_ids([media_id])
        assert media_id in snap
        return snap[media_id], _count_phase_expects_free_skip(
            snap[media_id], model_version="test-v1", is_image=False, is_video=True,
            objects_enabled=True, faces_enabled=True,
        )

    def test_count_phase_video_complete_keyframes_is_free_skip(self, tmp_path):
        # (k) An unchanged video with shots + keyframes + EVERY keyframe embedded
        # is a free skip — NOT charged at hist_vid_time — even though its
        # embeddings_version is NULL. The runtime video branch skips exactly this
        # state; the count phase now asks the SAME shared _video_skip_predicate.
        # object_detection_done is left 0 (a video indexed with video object
        # detection off): keyframes-present ⇒ the runtime never re-runs object
        # detection, so an un-done object stage does NOT block the free skip —
        # ending the old video_objects_enabled gate that used to (over-)charge it.
        db, mid = self._make_video_db(tmp_path, with_kf_embeddings=True)
        try:
            snap, free = self._video_free_skip(db, mid)
            assert snap["embeddings_version"] is None, "video is NOT stamped complete"
            assert snap["has_shots"] and snap["has_keyframes"]
            assert not snap["has_unembedded_keyframes"]
            assert free is True, "complete video is a free skip despite NULL version"
        finally:
            db.close()

    def test_count_phase_video_missing_keyframe_embeddings_is_charged(self, tmp_path):
        # (l) The other half: a video with keyframes but at least one MISSING
        # keyframe embedding (a pre-Stage-3 upgrade) IS work — the runtime
        # re-embeds those keyframes — so the count phase must NOT subtract it.
        db, mid = self._make_video_db(tmp_path, with_kf_embeddings=False)
        try:
            snap, free = self._video_free_skip(db, mid)
            assert snap["has_keyframes"] and snap["has_unembedded_keyframes"]
            assert free is False, "unembedded keyframes ⇒ real work ⇒ not free"
        finally:
            db.close()

    def test_count_phase_video_without_keyframes_is_charged(self, tmp_path):
        # (m) A video with no keyframes at all is never a free skip — the runtime
        # extracts + embeds keyframes (has_keyframes gates the shared predicate).
        db, mid = self._make_video_db(
            tmp_path, with_kf_embeddings=False, with_keyframes=False
        )
        try:
            snap, free = self._video_free_skip(db, mid)
            assert not snap["has_keyframes"]
            assert free is False
        finally:
            db.close()

    def test_video_object_detection_gate_helper(self):
        # (n) The shared video-object config gate still needs BOTH object
        # detection AND the video sub-flag (default off). It no longer feeds the
        # count-phase free-skip decision (the keyframe-state predicate subsumes
        # it), but the main loop's enable_video_detection still consults it.
        from msa_indexer.pipeline import _video_object_detection_enabled

        assert _video_object_detection_enabled(
            SimpleNamespace(enable_object_detection=True, device="cuda")
        ) is False, "enable_video_object_detection defaults off"
        assert _video_object_detection_enabled(
            SimpleNamespace(enable_object_detection=True, device="cuda",
                            enable_video_object_detection=True)
        ) is True
        assert _video_object_detection_enabled(
            SimpleNamespace(enable_object_detection=False, device="cuda",
                            enable_video_object_detection=True)
        ) is False, "no detector at all ⇒ no video object work"

    def test_count_phase_cross_source_duplicate_is_free_skip(self, tmp_path, monkeypatch):
        # (o) #208 round-4 (Codex P2): a cross-source duplicate. The SAME bytes
        # live under source A and source B. Content-addressed, they share ONE
        # media_id and ONE media row — canonical under whichever source indexed
        # it first (A) — while EACH source owns a fingerprint row pointing at
        # that media_id. The main loop reuses fp["media_id"] and skips B's copy;
        # the count phase must too. Loading B's snapshot filtered on
        # media.source_name = B would MISS the media_id (its row says A) and
        # re-charge B's copy as work; loading by the media_id SET B's
        # fingerprints reference resolves it.
        import shutil
        from msa_indexer import pipeline

        src_a = tmp_path / "src_a"
        src_b = tmp_path / "src_b"
        src_a.mkdir()
        src_b.mkdir()
        img = _make_images(src_a, 1)[0]
        shutil.copyfile(img, src_b / "same.jpg")  # byte-identical ⇒ same media_id

        qdrant = _FakeQdrant()
        _patch_pipeline_common(monkeypatch, pipeline, qdrant)
        _ordered_walk(monkeypatch, pipeline)
        monkeypatch.setattr(pipeline, "sha256_of_file", _CountingSha())
        summaries: list[dict] = []
        monkeypatch.setattr(
            pipeline, "_emit_indexer_summary", lambda **p: summaries.append(p)
        )
        config = _pipeline_config(tmp_path, src_a)
        # A is listed first so it owns the canonical media row.
        config.media_sources = [
            SimpleNamespace(name="src_a", path=str(src_a), enabled=True),
            SimpleNamespace(name="src_b", path=str(src_b), enabled=True),
        ]

        pipeline.run_index(config)  # run 1: X canonical in src_a, fingerprints in A & B

        # Setup sanity: one media row (canonical in src_a) + one fingerprint per
        # source, both pointing at the shared media_id.
        media = _media_rows(config.sqlite_path)
        assert len(media) == 1 and media[0]["source_name"] == "src_a"
        fps = _fp_rows(config.sqlite_path)
        assert {f["source_name"] for f in fps} == {"src_a", "src_b"}
        assert {f["media_id"] for f in fps} == {media[0]["media_id"]}

        summaries.clear()
        pipeline.run_index(config)  # run 2: both copies are fast-path hits

        cp = self._count_phase(summaries)
        assert cp["total_found"] == 2, "both copies are walked"
        assert cp["expected_to_process"] == 0, (
            "the cross-source duplicate must subtract as a free skip in src_b"
        )
        assert cp["estimated_remaining_seconds"] == 0
