"""M-8/S-3 — delta Qdrant export + tombstone propagation.

Covers the §6.1 S-3 test list of
internal/docs/indexer/M8_INCREMENTAL_INDEXING_PLAN.md:

- R10 additive migration from a pre-S-3 fixture DB (updated_seq /
  deleted_seq / s3_migration_seq / face_recreate_required), idempotent,
  pre-existing tombstones seeded at index_version_seq + 1
- §4.1 per-row dirty stamping: every payload-relevant write stamps the
  right rows (R7, one test per field group), locked by the payload-builder
  coverage test (derivation rule)
- §4.1 stamp allocation: run-scoped pending_seq vs out-of-band
  index_version_seq + 1 (transactional, rolls back with the write) —
  including the #204 healing proof
- §4.2 delta exporters (since_seq filter), the unconditional deletion
  pass, the widened watermark gate (face/deletion failure blocks the
  record — R8), 0-dirty no-op vs pre-Stage-3 empty tables
- §4.2 export decision: dirty-row trigger + first-delta-run guard +
  watermark advance (G8 crash simulation), face_recreate_required
  durability

Store-level tests use a real SQLite DB in tmp_path. Export-level tests use
a REAL embedded (local-mode) qdrant-client at tmp_path — notably because
local-mode `delete()` of an absent point id raises KeyError rather than
no-oping like the server, which the deletion pass must tolerate. Pipeline
tests follow the tests/test_fingerprint_fastpath.py harness (monkeypatched
sha256/embedder/exporters, SimpleNamespace config, real tmp media files)
with a REAL SQLiteStore underneath.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest
from PIL import Image

from msa_indexer.db.sqlite_store import SQLiteStore

SCHEMA_PATH = (
    Path(__file__).parent.parent / "src" / "msa_indexer" / "db" / "schema.sql"
)


# ---------------------------------------------------------------------------
# Frozen pre-S-3 schema (R10 fixture)
#
# A verbatim snapshot of schema.sql as it stood after M-8/S-1 but before
# S-3: no updated_seq on the embedding tables, no media.deleted_seq, no
# s3_migration_seq / face_recreate_required on index_state. Used to build a
# pre-upgrade DB in-test and assert the S-3 migration path.
# ---------------------------------------------------------------------------

_PRE_S3_SCHEMA = """
PRAGMA journal_mode=WAL;
CREATE TABLE IF NOT EXISTS media (
  media_id TEXT PRIMARY KEY,
  path TEXT NOT NULL,
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
  embeddings_version TEXT,
  missing_since_scan_id INTEGER
);
CREATE INDEX IF NOT EXISTS idx_media_source_rel ON media(source_name, rel_path);
CREATE INDEX IF NOT EXISTS idx_media_path ON media(path);
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
CREATE TABLE IF NOT EXISTS scan_run (
  scan_id      INTEGER PRIMARY KEY AUTOINCREMENT,
  started_at   TEXT DEFAULT (datetime('now')),
  completed_at TEXT,
  sources_json TEXT
);
CREATE TABLE IF NOT EXISTS file_fingerprint (
  source_name        TEXT NOT NULL,
  rel_path           TEXT NOT NULL,
  size_bytes         INTEGER NOT NULL,
  mtime_ns           INTEGER NOT NULL,
  media_id           TEXT NOT NULL,
  last_seen_scan_id  INTEGER,
  missing_since_scan_id INTEGER,
  PRIMARY KEY (source_name, rel_path),
  FOREIGN KEY (media_id) REFERENCES media(media_id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_fp_media ON file_fingerprint(media_id);
CREATE INDEX IF NOT EXISTS idx_fp_seen ON file_fingerprint(source_name, last_seen_scan_id);
"""


def _build_pre_s3_db(db_path: Path, index_version_seq: int = 3) -> None:
    """A populated post-S-1 / pre-S-3 database: live media with embeddings,
    a legacy tombstone (deleted before S-3, so unstamped), a video with
    keyframes, and a labeled face."""
    conn = sqlite3.connect(str(db_path))
    try:
        conn.executescript(_PRE_S3_SCHEMA)
        conn.execute(
            "INSERT INTO index_state(singleton_id, index_version_seq, index_version_ts)"
            " VALUES (1, ?, '2026-01-01T00:00:00Z')",
            (index_version_seq,),
        )
        conn.execute(
            "INSERT INTO media(media_id, path, source_name, rel_path, mime, deleted)"
            " VALUES ('m-live', '/photos/a.jpg', 'photos', 'a.jpg', 'image/jpg', 0)"
        )
        conn.execute(
            "INSERT INTO media(media_id, path, source_name, rel_path, mime, deleted)"
            " VALUES ('m-tomb', '/photos/gone.jpg', 'photos', 'gone.jpg', 'image/jpg', 1)"
        )
        conn.execute(
            "INSERT INTO media(media_id, path, source_name, rel_path, mime, deleted)"
            " VALUES ('v-live', '/photos/b.mp4', 'photos', 'b.mp4', 'video/mp4', 0)"
        )
        conn.execute(
            "INSERT INTO image_embedding(media_id, embedding, embedding_dim, embedding_model)"
            " VALUES ('m-live', x'00000000', 1, 'clip-test')"
        )
        conn.execute(
            "INSERT INTO video_keyframes(video_id, shot_index, kf_index, timestamp, shot_start, shot_end)"
            " VALUES ('v-live', 0, 0, 0.5, 0.0, 1.0)"
        )
        kf_id = conn.execute(
            "SELECT id FROM video_keyframes WHERE video_id='v-live'"
        ).fetchone()[0]
        conn.execute(
            "INSERT INTO keyframe_embedding(keyframe_id, embedding, embedding_dim, embedding_model)"
            " VALUES (?, x'00000000', 1, 'clip-test')",
            (kf_id,),
        )
        conn.execute(
            "INSERT INTO person(person_id, name, is_labeled) VALUES ('p-1', 'Alice', 1)"
        )
        conn.execute(
            "INSERT INTO face(face_id, media_id, x, y, w, h, confidence, person_id)"
            " VALUES ('m-live:f0', 'm-live', 0.1, 0.1, 0.2, 0.2, 0.99, 'p-1')"
        )
        conn.execute(
            "INSERT INTO face_embedding(face_id, embedding, embedding_dim, embedding_model)"
            " VALUES ('m-live:f0', x'00000000', 1, 'face-test')"
        )
        conn.commit()
    finally:
        conn.close()


def _columns(db_path: Path, table: str) -> list[str]:
    conn = sqlite3.connect(str(db_path))
    try:
        return [r[1] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()]
    finally:
        conn.close()


def _scalar(db_or_path, sql: str, params: tuple = ()):
    if isinstance(db_or_path, SQLiteStore):
        row = db_or_path.conn.execute(sql, params).fetchone()
        return row[0] if row else None
    conn = sqlite3.connect(str(db_or_path))
    try:
        row = conn.execute(sql, params).fetchone()
        return row[0] if row else None
    finally:
        conn.close()


@pytest.fixture
def store(tmp_path: Path):
    db = SQLiteStore(tmp_path / "media.sqlite", autocommit=True)
    db.init_schema(SCHEMA_PATH)
    yield db
    db.close()


def _insert_media(
    db: SQLiteStore,
    media_id: str,
    mime: str = "image/jpg",
    deleted: int = 0,
    path: str | None = None,
):
    db.conn.execute(
        "INSERT INTO media(media_id, path, source_name, rel_path, mime, deleted)"
        " VALUES (?, ?, 'photos', ?, ?, ?)",
        (media_id, path or f"/photos/{media_id}.x", f"{media_id}.x", mime, deleted),
    )
    db.commit()


def _add_image_with_embedding(db: SQLiteStore, media_id: str):
    _insert_media(db, media_id, mime="image/jpg")
    db.upsert_image_embedding(media_id, np.zeros(4, dtype=np.float32), model="clip-test")


def _add_video_with_keyframes(db: SQLiteStore, media_id: str, n_kf: int = 2) -> list[int]:
    _insert_media(db, media_id, mime="video/mp4", path=f"/photos/{media_id}.mp4")
    db.add_keyframes(
        media_id,
        [
            {
                "shot_index": 0,
                "kf_index": i,
                "timestamp": float(i),
                "shot_start": 0.0,
                "shot_end": float(n_kf),
            }
            for i in range(n_kf)
        ],
    )
    kf_ids = []
    for i in range(n_kf):
        kf_id = db.get_keyframe_id(media_id, 0, i)
        db.upsert_keyframe_embedding(kf_id, np.zeros(4, dtype=np.float32), model="clip-test")
        kf_ids.append(kf_id)
    return kf_ids


def _add_face_with_embedding(
    db: SQLiteStore, media_id: str, face_id: str, person_id: str | None = None
):
    db.add_faces(
        media_id,
        [
            {
                "face_id": face_id,
                "bbox": (0.1, 0.1, 0.2, 0.2),
                "confidence": 0.9,
                "person_id": person_id,
            }
        ],
    )
    db.upsert_face_embedding(face_id, np.zeros(4, dtype=np.float32), model="face-test")


def _updated_seq(db: SQLiteStore, table: str, key_col: str, key) -> int:
    row = db.conn.execute(
        f"SELECT updated_seq FROM {table} WHERE {key_col} = ?", (key,)
    ).fetchone()
    assert row is not None, f"no {table} row for {key}"
    return int(row[0])


# ---------------------------------------------------------------------------
# R10: additive migration from a pre-S-3 fixture DB
# ---------------------------------------------------------------------------


class TestS3Migration:
    def test_migration_is_additive_and_complete(self, tmp_path):
        db_path = tmp_path / "media.sqlite"
        _build_pre_s3_db(db_path)
        before_media = _scalar(db_path, "SELECT COUNT(*) FROM media")

        db = SQLiteStore(db_path)
        db.init_schema(SCHEMA_PATH)
        db.close()

        for table in ("image_embedding", "keyframe_embedding", "face_embedding"):
            assert "updated_seq" in _columns(db_path, table)
        assert "deleted_seq" in _columns(db_path, "media")
        assert "s3_migration_seq" in _columns(db_path, "index_state")
        assert "face_recreate_required" in _columns(db_path, "index_state")
        # No rows lost, embeddings intact
        assert _scalar(db_path, "SELECT COUNT(*) FROM media") == before_media
        assert _scalar(db_path, "SELECT COUNT(*) FROM image_embedding") == 1
        assert _scalar(db_path, "SELECT COUNT(*) FROM keyframe_embedding") == 1
        assert _scalar(db_path, "SELECT COUNT(*) FROM face_embedding") == 1
        # Covering indexes exist
        conn = sqlite3.connect(str(db_path))
        try:
            names = {
                r[0]
                for r in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='index'"
                ).fetchall()
            }
        finally:
            conn.close()
        for idx in (
            "idx_image_emb_updated",
            "idx_kf_emb_updated",
            "idx_face_emb_updated",
            "idx_media_deleted_seq",
        ):
            assert idx in names, f"missing covering index {idx}"

    def test_default_zero_means_already_in_qdrant(self, tmp_path):
        db_path = tmp_path / "media.sqlite"
        _build_pre_s3_db(db_path)
        db = SQLiteStore(db_path)
        db.init_schema(SCHEMA_PATH)
        try:
            assert _updated_seq(db, "image_embedding", "media_id", "m-live") == 0
            assert _updated_seq(db, "face_embedding", "face_id", "m-live:f0") == 0
            # Legacy live rows are NOT selected by a delta filter at the
            # migration-time watermark; only the seeded tombstone is dirty.
            tombs = db.iter_stamped_tombstones(3)
            assert [t["media_id"] for t in tombs] == ["m-tomb"]
        finally:
            db.close()

    def test_preexisting_tombstones_seeded_above_watermark(self, tmp_path):
        db_path = tmp_path / "media.sqlite"
        _build_pre_s3_db(db_path, index_version_seq=3)
        db = SQLiteStore(db_path)
        db.init_schema(SCHEMA_PATH)
        try:
            # Seeded at index_version_seq + 1 — ABOVE the watermark, not at
            # it: in-sync-at-migration (exported == 3) must still select it.
            assert (
                _scalar(db, "SELECT deleted_seq FROM media WHERE media_id='m-tomb'")
                == 4
            )
            assert db.dirty_rows_exist(3) is True
            tombs = db.iter_stamped_tombstones(3)
            assert [t["media_id"] for t in tombs] == ["m-tomb"]
        finally:
            db.close()

    def test_s3_migration_seq_seeded_once_never_reseeded(self, tmp_path):
        db_path = tmp_path / "media.sqlite"
        _build_pre_s3_db(db_path, index_version_seq=3)
        db = SQLiteStore(db_path)
        db.init_schema(SCHEMA_PATH)
        assert db.get_s3_state()["s3_migration_seq"] == 3
        # Advance the version, re-run the migration: seed must NOT move.
        db.bump_index_version()
        db.init_schema(SCHEMA_PATH)
        assert db.get_s3_state()["s3_migration_seq"] == 3
        db.close()

    def test_migration_idempotent_on_rerun(self, tmp_path):
        db_path = tmp_path / "media.sqlite"
        _build_pre_s3_db(db_path)
        for _ in range(3):
            db = SQLiteStore(db_path)
            db.init_schema(SCHEMA_PATH)
            db.close()
        # Single deleted_seq/updated_seq column, tombstone seed unchanged
        assert _columns(db_path, "media").count("deleted_seq") == 1
        assert _columns(db_path, "image_embedding").count("updated_seq") == 1
        assert _scalar(db_path, "SELECT deleted_seq FROM media WHERE media_id='m-tomb'") == 4

    def test_fresh_db_defaults(self, store):
        state = store.get_s3_state()
        assert state["s3_migration_seq"] == 0
        assert state["face_recreate_required"] is False


# ---------------------------------------------------------------------------
# §4.1 stamp infrastructure: tombstone / resurrect
# ---------------------------------------------------------------------------


class TestTombstoneStamping:
    def test_every_tombstone_transition_carries_a_stamp(self, store):
        _add_image_with_embedding(store, "m-1")
        store.stamp_seq = 7  # run-scoped allocation
        store.tombstone_media("m-1")
        assert _scalar(store, "SELECT deleted_seq FROM media WHERE media_id='m-1'") == 7
        assert _scalar(store, "SELECT deleted FROM media WHERE media_id='m-1'") == 1

    def test_out_of_band_tombstone_allocates_seq_plus_one(self, store):
        _add_image_with_embedding(store, "m-1")
        store.bump_index_version()  # seq -> 1
        assert store.stamp_seq is None
        store.tombstone_media("m-1")
        assert _scalar(store, "SELECT deleted_seq FROM media WHERE media_id='m-1'") == 2

    def test_resurrect_clears_stamp_and_stamps_embedding_rows(self, store):
        _add_image_with_embedding(store, "m-1")
        _add_face_with_embedding(store, "m-1", "m-1:f0")
        store.conn.execute("UPDATE image_embedding SET updated_seq = 0")
        store.conn.execute("UPDATE face_embedding SET updated_seq = 0")
        store.commit()
        store.stamp_seq = 5
        store.tombstone_media("m-1")
        store.stamp_seq = 9
        store.resurrect_media("m-1")
        assert _scalar(store, "SELECT deleted_seq FROM media WHERE media_id='m-1'") is None
        assert _scalar(store, "SELECT deleted FROM media WHERE media_id='m-1'") == 0
        # Reactivation re-upsert: rows stamped so the next delta sends them
        assert _updated_seq(store, "image_embedding", "media_id", "m-1") == 9
        assert _updated_seq(store, "face_embedding", "face_id", "m-1:f0") == 9

    def test_grace_only_clear_does_not_stamp(self, store):
        _add_image_with_embedding(store, "m-1")
        store.conn.execute("UPDATE image_embedding SET updated_seq = 0")
        store.commit()
        store.set_media_missing_since("m-1", 12)
        store.stamp_seq = 9
        store.resurrect_media("m-1")  # deleted was 0 — pure grace clear
        assert _updated_seq(store, "image_embedding", "media_id", "m-1") == 0, (
            "a grace-only clear must not dirty the row (R1: no-op runs stay no-ops)"
        )


class TestDirtyProbes:
    def test_dirty_rows_exist_default_zero_semantics(self, store):
        _add_image_with_embedding(store, "m-1")
        store.conn.execute("UPDATE image_embedding SET updated_seq = 0")
        store.commit()
        # DEFAULT 0 = "already in Qdrant": not dirty against watermark 0+
        assert store.dirty_rows_exist(0) is False
        # ...but dirty when no export record exists (floor -1)
        assert store.dirty_rows_exist(-1) is True

    def test_dirty_probe_sees_each_table_and_tombstones(self, store):
        _add_image_with_embedding(store, "m-img")
        _add_video_with_keyframes(store, "m-vid")
        _add_face_with_embedding(store, "m-img", "m-img:f0")
        store.conn.execute("UPDATE image_embedding SET updated_seq = 0")
        store.conn.execute("UPDATE keyframe_embedding SET updated_seq = 0")
        store.conn.execute("UPDATE face_embedding SET updated_seq = 0")
        store.commit()
        assert store.dirty_rows_exist(0) is False
        for sql in (
            "UPDATE image_embedding SET updated_seq = 5 WHERE media_id='m-img'",
            "UPDATE keyframe_embedding SET updated_seq = 5",
            "UPDATE face_embedding SET updated_seq = 5",
        ):
            store.conn.execute(sql)
            store.commit()
            assert store.dirty_rows_exist(4) is True
            assert store.dirty_rows_exist(5) is False
            store.conn.execute(
                sql.replace("SET updated_seq = 5", "SET updated_seq = 0")
            )
            store.commit()
        store.stamp_seq = 5
        store.tombstone_media("m-vid")
        assert store.dirty_rows_exist(4) is True
        assert store.dirty_rows_exist(5) is False

    def test_max_stamped_seq_spans_tables_and_tombstones(self, store):
        _add_image_with_embedding(store, "m-1")
        store.conn.execute("UPDATE image_embedding SET updated_seq = 3")
        store.commit()
        assert store.max_stamped_seq() == 3
        store.stamp_seq = 8
        store.tombstone_media("m-1")
        assert store.max_stamped_seq() == 8

    def test_iter_stamped_tombstones_yields_point_id_inputs(self, store):
        kf_ids = _add_video_with_keyframes(store, "m-vid", n_kf=2)
        assert len(kf_ids) == 2
        _add_face_with_embedding(store, "m-vid", "vf:m-vid:0:0:f0")
        store.stamp_seq = 4
        store.tombstone_media("m-vid")
        tombs = store.iter_stamped_tombstones(3)
        assert len(tombs) == 1
        t = tombs[0]
        assert t["media_id"] == "m-vid"
        assert sorted(t["keyframes"]) == [(0, 0), (0, 1)]
        assert t["face_ids"] == ["vf:m-vid:0:0:f0"]
        # At/below the floor: not selected
        assert store.iter_stamped_tombstones(4) == []
        # since_seq=None (full export): every stamped tombstone
        assert len(store.iter_stamped_tombstones(None)) == 1


# ---------------------------------------------------------------------------
# §4.1 payload-builder coverage test — THE derivation-rule lock
# ---------------------------------------------------------------------------


def _labelled_fixture(store: SQLiteStore):
    """Image + video, each with embeddings and a labeled face."""
    store.stamp_seq = 1
    _add_image_with_embedding(store, "m-img")
    _add_video_with_keyframes(store, "m-vid", n_kf=2)
    store.conn.execute(
        "INSERT INTO person(person_id, name, is_labeled) VALUES ('p-1', 'Alice', 1)"
    )
    store.conn.execute(
        "INSERT INTO person(person_id, name, is_labeled) VALUES ('p-2', 'Bob', 1)"
    )
    store.commit()
    _add_face_with_embedding(store, "m-img", "m-img:f0", person_id="p-1")
    _add_face_with_embedding(store, "m-vid", "vf:m-vid:0:0:f0", person_id="p-1")
    _reset_stamps(store)
    store.stamp_seq = 42


def _reset_stamps(store: SQLiteStore):
    store.conn.execute("UPDATE image_embedding SET updated_seq = 0")
    store.conn.execute("UPDATE keyframe_embedding SET updated_seq = 0")
    store.conn.execute("UPDATE face_embedding SET updated_seq = 0")
    store.commit()


def _stamp_snapshot(store: SQLiteStore) -> dict:
    return {
        "image": {
            r[0]: r[1]
            for r in store.conn.execute(
                "SELECT media_id, updated_seq FROM image_embedding"
            ).fetchall()
        },
        "keyframe": {
            r[0]: r[1]
            for r in store.conn.execute(
                "SELECT ke.keyframe_id, ke.updated_seq FROM keyframe_embedding ke"
            ).fetchall()
        },
        "face": {
            r[0]: r[1]
            for r in store.conn.execute(
                "SELECT face_id, updated_seq FROM face_embedding"
            ).fetchall()
        },
    }


class TestPayloadColumnCoverage:
    """The §4.1 derivation-rule lock: builders ↔ PAYLOAD_SOURCES ↔
    STAMP_RULES ↔ actually-stamping store writes."""

    def test_builder_key_sets_match_declared_map(self):
        from msa_indexer.db import payload_columns as pc
        from msa_indexer.db.qdrant_export import (
            build_face_payload,
            build_payload,
            build_video_payload,
        )

        image_row = {
            "id": "m", "path": "/p.jpg", "people": [], "place": "x",
            "ts": "2026-01-01", "added_at": "2026-01-01", "tags": ["cat"],
        }
        assert set(build_payload(image_row)) == set(pc.PAYLOAD_SOURCES["image"]), (
            "image payload keys drifted from PAYLOAD_SOURCES['image'] — update "
            "payload_columns.py AND add stamp rules for any new source columns"
        )

        video_row = {
            "video_id": "v", "path": "/p.mp4", "shot_index": 0, "kf_index": 0,
            "timestamp": 0.5, "shot_start": 0.0, "shot_end": 1.0, "tags": [],
            "gps_lat": None, "gps_lon": None, "gps_alt": None,
            "gps_datetime_utc": None, "gps_fix": None, "gps_source": None,
            "place": None, "people": [],
        }
        assert set(build_video_payload(video_row)) == set(pc.PAYLOAD_SOURCES["video"]), (
            "video payload keys drifted from PAYLOAD_SOURCES['video']"
        )

        face_row = {
            "face_id": "f", "media_id": "m", "path": "/p.jpg", "date": None,
            "bbox": [0, 0, 1, 1], "confidence": 0.9, "person_id": None,
            "person_name": None, "gender": None, "age": None, "type": "image",
            "shot_index": None, "kf_index": None,
        }
        assert set(build_face_payload(face_row)) == set(pc.PAYLOAD_SOURCES["face"]), (
            "face payload keys drifted from PAYLOAD_SOURCES['face']"
        )

    def test_iterators_feed_every_declared_key(self, store):
        """The declared keys must be constructible from real iterator rows —
        guards against PAYLOAD_SOURCES declaring keys the iterators no
        longer supply."""
        from msa_indexer.db import payload_columns as pc
        from msa_indexer.db.qdrant_export import (
            build_face_payload,
            build_payload,
            build_video_payload,
        )

        _labelled_fixture(store)
        img_rows = list(store.iter_items())
        assert img_rows, "iter_items yielded nothing"
        assert set(build_payload(img_rows[0])) == set(pc.PAYLOAD_SOURCES["image"])
        vk_rows = list(store.iter_video_keyframes())
        assert vk_rows, "iter_video_keyframes yielded nothing"
        assert set(build_video_payload(vk_rows[0])) == set(pc.PAYLOAD_SOURCES["video"])
        face_rows = list(store.iter_faces())
        assert face_rows, "iter_faces yielded nothing"
        assert set(build_face_payload(face_rows[0])) == set(pc.PAYLOAD_SOURCES["face"])

    def test_every_source_cell_has_a_stamp_rule(self):
        from msa_indexer.db import payload_columns as pc

        missing = pc.all_source_cells() - set(pc.STAMP_RULES)
        assert missing == set(), (
            f"payload source cells WITHOUT a stamp rule: {sorted(missing)} — "
            "a write to these columns would never re-export the affected "
            "payloads under delta export (R7)"
        )
        orphaned = set(pc.STAMP_RULES) - pc.all_source_cells()
        assert orphaned == set(), (
            f"stamp rules for cells no payload builder reads: {sorted(orphaned)}"
        )

    def test_every_stamp_rule_is_exercised_and_stamps(self, store):
        """Part (iii) of the lock: each rule's store writes observably set
        updated_seq on the rule's declared targets (exercised, not
        introspected)."""
        from msa_indexer.db import payload_columns as pc

        exercised: set[str] = set()

        # media_superset — update_media_fields on a payload column
        _labelled_fixture(store)
        store.update_media_fields("m-vid", {"place": "Lisbon"})
        snap = _stamp_snapshot(store)
        assert all(v == 42 for v in snap["keyframe"].values())
        assert snap["face"]["vf:m-vid:0:0:f0"] == 42
        assert snap["image"]["m-img"] == 0, "other media must stay unstamped"
        store.update_media_fields("m-img", {"ts_utc": "2026-02-02"})
        snap = _stamp_snapshot(store)
        assert snap["image"]["m-img"] == 42
        assert snap["face"]["m-img:f0"] == 42
        exercised.add("media_superset")

        # keyframe_rows — add_keyframes conflict-update refreshes payload cells
        _reset_stamps(store)
        store.add_keyframes(
            "m-vid",
            [{
                "shot_index": 0, "kf_index": 0, "timestamp": 0.0,
                "shot_start": 0.0, "shot_end": 2.0, "tags": ["dog"],
            }],
        )
        snap = _stamp_snapshot(store)
        assert all(v == 42 for v in snap["keyframe"].values())
        assert snap["image"]["m-img"] == 0
        exercised.add("keyframe_rows")

        # face_rows — add_faces conflict-update refreshes payload cells
        _reset_stamps(store)
        store.add_faces(
            "m-vid",
            [{
                "face_id": "vf:m-vid:0:0:f0", "bbox": (0.2, 0.2, 0.3, 0.3),
                "confidence": 0.7, "person_id": "p-1",
            }],
        )
        snap = _stamp_snapshot(store)
        assert snap["face"]["vf:m-vid:0:0:f0"] == 42
        assert all(v == 42 for v in snap["keyframe"].values()), (
            "video payloads embed people — keyframe rows must stamp too"
        )
        exercised.add("face_rows")

        # face_label — update_face_person / clear_face_person
        _reset_stamps(store)
        store.update_face_person("vf:m-vid:0:0:f0", "p-2")
        snap = _stamp_snapshot(store)
        assert snap["face"]["vf:m-vid:0:0:f0"] == 42
        assert all(v == 42 for v in snap["keyframe"].values())
        assert snap["face"]["m-img:f0"] == 0
        exercised.add("face_label")

        # person_rows — rename_person stamps every face of the person
        _reset_stamps(store)
        store.rename_person("p-1", "Alicia")
        snap = _stamp_snapshot(store)
        assert snap["face"]["m-img:f0"] == 42, "every face of the renamed person"
        exercised.add("person_rows")

        assert exercised == set(pc.STAMP_RULES.values()), (
            f"stamp rules never exercised by this test: "
            f"{set(pc.STAMP_RULES.values()) - exercised} — extend the test "
            "when adding a rule"
        )


# ---------------------------------------------------------------------------
# R7: per-field-group stamp tests
# ---------------------------------------------------------------------------


class TestPerFieldGroupStamps:
    def test_media_place_stamps_image_video_and_face_rows(self, store):
        _labelled_fixture(store)
        store.update_media_fields("m-img", {"place": "Porto"})
        store.update_media_fields("m-vid", {"place": "Porto"})
        snap = _stamp_snapshot(store)
        assert snap["image"]["m-img"] == 42
        assert all(v == 42 for v in snap["keyframe"].values())
        assert snap["face"]["m-img:f0"] == 42
        assert snap["face"]["vf:m-vid:0:0:f0"] == 42

    def test_media_ts_utc_stamps_face_rows_too(self, store):
        # face payloads carry the media timestamp as `date`
        _labelled_fixture(store)
        store.update_media_fields("m-img", {"ts_utc": "2026-03-03"})
        assert _updated_seq(store, "face_embedding", "face_id", "m-img:f0") == 42

    def test_media_gps_fields_stamp(self, store):
        _labelled_fixture(store)
        store.update_media_fields("m-img", {"gps_lat": 1.0, "gps_lon": 2.0})
        assert _updated_seq(store, "image_embedding", "media_id", "m-img") == 42

    def test_non_payload_media_fields_do_not_stamp(self, store):
        _labelled_fixture(store)
        store.update_media_fields("m-img", {"gps_data_mode": "none"})
        store.mark_face_detection_done("m-img")
        store.mark_object_detection_done("m-img")
        store.mark_embeddings_done("m-img", "v2")
        store.mark_gps_processed("m-img")
        snap = _stamp_snapshot(store)
        assert snap["image"]["m-img"] == 0, (
            "bookkeeping-only writes must not dirty rows (R1)"
        )

    def test_media_tag_insert_stamps(self, store):
        _labelled_fixture(store)
        store.add_tags("m-img", ["beach"])
        assert _updated_seq(store, "image_embedding", "media_id", "m-img") == 42
        # video payloads fall back to media-level tags
        _reset_stamps(store)
        store.add_tags("m-vid", ["beach"])
        snap = _stamp_snapshot(store)
        assert all(v == 42 for v in snap["keyframe"].values())

    def test_vk_level_tags_gps_place_stamp_that_video(self, store):
        _labelled_fixture(store)
        store.add_keyframes(
            "m-vid",
            [{
                "shot_index": 0, "kf_index": 1, "timestamp": 1.0,
                "shot_start": 0.0, "shot_end": 2.0,
                "gps_lat": 3.0, "gps_lon": 4.0, "place": "Faro",
            }],
        )
        snap = _stamp_snapshot(store)
        assert all(v == 42 for v in snap["keyframe"].values())
        assert snap["image"]["m-img"] == 0

    def test_face_label_on_image_and_video(self, store):
        _labelled_fixture(store)
        store.update_face_person("m-img:f0", "p-2")
        snap = _stamp_snapshot(store)
        assert snap["face"]["m-img:f0"] == 42
        # image media has no keyframes — none stamped
        assert all(v == 0 for v in snap["keyframe"].values())
        _reset_stamps(store)
        store.clear_face_person("vf:m-vid:0:0:f0")
        snap = _stamp_snapshot(store)
        assert snap["face"]["vf:m-vid:0:0:f0"] == 42
        assert all(v == 42 for v in snap["keyframe"].values()), (
            "labeling a video face must stamp that video's keyframe rows "
            "(keyframe payloads embed people names)"
        )

    def test_batch_label_stamps_all_faces(self, store):
        _labelled_fixture(store)
        n = store.update_faces_person_batch(["m-img:f0", "vf:m-vid:0:0:f0"], "p-2")
        assert n == 2
        snap = _stamp_snapshot(store)
        assert snap["face"]["m-img:f0"] == 42
        assert snap["face"]["vf:m-vid:0:0:f0"] == 42
        assert all(v == 42 for v in snap["keyframe"].values())

    def test_person_rename_stamps_every_face_plus_keyframes(self, store):
        _labelled_fixture(store)
        store.rename_person("p-1", "Alicia")
        snap = _stamp_snapshot(store)
        assert snap["face"]["m-img:f0"] == 42
        assert snap["face"]["vf:m-vid:0:0:f0"] == 42
        assert all(v == 42 for v in snap["keyframe"].values())

    def test_person_merge_stamps_reassigned_faces(self, store):
        _labelled_fixture(store)
        reassigned = store.merge_people("p-1", "p-2")
        assert reassigned == 2
        snap = _stamp_snapshot(store)
        assert snap["face"]["m-img:f0"] == 42
        assert snap["face"]["vf:m-vid:0:0:f0"] == 42
        assert all(v == 42 for v in snap["keyframe"].values())

    def test_path_change_stamps_all_three_collections(self, store):
        # S-1 move handling / path promotion routes through
        # update_media_fields({path, source_name, rel_path})
        _labelled_fixture(store)
        store.update_media_fields(
            "m-vid",
            {"path": "/photos/sub/m-vid.mp4", "source_name": "photos", "rel_path": "sub/m-vid.mp4"},
        )
        snap = _stamp_snapshot(store)
        assert all(v == 42 for v in snap["keyframe"].values())
        assert snap["face"]["vf:m-vid:0:0:f0"] == 42
        _reset_stamps(store)
        store.update_media_fields(
            "m-img",
            {"path": "/photos/sub/m-img.jpg", "source_name": "photos", "rel_path": "sub/m-img.jpg"},
        )
        assert _updated_seq(store, "image_embedding", "media_id", "m-img") == 42
        assert _updated_seq(store, "face_embedding", "face_id", "m-img:f0") == 42

    def test_embedding_upserts_stamp_both_arms(self, store):
        _labelled_fixture(store)
        # conflict-update arm
        store.upsert_image_embedding("m-img", np.ones(4, dtype=np.float32), model="clip-test")
        assert _updated_seq(store, "image_embedding", "media_id", "m-img") == 42
        # insert arm
        _insert_media(store, "m-new")
        store.upsert_image_embedding("m-new", np.ones(4, dtype=np.float32), model="clip-test")
        assert _updated_seq(store, "image_embedding", "media_id", "m-new") == 42

    def test_upsert_media_refresh_stamps_superset(self, store):
        _labelled_fixture(store)
        store.upsert_media(
            {
                "media_id": "m-img",
                "path": "/photos/m-img.x",
                "source_name": "photos",
                "rel_path": "m-img.x",
                "place": "Braga",
            }
        )
        snap = _stamp_snapshot(store)
        assert snap["image"]["m-img"] == 42
        assert snap["face"]["m-img:f0"] == 42

    def test_delete_faces_sets_recreate_marker_in_same_txn(self, store):
        _labelled_fixture(store)
        assert store.get_s3_state()["face_recreate_required"] is False
        n = store.delete_faces_for_media("m-vid")
        assert n == 1
        assert store.get_s3_state()["face_recreate_required"] is True
        # cascade removed the embedding row; keyframes stamped (people changed)
        snap = _stamp_snapshot(store)
        assert "vf:m-vid:0:0:f0" not in snap["face"]
        assert all(v == 42 for v in snap["keyframe"].values())
        # clear only via the explicit post-recreate-export call
        store.clear_face_recreate_required()
        assert store.get_s3_state()["face_recreate_required"] is False

    def test_delete_faces_noop_media_does_not_set_marker(self, store):
        _labelled_fixture(store)
        _insert_media(store, "m-plain")
        assert store.delete_faces_for_media("m-plain") == 0
        assert store.get_s3_state()["face_recreate_required"] is False


# ---------------------------------------------------------------------------
# §4.2 delta exporters + unconditional deletion pass + widened gate
# (real embedded local-mode Qdrant in tmp_path)
# ---------------------------------------------------------------------------


@pytest.fixture
def qdrant_env(tmp_path, monkeypatch):
    """Point the module-level settings of qdrant_export at tmp_path."""
    from msa_indexer.db import qdrant_export

    stub = SimpleNamespace(
        qdrant_path=tmp_path / "qdrant",
        collections=SimpleNamespace(image="image_emb", video="video_emb", face="face_emb"),
        qdrant_recreate_collections_on_export=False,
    )
    monkeypatch.setattr(qdrant_export, "S", stub)
    return stub


def _export_config(tmp_path, **overrides) -> SimpleNamespace:
    cfg = SimpleNamespace(
        sqlite_path=str(tmp_path / "media.sqlite"),
        export_recreate=False,
        col_video="video_emb",
        col_face="face_emb",
        reprocess_gps=False,
        reprocess_objects=False,
        reprocess_faces=False,
        reprocess_embeddings=False,
        face_recognizer_backend="facenet_pytorch",
    )
    for k, v in overrides.items():
        setattr(cfg, k, v)
    return cfg


def _qdrant_counts(qdrant_path) -> dict:
    from qdrant_client import QdrantClient

    client = QdrantClient(path=str(qdrant_path))
    try:
        out = {}
        for name in ("image_emb", "video_emb", "face_emb"):
            try:
                out[name] = client.count(name).count
            except Exception:
                out[name] = 0
        return out
    finally:
        client.close()


class TestDeltaExportAgainstRealQdrant:
    def _synced_fixture(self, store: SQLiteStore, tmp_path, qdrant_env):
        """Two images + one video (2 kf) + faces, fully exported, stamps 0."""
        from msa_indexer import pipeline

        _labelled_fixture(store)  # m-img (face), m-vid (2 kf, face), people
        store.stamp_seq = 1
        _add_image_with_embedding(store, "m-img2")
        _reset_stamps(store)
        outcome = pipeline._do_qdrant_export(
            _export_config(tmp_path), export_all=True
        )
        assert bool(outcome) is True
        counts = _qdrant_counts(qdrant_env.qdrant_path)
        assert counts == {"image_emb": 2, "video_emb": 2, "face_emb": 2}
        return counts

    def test_delta_export_sends_only_dirty_rows(self, store, tmp_path, qdrant_env):
        from msa_indexer import pipeline

        self._synced_fixture(store, tmp_path, qdrant_env)
        # Dirty ONE image row above the watermark
        store.conn.execute(
            "UPDATE image_embedding SET updated_seq = 5 WHERE media_id = 'm-img2'"
        )
        store.commit()
        outcome = pipeline._do_qdrant_export(
            _export_config(tmp_path), export_all=False, since_seq=4
        )
        assert bool(outcome) is True
        assert outcome.image_sent == 1, "delta must send ONLY the dirty row"
        assert outcome.video_sent == 0
        assert outcome.face_sent == 0
        assert outcome.deleted_points == 0

    def test_zero_dirty_delta_is_noop_and_records(self, store, tmp_path, qdrant_env):
        from msa_indexer import pipeline

        before = self._synced_fixture(store, tmp_path, qdrant_env)
        outcome = pipeline._do_qdrant_export(
            _export_config(tmp_path), export_all=False, since_seq=99
        )
        assert bool(outcome) is True, "0 dirty rows is a legitimate no-op (§4.3)"
        assert outcome.image_sent == 0 and outcome.video_sent == 0 and outcome.face_sent == 0
        assert _qdrant_counts(qdrant_env.qdrant_path) == before, (
            "a 0-dirty no-op must not touch any collection"
        )

    def test_pre_stage3_empty_tables_still_blocks_in_delta(self, store, tmp_path, qdrant_env):
        from msa_indexer import pipeline

        _insert_media(store, "m-meta-only")  # media row, NO embeddings at all
        outcome = pipeline._do_qdrant_export(
            _export_config(tmp_path), export_all=False, since_seq=0
        )
        assert bool(outcome) is False
        assert outcome.empty_tables is True

    def test_tombstone_deletion_removes_points_per_collection(self, store, tmp_path, qdrant_env):
        from msa_indexer import pipeline

        self._synced_fixture(store, tmp_path, qdrant_env)
        # Tombstone the video: its 2 keyframe points + its face point must go
        store.stamp_seq = 5
        store.tombstone_media("m-vid")
        outcome = pipeline._do_qdrant_export(
            _export_config(tmp_path), export_all=False, since_seq=4
        )
        assert bool(outcome) is True, "a deletion-only delta run must record"
        assert outcome.deleted_points == 3
        counts = _qdrant_counts(qdrant_env.qdrant_path)
        assert counts == {"image_emb": 2, "video_emb": 0, "face_emb": 1}

        # Tombstone an image: its image point + its face point must go
        store.stamp_seq = 6
        store.tombstone_media("m-img")
        outcome = pipeline._do_qdrant_export(
            _export_config(tmp_path), export_all=False, since_seq=5
        )
        assert bool(outcome) is True
        counts = _qdrant_counts(qdrant_env.qdrant_path)
        assert counts == {"image_emb": 1, "video_emb": 0, "face_emb": 0}

    def test_same_run_full_export_tombstone_deletion(self, store, tmp_path, qdrant_env):
        """§4.2: the deletion pass runs unconditionally — FULL exports
        included. A tombstone created in the same run as a full export would
        otherwise orphan its points forever (its deleted_seq equals the
        watermark that run records)."""
        from msa_indexer import pipeline

        self._synced_fixture(store, tmp_path, qdrant_env)
        store.stamp_seq = 5
        store.tombstone_media("m-img2")
        outcome = pipeline._do_qdrant_export(
            _export_config(tmp_path), export_all=True  # FULL export
        )
        assert bool(outcome) is True
        assert outcome.deleted_points >= 1
        counts = _qdrant_counts(qdrant_env.qdrant_path)
        assert counts["image_emb"] == 1, (
            "full export must still delete the same-run tombstone's point"
        )

    def test_deletion_pass_tolerates_absent_points(self, store, tmp_path, qdrant_env):
        """Embedded local-mode Qdrant raises KeyError deleting an absent
        point id (unlike the server) — the pass must filter, not crash, and
        a repeat pass must be a true no-op."""
        from msa_indexer import pipeline
        from msa_indexer.db.qdrant_export import delete_tombstoned_points_from_qdrant

        self._synced_fixture(store, tmp_path, qdrant_env)
        # Tombstone media that was NEVER exported (no points exist for it)
        store.stamp_seq = 5
        _insert_media(store, "m-ghost")
        store.tombstone_media("m-ghost")
        stats = delete_tombstoned_points_from_qdrant(
            Path(store.path), since_seq=4
        )
        assert stats["tombstones"] == 1
        assert stats["deleted_points"] == 0  # nothing existed — true no-op
        # Repeat pass over an already-deleted tombstone: also a no-op
        store.stamp_seq = 6
        store.tombstone_media("m-img")
        stats = delete_tombstoned_points_from_qdrant(Path(store.path), since_seq=4)
        assert stats["deleted_points"] == 2  # image + face point
        stats = delete_tombstoned_points_from_qdrant(Path(store.path), since_seq=4)
        assert stats["deleted_points"] == 0, "re-deleting must be a no-op"

    def test_face_delta_failure_blocks_watermark_and_rows_stay_dirty(
        self, store, tmp_path, qdrant_env, monkeypatch
    ):
        from msa_indexer import pipeline
        from msa_indexer.db import qdrant_export

        self._synced_fixture(store, tmp_path, qdrant_env)
        store.conn.execute("UPDATE face_embedding SET updated_seq = 5")
        store.commit()

        def _boom(*_a, **_k):
            raise RuntimeError("face export down")

        monkeypatch.setattr(qdrant_export, "export_faces_to_qdrant", _boom)
        outcome = pipeline._do_qdrant_export(
            _export_config(tmp_path), export_all=False, since_seq=4
        )
        assert bool(outcome) is False, "face failure must block the record (R8)"
        assert outcome.faces_ok is False
        # The dirty stamps survive for the next run
        assert store.dirty_rows_exist(4) is True

    def test_deletion_pass_failure_blocks_watermark(
        self, store, tmp_path, qdrant_env, monkeypatch
    ):
        from msa_indexer import pipeline
        from msa_indexer.db import qdrant_export

        self._synced_fixture(store, tmp_path, qdrant_env)
        store.stamp_seq = 5
        store.tombstone_media("m-img2")

        def _boom(*_a, **_k):
            raise RuntimeError("deletion pass down")

        monkeypatch.setattr(
            qdrant_export, "delete_tombstoned_points_from_qdrant", _boom
        )
        outcome = pipeline._do_qdrant_export(
            _export_config(tmp_path), export_all=False, since_seq=4
        )
        assert bool(outcome) is False
        assert outcome.deletions_ok is False
        assert store.dirty_rows_exist(4) is True, "tombstone stays stamped"

    def test_recreate_marker_forces_full_recreate_face_export(
        self, store, tmp_path, qdrant_env, monkeypatch
    ):
        from msa_indexer import pipeline
        from msa_indexer.db import qdrant_export

        self._synced_fixture(store, tmp_path, qdrant_env)
        # Simulate reprocess-faces having deleted rows: marker set durably
        assert store.delete_faces_for_media("m-vid") == 1
        assert store.get_s3_state()["face_recreate_required"] is True

        seen: dict = {}
        real_faces = qdrant_export.export_faces_to_qdrant

        def _spy(*a, **k):
            seen.update(k)
            return real_faces(*a, **k)

        monkeypatch.setattr(qdrant_export, "export_faces_to_qdrant", _spy)
        outcome = pipeline._do_qdrant_export(
            _export_config(tmp_path), export_all=False, since_seq=4
        )
        assert seen.get("recreate") is True, "marker must force recreate mode"
        assert seen.get("since_seq") is None, "recreate implies a FULL face export"
        assert bool(outcome) is True
        assert store.get_s3_state()["face_recreate_required"] is False, (
            "marker cleared only after the recreate export succeeded"
        )
        # The deleted video face's point is gone from the recreated collection
        counts = _qdrant_counts(qdrant_env.qdrant_path)
        assert counts["face_emb"] == 1

    def test_recreate_marker_survives_failed_recreate(
        self, store, tmp_path, qdrant_env, monkeypatch
    ):
        from msa_indexer import pipeline
        from msa_indexer.db import qdrant_export

        self._synced_fixture(store, tmp_path, qdrant_env)
        store.delete_faces_for_media("m-vid")

        def _boom(*_a, **_k):
            raise RuntimeError("face export down")

        monkeypatch.setattr(qdrant_export, "export_faces_to_qdrant", _boom)
        outcome = pipeline._do_qdrant_export(
            _export_config(tmp_path), export_all=False, since_seq=4
        )
        assert bool(outcome) is False
        assert store.get_s3_state()["face_recreate_required"] is True, (
            "marker must survive a failed recreate export (crash-durable repair)"
        )

    def test_reprocess_mode_still_runs_recreate_face_export(
        self, store, tmp_path, qdrant_env
    ):
        """A reprocess-faces run gates image/video export off (no record)
        but the durable marker still forces the face recreate — stale face
        points must not outlive their rows."""
        from msa_indexer import pipeline

        self._synced_fixture(store, tmp_path, qdrant_env)
        store.delete_faces_for_media("m-vid")
        outcome = pipeline._do_qdrant_export(
            _export_config(tmp_path, reprocess_faces=True), export_all=False
        )
        assert outcome.images_attempted is False
        assert bool(outcome) is False, "reprocess mode still withholds the record"
        assert outcome.faces_ok is True
        assert store.get_s3_state()["face_recreate_required"] is False
        counts = _qdrant_counts(qdrant_env.qdrant_path)
        assert counts["face_emb"] == 1, "recreate removed the deleted face's point"

    def test_windows_recreate_removal_failure_blocks_watermark(
        self, store, tmp_path, qdrant_env, monkeypatch
    ):
        """M-8/S-3 hotfix invariant end-to-end (simulating the Windows
        open-handle): if the embedded recreate cannot remove the on-disk face
        collection, the face export must RAISE, the run must NOT record the
        export version, and the durable recreate marker must survive — never
        the silent "stale points survive AND the watermark advances" state
        (§4.2 gate)."""
        import time as _time

        import qdrant_client.local.qdrant_local as _ql

        from msa_indexer import pipeline
        from msa_indexer.db import qdrant_export

        self._synced_fixture(store, tmp_path, qdrant_env)
        store.delete_faces_for_media("m-vid")  # marker set durably
        assert store.get_s3_state()["face_recreate_required"] is True

        monkeypatch.setattr(_time, "sleep", lambda *_a: None)
        # Simulate Windows: the backend's own delete rmtree cannot remove the
        # dir (open handle) ...
        monkeypatch.setattr(_ql.shutil, "rmtree", lambda *_a, **_k: None)
        # ... and our retry removal is pinned open too (persistent WinError 32).
        monkeypatch.setattr(
            qdrant_export,
            "_rmtree",
            lambda *_a, **_k: (_ for _ in ()).throw(
                OSError("[WinError 32] file in use (simulated)")
            ),
        )
        outcome = pipeline._do_qdrant_export(
            _export_config(tmp_path), export_all=False, since_seq=4
        )
        assert bool(outcome) is False, "an unremovable recreate must block the record"
        assert outcome.faces_ok is False
        assert store.get_s3_state()["face_recreate_required"] is True, (
            "marker must survive so the recreate retries — watermark NOT advanced"
        )

    def test_face_export_errors_block_record_and_keep_marker(
        self, store, tmp_path, qdrant_env, monkeypatch
    ):
        """PR #205 review (Codex P2): the face exporter swallows batch-upsert
        failures into errors>0 and returns normally — that must still block
        the record and keep the durable recreate marker set."""
        from msa_indexer import pipeline
        from msa_indexer.db import qdrant_export

        self._synced_fixture(store, tmp_path, qdrant_env)
        store.delete_faces_for_media("m-vid")  # marker set durably
        monkeypatch.setattr(
            qdrant_export,
            "export_faces_to_qdrant",
            lambda *a, **k: {
                "faces_count": 1, "sent": 0, "skipped": 0, "errors": 1, "dim": 4,
            },
        )
        outcome = pipeline._do_qdrant_export(
            _export_config(tmp_path), export_all=False, since_seq=4
        )
        assert bool(outcome) is False, "errors>0 must block the record (R8)"
        assert outcome.faces_ok is False
        assert store.get_s3_state()["face_recreate_required"] is True, (
            "marker must survive an errors>0 face export"
        )

    def test_face_export_skips_do_not_block(
        self, store, tmp_path, qdrant_env, monkeypatch
    ):
        """skipped (face row without an embedding blob) is stable SQLite
        state that re-stamps itself when the embedding lands — it must NOT
        wedge the watermark."""
        from msa_indexer import pipeline
        from msa_indexer.db import qdrant_export

        self._synced_fixture(store, tmp_path, qdrant_env)
        monkeypatch.setattr(
            qdrant_export,
            "export_faces_to_qdrant",
            lambda *a, **k: {
                "faces_count": 2, "sent": 1, "skipped": 1, "errors": 0, "dim": 4,
            },
        )
        outcome = pipeline._do_qdrant_export(
            _export_config(tmp_path), export_all=False, since_seq=4
        )
        assert bool(outcome) is True
        assert outcome.faces_ok is True

    def test_image_video_export_errors_block_record(
        self, store, tmp_path, qdrant_env, monkeypatch
    ):
        """Same swallow exists in the image/video exporters: sent>0 with
        errors>0 must not advance the watermark past the failed rows."""
        from msa_indexer import pipeline

        self._synced_fixture(store, tmp_path, qdrant_env)
        monkeypatch.setattr(
            pipeline,
            "export_images_to_qdrant",
            lambda *a, **k: {
                "image_count": 2, "sent": 1, "skipped": 0, "errors": 1, "dim": 4,
            },
        )
        monkeypatch.setattr(
            pipeline,
            "export_video_frames_to_qdrant",
            lambda *a, **k: {
                "video_keyframes_count": 0, "sent": 0, "skipped": 0, "errors": 0, "dim": 4,
            },
        )
        outcome = pipeline._do_qdrant_export(
            _export_config(tmp_path), export_all=False, since_seq=4
        )
        assert bool(outcome) is False
        assert outcome.images_ok is False

    def test_recreate_with_empty_face_table_clears_stale_collection(
        self, store, tmp_path, qdrant_env
    ):
        """PR #205 review (Codex P2): when reprocessing deleted the LAST
        face rows, the recreate export must still clear the old points (the
        collection is deleted outright) before the marker is cleared —
        otherwise stale face points survive in Qdrant with no future retry."""
        from msa_indexer import pipeline

        self._synced_fixture(store, tmp_path, qdrant_env)
        # Delete BOTH media's faces: face_embedding is now empty, marker set
        assert store.delete_faces_for_media("m-img") == 1
        assert store.delete_faces_for_media("m-vid") == 1
        assert (
            _scalar(store, "SELECT COUNT(*) FROM face_embedding") == 0
        ), "fixture must hit the empty-table recreate shape"
        outcome = pipeline._do_qdrant_export(
            _export_config(tmp_path), export_all=False, since_seq=4
        )
        assert bool(outcome) is True
        assert store.get_s3_state()["face_recreate_required"] is False, (
            "marker cleared only after the stale collection was removed"
        )
        counts = _qdrant_counts(qdrant_env.qdrant_path)
        assert counts["face_emb"] == 0, (
            "the two stale face points must not survive the empty recreate"
        )
        assert counts["image_emb"] == 2 and counts["video_emb"] == 2, (
            "other collections untouched"
        )
        # Idempotent: marker already clear, collection already gone
        outcome = pipeline._do_qdrant_export(
            _export_config(tmp_path), export_all=False, since_seq=4
        )
        assert bool(outcome) is True

    def test_windows_empty_face_recreate_removal_failure_blocks_watermark(
        self, store, tmp_path, qdrant_env, monkeypatch
    ):
        """Round-1 mesh (Codex P2): the empty-``face_embedding``-table recreate
        branch must route through the SAME close+purge/raise path as
        ensure_collection. Simulating Windows (the on-disk face collection dir
        cannot be removed), an EMPTY-table recreate must RAISE — the run must
        NOT record the export version and the durable recreate marker must
        survive — never the silent "stale face collection survives on disk AND
        the watermark advances" state (§4.2 gate). Before the fix this branch
        called delete_collection directly, so a blocked rmtree left the stale
        collection to be RELOADED by the next non-empty export after the marker
        was already cleared."""
        import time as _time

        import qdrant_client.local.qdrant_local as _ql

        from msa_indexer import pipeline
        from msa_indexer.db import qdrant_export

        self._synced_fixture(store, tmp_path, qdrant_env)
        # Delete BOTH media's faces → face_embedding empty → the first_row-None
        # recreate branch; marker set durably.
        assert store.delete_faces_for_media("m-img") == 1
        assert store.delete_faces_for_media("m-vid") == 1
        assert (
            _scalar(store, "SELECT COUNT(*) FROM face_embedding") == 0
        ), "fixture must hit the empty-table recreate shape"
        assert store.get_s3_state()["face_recreate_required"] is True

        monkeypatch.setattr(_time, "sleep", lambda *_a: None)
        # Simulate Windows: neither the backend's own delete rmtree ...
        monkeypatch.setattr(_ql.shutil, "rmtree", lambda *_a, **_k: None)
        # ... nor our retry removal can remove the on-disk face collection dir.
        monkeypatch.setattr(
            qdrant_export,
            "_rmtree",
            lambda *_a, **_k: (_ for _ in ()).throw(
                OSError("[WinError 32] file in use (simulated)")
            ),
        )
        outcome = pipeline._do_qdrant_export(
            _export_config(tmp_path), export_all=False, since_seq=4
        )
        assert bool(outcome) is False, (
            "an unremovable empty-table recreate must block the record"
        )
        assert outcome.faces_ok is False
        assert store.get_s3_state()["face_recreate_required"] is True, (
            "marker must survive so the recreate retries — watermark NOT advanced"
        )

    def test_transient_collection_lookup_error_raises_not_skips(
        self, store, tmp_path, qdrant_env, monkeypatch
    ):
        """PR #205 round-2 review (Codex P2): a NON-not-found failure of the
        deletion pass's collection lookup must propagate — the caller then
        keeps deletions_ok False and blocks the watermark. Treating it as
        "collection absent" would return a successful pass and the selected
        tombstones would fall below the next since_seq with their points
        never deleted (permanent dangling points)."""
        from msa_indexer.db import qdrant_export
        from msa_indexer.db.qdrant_export import (
            delete_tombstoned_points_from_qdrant,
        )

        self._synced_fixture(store, tmp_path, qdrant_env)
        store.stamp_seq = 5
        store.tombstone_media("m-img2")

        real_cls = qdrant_export.QdrantClient

        class _FlakyLookupClient(real_cls):
            def get_collection(self, *a, **k):
                raise RuntimeError("storage IO error (transient)")

        monkeypatch.setattr(qdrant_export, "QdrantClient", _FlakyLookupClient)
        with pytest.raises(RuntimeError, match="storage IO error"):
            delete_tombstoned_points_from_qdrant(Path(store.path), since_seq=4)
        # The tombstone stays stamped/selectable for the next run...
        assert store.dirty_rows_exist(4) is True
        # ...and the point was NOT deleted (nor falsely reported as handled)
        monkeypatch.setattr(qdrant_export, "QdrantClient", real_cls)
        counts = _qdrant_counts(qdrant_env.qdrant_path)
        assert counts["image_emb"] == 2

    def test_deletion_pass_skips_verified_not_found_collections(
        self, store, tmp_path, qdrant_env
    ):
        """A genuinely absent collection (never created) is still a clean
        skip: verified not-found continues, the pass succeeds with nothing
        deleted."""
        from msa_indexer.db.qdrant_export import (
            delete_tombstoned_points_from_qdrant,
        )

        # No export ever ran — the Qdrant store has NO collections at all.
        _labelled_fixture(store)
        store.stamp_seq = 5
        store.tombstone_media("m-img")
        stats = delete_tombstoned_points_from_qdrant(
            Path(store.path), since_seq=4
        )
        assert stats["tombstones"] == 1
        assert stats["deleted_points"] == 0

    def test_missing_collection_degrades_that_exporter_to_full(
        self, store, tmp_path, qdrant_env
    ):
        """PR #205 round-3 review (Codex P2): a delta run whose target
        collection is missing (ensure_collection silently creates it EMPTY)
        must degrade to a FULL export for THAT collection only — otherwise
        only rows above the watermark upload, the version records, and
        every unchanged point is permanently absent/unsearchable."""
        from qdrant_client import QdrantClient

        from msa_indexer import pipeline

        before = self._synced_fixture(store, tmp_path, qdrant_env)
        # Drop ONE collection between runs (partial Qdrant loss / user wipe)
        client = QdrantClient(path=str(qdrant_env.qdrant_path))
        try:
            client.delete_collection("video_emb")
        finally:
            client.close()
        # Dirty ONE image row so the untouched collections stay observably
        # delta-sized
        store.conn.execute(
            "UPDATE image_embedding SET updated_seq = 5 WHERE media_id = 'm-img2'"
        )
        store.commit()
        outcome = pipeline._do_qdrant_export(
            _export_config(tmp_path), export_all=False, since_seq=4
        )
        assert bool(outcome) is True, "the degraded run must still record"
        assert outcome.video_sent == 2, (
            "missing collection must be repopulated in FULL"
        )
        assert outcome.image_sent == 1, "other collections stay delta"
        assert outcome.face_sent == 0, "other collections stay delta"
        assert _qdrant_counts(qdrant_env.qdrant_path) == before, (
            "every unchanged point must be present again after the run"
        )

    def test_missing_face_collection_degrades_face_export_to_full(
        self, store, tmp_path, qdrant_env
    ):
        """Same round-3 finding, face arm: the face exporter has its own
        since_seq handling (recreate paths pass None) — a silently-missing
        face collection under plain delta must also degrade to full."""
        from qdrant_client import QdrantClient

        from msa_indexer import pipeline

        before = self._synced_fixture(store, tmp_path, qdrant_env)
        client = QdrantClient(path=str(qdrant_env.qdrant_path))
        try:
            client.delete_collection("face_emb")
        finally:
            client.close()
        outcome = pipeline._do_qdrant_export(
            _export_config(tmp_path), export_all=False, since_seq=4
        )
        assert bool(outcome) is True
        assert outcome.face_sent == 2, "face collection repopulated in FULL"
        assert outcome.image_sent == 0 and outcome.video_sent == 0, (
            "image/video stay delta (0 dirty rows)"
        )
        assert _qdrant_counts(qdrant_env.qdrant_path) == before

    def test_forced_recreate_under_delta_window_repopulates_fully(
        self, store, tmp_path, qdrant_env
    ):
        """Same round-3 finding, recreate arm: a recreate request combined
        with a delta window must behave like the missing-collection case —
        the recreated (empty) collections get the FULL point set back."""
        from msa_indexer import pipeline

        before = self._synced_fixture(store, tmp_path, qdrant_env)
        outcome = pipeline._do_qdrant_export(
            _export_config(tmp_path, export_recreate=True),
            export_all=False,
            since_seq=4,
        )
        assert bool(outcome) is True
        assert outcome.image_sent == 2
        assert outcome.video_sent == 2
        assert outcome.face_sent == 2
        assert _qdrant_counts(qdrant_env.qdrant_path) == before, (
            "recreated collections must hold every point, not just dirty rows"
        )


class TestEnsureCollectionCreatedReport:
    """Round-3 finding contract: ensure_collection reports whether it
    actually created (or recreated) the collection — the signal exporters
    use to degrade a delta window to full."""

    def test_created_reused_and_recreated(self, qdrant_env):
        from qdrant_client import QdrantClient

        from msa_indexer.db.qdrant_export import ensure_collection

        client = QdrantClient(path=str(qdrant_env.qdrant_path))
        try:
            assert ensure_collection(client, "c1", vector_size=4) is True, (
                "missing collection → created"
            )
            assert ensure_collection(client, "c1", vector_size=4) is False, (
                "pre-existing collection → reused"
            )
            assert (
                ensure_collection(client, "c1", vector_size=4, recreate=True)
                is True
            ), "recreate → starts empty → created"
        finally:
            client.close()


class TestEnsureCollectionRecreateFailures:
    """§4.2 audit (round 2, same class as the deletion-pass finding): a
    transient failure during recreate-deletion must propagate. Proceeding
    would upsert into the STALE collection, the export would "succeed", and
    the caller would record the watermark / clear face_recreate_required
    with the orphaned points still present — permanently."""

    def test_transient_delete_failure_raises(self):
        from msa_indexer.db.qdrant_export import ensure_collection

        class _Client:
            def get_collection(self, name):
                return SimpleNamespace(points_count=1)

            def delete_collection(self, name):
                raise RuntimeError("storage IO error (transient)")

        with pytest.raises(RuntimeError, match="storage IO error"):
            ensure_collection(_Client(), "face_emb", 512, recreate=True)

    def test_not_found_delete_still_creates(self):
        from msa_indexer.db.qdrant_export import ensure_collection

        created = []

        class _Client:
            def get_collection(self, name):
                raise ValueError(f"Collection {name} not found")

            def delete_collection(self, name):
                raise ValueError(f"Collection {name} not found")

            def create_collection(self, collection_name, vectors_config):
                created.append(collection_name)

        ensure_collection(_Client(), "face_emb", 512, recreate=True)
        assert created == ["face_emb"]

    def test_collection_surviving_recreate_deletion_raises(self, monkeypatch):
        import time

        from msa_indexer.db.qdrant_export import ensure_collection

        monkeypatch.setattr(time, "sleep", lambda s: None)

        class _Client:
            """delete_collection "succeeds" but the collection never goes
            away (e.g. a server-side deletion that silently failed)."""

            def get_collection(self, name):
                return SimpleNamespace(
                    points_count=7,
                    vectors_config=SimpleNamespace(
                        params=SimpleNamespace(size=512)
                    ),
                )

            def delete_collection(self, name):
                pass

        with pytest.raises(RuntimeError, match="recreate was not honored"):
            ensure_collection(_Client(), "face_emb", 512, recreate=True)


class TestEmbeddedRecreateWindowsOpenHandle:
    """M-8/S-3 hotfix: the embedded (local) recreate path must actually remove
    the on-disk collection directory. On Windows the LocalCollection's open
    sqlite handle makes QdrantLocal.delete_collection's
    ``rmtree(ignore_errors=True)`` fail silently, ``storage.sqlite`` survives,
    and create_collection RELOADS the stale points (LOG-001 sibling). These
    pin our seam's removal + the RAISE-blocks-the-watermark invariant on
    non-Windows hosts (a Windows-only failure otherwise: the on-disk dir here
    is removable so the fix is a no-op that always passes)."""

    @staticmethod
    def _seed_collection(path, name="c", n=2):
        from qdrant_client import QdrantClient
        from qdrant_client.http import models as rest

        client = QdrantClient(path=str(path))
        client.create_collection(
            name, vectors_config=rest.VectorParams(size=4, distance=rest.Distance.COSINE)
        )
        client.upsert(
            name,
            points=[
                rest.PointStruct(id=i, vector=[0.0, 0.0, 0.0, 0.0]) for i in range(1, n + 1)
            ],
        )
        return client

    def test_purge_removes_embedded_collection_dir(self, tmp_path):
        import os

        from msa_indexer.db import qdrant_export

        client = self._seed_collection(tmp_path / "qd")
        try:
            coll_dir = os.path.join(str(tmp_path / "qd"), "collection", "c")
            assert os.path.isdir(coll_dir)
            qdrant_export._close_local_collection_handle(client, "c")
            qdrant_export._purge_local_collection_dir(client, "c")
            assert not os.path.exists(coll_dir), (
                "purge must remove the on-disk collection dir"
            )
        finally:
            client.close()

    def test_purge_raises_when_removal_fails(self, tmp_path, monkeypatch):
        """RAISE invariant: a removal that cannot be guaranteed must propagate,
        so the caller's §4.2 gate never records a watermark over survivors."""
        import time

        from msa_indexer.db import qdrant_export

        client = self._seed_collection(tmp_path / "qd")
        try:
            monkeypatch.setattr(time, "sleep", lambda *_a: None)
            monkeypatch.setattr(
                qdrant_export,
                "_rmtree",
                lambda *_a, **_k: (_ for _ in ()).throw(
                    OSError("[WinError 32] file in use (simulated)")
                ),
            )
            with pytest.raises(RuntimeError, match="Could not remove on-disk"):
                qdrant_export._purge_local_collection_dir(client, "c")
        finally:
            client.close()

    def test_purge_noop_in_server_mode(self, monkeypatch):
        """Guard: server/remote mode has no on-disk dir — the fix must be a
        harmless no-op there and never touch the filesystem."""
        from msa_indexer.db import qdrant_export

        monkeypatch.setattr(
            qdrant_export,
            "_rmtree",
            lambda *_a, **_k: pytest.fail("_rmtree must not run in server mode"),
        )
        remote_like = SimpleNamespace(_client=SimpleNamespace())  # no `persistent`
        # Neither helper may raise or touch disk.
        qdrant_export._close_local_collection_handle(remote_like, "c")
        qdrant_export._purge_local_collection_dir(remote_like, "c")
        assert qdrant_export._embedded_collection_dir(remote_like, "c") is None

    def test_recreate_raises_when_on_disk_removal_fails(self, tmp_path, monkeypatch):
        """End-to-end at the ensure_collection seam, simulating Windows: the
        backend's delete rmtree is a no-op (handle blocked it) AND our retry
        removal fails persistently — ensure_collection must RAISE instead of
        letting create_collection reload the surviving points."""
        import time

        import qdrant_client.local.qdrant_local as _ql
        from msa_indexer.db import qdrant_export

        client = self._seed_collection(tmp_path / "qd")
        try:
            monkeypatch.setattr(time, "sleep", lambda *_a: None)
            # Simulate Windows: the backend's own rmtree cannot remove the dir.
            monkeypatch.setattr(_ql.shutil, "rmtree", lambda *_a, **_k: None)
            # And our retry removal is pinned open too (persistent WinError 32).
            monkeypatch.setattr(
                qdrant_export,
                "_rmtree",
                lambda *_a, **_k: (_ for _ in ()).throw(
                    OSError("[WinError 32] file in use (simulated)")
                ),
            )
            with pytest.raises(RuntimeError, match="Could not remove on-disk"):
                qdrant_export.ensure_collection(client, "c", vector_size=4, recreate=True)
        finally:
            client.close()


class TestDeltaIterators:
    def test_iter_items_delta_filters_by_updated_seq(self, store):
        _labelled_fixture(store)
        store.conn.execute(
            "UPDATE image_embedding SET updated_seq = 7 WHERE media_id = 'm-img'"
        )
        store.commit()
        assert [r["id"] for r in store.iter_items(since_seq=6)] == ["m-img"]
        assert [r["id"] for r in store.iter_items(since_seq=7)] == []
        # full mode unchanged
        assert [r["id"] for r in store.iter_items()] == ["m-img"]

    def test_iter_video_keyframes_delta_filters(self, store):
        _labelled_fixture(store)
        kf_id = store.get_keyframe_id("m-vid", 0, 0)
        store.conn.execute(
            "UPDATE keyframe_embedding SET updated_seq = 7 WHERE keyframe_id = ?",
            (kf_id,),
        )
        store.commit()
        rows = list(store.iter_video_keyframes(since_seq=6))
        assert [(r["shot_index"], r["kf_index"]) for r in rows] == [(0, 0)]
        assert list(store.iter_video_keyframes(since_seq=7)) == []
        assert len(list(store.iter_video_keyframes())) == 2

    def test_iter_faces_delta_filters(self, store):
        _labelled_fixture(store)
        store.conn.execute(
            "UPDATE face_embedding SET updated_seq = 7 WHERE face_id = 'm-img:f0'"
        )
        store.commit()
        assert [r["face_id"] for r in store.iter_faces(since_seq=6)] == ["m-img:f0"]
        assert list(store.iter_faces(since_seq=7)) == []
        assert len(list(store.iter_faces())) == 2


# ---------------------------------------------------------------------------
# §4.2 export decision: dirty trigger (P1), first-delta-run guard (P1),
# watermark advance, recreate marker — pipeline level (G8 set).
#
# Harness mirrors tests/test_fingerprint_fastpath.py: monkeypatched
# embedder/export/exif, SimpleNamespace config, real tmp media files, REAL
# SQLiteStore underneath. Duplicated (not imported) to keep test modules
# independent.
# ---------------------------------------------------------------------------


class _FakeClipEmbedder:
    dim = 8

    def __init__(self, *_args, **_kwargs):
        pass

    def image_embed(self, images):
        return [np.zeros(self.dim, dtype=np.float32) for _ in images]


class _DeltaFakeQdrant:
    """Records exports (incl. the since_seq each was called with) and
    mirrors the recorded version like real Qdrant."""

    def __init__(self):
        self.export_calls = 0
        self.since_seqs: list = []
        self.recorded: dict | None = None
        self.export_result = True

    def do_export(self, *_args, **kwargs):
        self.export_calls += 1
        self.since_seqs.append(kwargs.get("since_seq"))
        return self.export_result

    def record_version(self, seq, ts):
        self.recorded = {"index_version_seq": seq, "index_version_ts": ts}

    def get_version(self):
        return dict(self.recorded) if self.recorded else None


def _patch_pipeline(monkeypatch, pipeline, qdrant: _DeltaFakeQdrant):
    monkeypatch.setattr(pipeline, "resolve_for_access", lambda p: p)
    monkeypatch.setattr(pipeline, "get_exif_basic", lambda _p: {})
    monkeypatch.setattr(pipeline, "write_thumbnail", lambda *_a, **_k: None)
    monkeypatch.setattr(pipeline, "ClipEmbedder", _FakeClipEmbedder)
    monkeypatch.setattr(pipeline, "sha256_of_file", lambda p: "sha-" + Path(p).name)
    monkeypatch.setattr(pipeline, "_load_historical_perf", lambda _p: (1.0, 30.0, 5.0))
    monkeypatch.setattr(pipeline, "_emit_indexer_summary", lambda **_p: None)
    monkeypatch.setattr(pipeline, "get_qdrant_export_version", qdrant.get_version)
    monkeypatch.setattr(pipeline, "record_qdrant_export_version", qdrant.record_version)
    monkeypatch.setattr(pipeline, "_do_qdrant_export", qdrant.do_export)


def _delta_pipeline_config(tmp_path: Path, media_dir: Path, source_name: str = "scratch", **overrides):
    cfg = SimpleNamespace(
        sqlite_path=str(tmp_path / "media.sqlite"),
        thumb_dir=tmp_path / "thumbs",
        face_thumb_dir=tmp_path / "face-thumbs",
        log_dir=tmp_path / "logs",
        model_name="ViT-L-14",
        pretrained="openai",
        model_version="test-v1",
        device="cpu",
        enable_object_detection=False,
        enable_face_recognition=False,
        media_sources=[SimpleNamespace(name=source_name, path=str(media_dir), enabled=True)],
    )
    for k, v in overrides.items():
        setattr(cfg, k, v)
    return cfg


def _index_seq(sqlite_path) -> int:
    conn = sqlite3.connect(str(sqlite_path))
    try:
        row = conn.execute(
            "SELECT index_version_seq FROM index_state WHERE singleton_id=1"
        ).fetchone()
        return int(row[0]) if row else 0
    finally:
        conn.close()


def _make_images(media_dir: Path, count: int) -> list[Path]:
    paths = []
    media_dir.mkdir(exist_ok=True)
    for i in range(count):
        p = media_dir / f"img_{i:03d}.jpg"
        Image.new("RGB", (8, 8), color=(i % 256, (i * 7) % 256, 200)).save(p)
        paths.append(p)
    return paths


class TestG8CrashRecovery:
    def test_crash_recovery_exports_once_and_advances_watermark(
        self, tmp_path, monkeypatch
    ):
        """G8: rows stamped by a crashed run (committed, no bump, no record)
        must trigger an export on the next no-op run; the watermark advances
        to the max stamped seq so the recovery runs ONCE."""
        from msa_indexer import pipeline

        media_dir = tmp_path / "photos"
        _make_images(media_dir, 2)
        qdrant = _DeltaFakeQdrant()
        _patch_pipeline(monkeypatch, pipeline, qdrant)
        config = _delta_pipeline_config(tmp_path, media_dir)

        # Run 1: fresh DB — full export (no record exists), recorded at seq 1
        pipeline.run_index(config)
        assert _index_seq(config.sqlite_path) == 1
        assert qdrant.export_calls == 1
        assert qdrant.since_seqs[-1] is None, "no export record yet → full"
        assert qdrant.recorded["index_version_seq"] == 1

        # Simulate the crash window: a run stamped rows at pending_seq=2 and
        # died before bump_index_version / the version record.
        conn = sqlite3.connect(config.sqlite_path)
        conn.execute(
            "UPDATE image_embedding SET updated_seq = 2 WHERE media_id = "
            "(SELECT media_id FROM image_embedding LIMIT 1)"
        )
        conn.commit()
        conn.close()

        # Run 2 (nothing changed on disk): the dirty trigger must fire, the
        # interrupted bump completes (seq -> 2), delta export since 1, and
        # the NEW watermark (2) is recorded.
        pipeline.run_index(config)
        assert qdrant.export_calls == 2, "dirty trigger must force the export path"
        assert qdrant.since_seqs[-1] == 1, "delta engaged against the recorded seq"
        assert _index_seq(config.sqlite_path) == 2, "interrupted bump completed"
        assert qdrant.recorded["index_version_seq"] == 2, (
            "recording the stale lower seq would repeat the recovery forever"
        )

        # Run 3: recovery ran ONCE — a following no-op stays silent.
        pipeline.run_index(config)
        assert qdrant.export_calls == 2

    def test_crashed_before_first_bump_edge(self, tmp_path, monkeypatch):
        """seq 0, stamps at 1, no export record: trigger fires, seq advances
        to 1, guard forces full, record lands at 1."""
        from msa_indexer import pipeline

        media_dir = tmp_path / "photos"
        _make_images(media_dir, 1)
        qdrant = _DeltaFakeQdrant()
        _patch_pipeline(monkeypatch, pipeline, qdrant)
        config = _delta_pipeline_config(tmp_path, media_dir)

        # Build the DB state a crashed FIRST run leaves: rows + stamps at 1,
        # seq still 0, no record. Easiest faithful construction: run once
        # with the record call suppressed (export "crashed" before record).
        qdrant.export_result = False
        pipeline.run_index(config)
        assert qdrant.recorded is None
        assert _index_seq(config.sqlite_path) == 1  # bump landed this time
        conn = sqlite3.connect(config.sqlite_path)
        conn.execute("UPDATE index_state SET index_version_seq = 0")  # undo bump = crash
        conn.commit()
        conn.close()

        qdrant.export_result = True
        pipeline.run_index(config)
        assert qdrant.export_calls == 2
        assert qdrant.since_seqs[-1] is None, "no record → first-delta-run guard → full"
        assert _index_seq(config.sqlite_path) == 1, "advanced to max stamped seq"
        assert qdrant.recorded["index_version_seq"] == 1

        pipeline.run_index(config)
        assert qdrant.export_calls == 2, "recovery must not repeat"


class TestFirstDeltaRunGuard:
    def _migrated_run(self, tmp_path, monkeypatch, exported_seq):
        from msa_indexer import pipeline

        db_path = tmp_path / "media.sqlite"
        _build_pre_s3_db(db_path, index_version_seq=3)
        media_dir = tmp_path / "empty-src"
        media_dir.mkdir()
        qdrant = _DeltaFakeQdrant()
        if exported_seq is not None:
            qdrant.recorded = {"index_version_seq": exported_seq, "index_version_ts": "t"}
        _patch_pipeline(monkeypatch, pipeline, qdrant)
        config = _delta_pipeline_config(tmp_path, media_dir, source_name="scratch")
        return pipeline, config, qdrant

    def test_stale_qdrant_migration_forces_one_full_export(self, tmp_path, monkeypatch):
        """exported_seq < s3_migration_seq (an export failed pre-migration):
        the first export after migration MUST be full — a delta pass over
        legacy updated_seq=0 rows would export nothing and record currency,
        permanently hiding the stale points."""
        pipeline, config, qdrant = self._migrated_run(tmp_path, monkeypatch, exported_seq=2)

        pipeline.run_index(config)  # migration runs inside init_schema
        assert qdrant.export_calls == 1, "qdrant_stale must trigger the catch-up"
        assert qdrant.since_seqs[-1] is None, "first-delta-run guard forces FULL"
        # The full export records seq >= migration seq → delta engages after
        assert qdrant.recorded["index_version_seq"] >= 3

        # A later out-of-band stamp now engages delta mode
        conn = sqlite3.connect(config.sqlite_path)
        conn.execute("UPDATE face_embedding SET updated_seq = 90")
        conn.commit()
        conn.close()
        pipeline.run_index(config)
        assert qdrant.export_calls == 2
        assert qdrant.since_seqs[-1] is not None, "delta engaged after the full export"

    def test_in_sync_migration_engages_delta_and_deletes_legacy_tombstones(
        self, tmp_path, monkeypatch
    ):
        """exported_seq == index_version_seq at migration: the seeded legacy
        tombstone (stamped at seq+1) fires the dirty trigger on the first
        run, delta engages immediately, the watermark advances past the
        seed, and the recovery runs once."""
        pipeline, config, qdrant = self._migrated_run(tmp_path, monkeypatch, exported_seq=3)

        pipeline.run_index(config)
        assert qdrant.export_calls == 1, (
            "the seeded tombstone (deleted_seq=4 > exported 3) must fire the dirty trigger"
        )
        assert qdrant.since_seqs[-1] == 3, "delta engages immediately when in sync"
        assert _index_seq(config.sqlite_path) == 4, "watermark advanced past the seed"
        assert qdrant.recorded["index_version_seq"] == 4

        pipeline.run_index(config)
        assert qdrant.export_calls == 1, "tombstone cleanup must not repeat"

    def test_missing_export_record_forces_full(self, tmp_path, monkeypatch):
        pipeline, config, qdrant = self._migrated_run(tmp_path, monkeypatch, exported_seq=None)

        # No record at all: qdrant_stale (local seq 3 > -1) → export, FULL
        pipeline.run_index(config)
        assert qdrant.export_calls == 1
        assert qdrant.since_seqs[-1] is None


class TestFaceRecreateTrigger:
    def test_marker_alone_fires_the_export_trigger_and_persists_on_failure(
        self, tmp_path, monkeypatch
    ):
        """face_recreate_required survives a crash between the face-row
        deletion commit and the export: the next run's trigger fires on the
        marker alone; a failed export keeps it set (and keeps triggering)
        until a recreate export succeeds."""
        from msa_indexer import pipeline

        media_dir = tmp_path / "photos"
        _make_images(media_dir, 1)
        qdrant = _DeltaFakeQdrant()
        _patch_pipeline(monkeypatch, pipeline, qdrant)
        config = _delta_pipeline_config(tmp_path, media_dir)
        pipeline.run_index(config)
        assert qdrant.export_calls == 1

        # Simulate: a reprocess-faces run deleted face rows (marker set in
        # that same transaction) and crashed before its export.
        conn = sqlite3.connect(config.sqlite_path)
        conn.execute("UPDATE index_state SET face_recreate_required = 1")
        conn.commit()
        conn.close()

        # Export fails → marker must persist and re-trigger
        qdrant.export_result = False
        pipeline.run_index(config)
        assert qdrant.export_calls == 2, "marker alone must fire the trigger"
        assert (
            _scalar(Path(config.sqlite_path), "SELECT face_recreate_required FROM index_state") == 1
        ), "a failed export must not clear the durable marker"

        # Next run still triggers; a successful recreate export (the real
        # _do_qdrant_export clears the marker — proven in the real-Qdrant
        # tests) is simulated here by clearing after success.
        qdrant.export_result = True
        pipeline.run_index(config)
        assert qdrant.export_calls == 3


# ---------------------------------------------------------------------------
# §4.1 out-of-band stamp allocation + #204 healing proof (slice 3)
#
# The S-2 commit-gated endpoints open SQLiteStore(db_path, autocommit=False)
# and finalize via commit_after_sync (commit or rollback). Store-layer
# stamping composes with them as-is: _stamp()'s index_version_seq read and
# the payload write execute in the SAME deferred transaction, so the S-2
# rollback arm rolls the stamp back with the mutation — no orphan stamps.
# ---------------------------------------------------------------------------


class TestOutOfBandStampAllocation:
    def _labeled_db(self, tmp_path):
        """A committed store with an exported-looking face (stamp 1, seq 1)."""
        db = SQLiteStore(tmp_path / "media.sqlite", autocommit=True)
        db.init_schema(SCHEMA_PATH)
        db.stamp_seq = 1
        _add_image_with_embedding(db, "m-img")
        db.conn.execute(
            "INSERT INTO person(person_id, name, is_labeled) VALUES ('p-1', 'Alice', 1)"
        )
        db.commit()
        _add_face_with_embedding(db, "m-img", "m-img:f0", person_id=None)
        db.bump_index_version()  # seq -> 1
        db.close()
        return tmp_path / "media.sqlite"

    def test_label_allocates_seq_plus_one_transactionally(self, tmp_path):
        db_path = self._labeled_db(tmp_path)
        db = SQLiteStore(db_path, autocommit=False)  # the §4 endpoint shape
        try:
            assert db.stamp_seq is None, "no indexer run in flight"
            db.update_face_person("m-img:f0", "p-1")
            # Same-connection view: stamp = index_version_seq + 1 = 2
            assert _updated_seq(db, "face_embedding", "face_id", "m-img:f0") == 2
            # A second connection must NOT see the uncommitted stamp
            other = sqlite3.connect(str(db_path))
            try:
                row = other.execute(
                    "SELECT updated_seq FROM face_embedding WHERE face_id='m-img:f0'"
                ).fetchone()
                assert row[0] == 1, "stamp must stay inside the deferred transaction"
            finally:
                other.close()
            db.commit()  # the commit_after_sync happy arm
        finally:
            db.close()
        assert _scalar(db_path, "SELECT updated_seq FROM face_embedding WHERE face_id='m-img:f0'") == 2
        assert _scalar(db_path, "SELECT person_id FROM face WHERE face_id='m-img:f0'") == "p-1"

    def test_rename_allocates_seq_plus_one_transactionally(self, tmp_path):
        db_path = self._labeled_db(tmp_path)
        auto = SQLiteStore(db_path, autocommit=True)
        auto.stamp_seq = 1
        auto.update_face_person("m-img:f0", "p-1")
        auto.conn.execute("UPDATE face_embedding SET updated_seq = 1")
        auto.commit()
        auto.close()

        db = SQLiteStore(db_path, autocommit=False)
        try:
            db.rename_person("p-1", "Alicia")
            assert _updated_seq(db, "face_embedding", "face_id", "m-img:f0") == 2
            db.commit()
        finally:
            db.close()
        assert _scalar(db_path, "SELECT updated_seq FROM face_embedding WHERE face_id='m-img:f0'") == 2

    def test_rolled_back_guard_transaction_rolls_stamp_back(self, tmp_path):
        """The S-2 ceiling-residual arm ROLLS BACK the mutation — the stamp
        must roll back with it (no orphan stamps driving pointless
        re-exports of unchanged payloads)."""
        db_path = self._labeled_db(tmp_path)
        db = SQLiteStore(db_path, autocommit=False)
        try:
            db.update_face_person("m-img:f0", "p-1")
            assert _updated_seq(db, "face_embedding", "face_id", "m-img:f0") == 2
            db.rollback()  # commit_after_sync rejection arm
        finally:
            db.close()
        assert _scalar(db_path, "SELECT updated_seq FROM face_embedding WHERE face_id='m-img:f0'") == 1, (
            "orphan stamp survived the rollback"
        )
        assert _scalar(db_path, "SELECT person_id FROM face WHERE face_id='m-img:f0'") is None


class Test204Healing:
    def test_204_late_commit_after_watermark_is_repaired(self, tmp_path, monkeypatch):
        """#204 residual arm (close-vs-commit): the exporter records the
        watermark at seq N while a commit-gated label write — whose live
        qdrant_sync patch was swallowed — commits AFTER the exporter's read
        with stamp N+1. The stamp is the repair: the next run's dirty probe
        fires, the delta selection includes the labeled face, and the
        watermark advances so the repair runs once."""
        from msa_indexer import pipeline

        media_dir = tmp_path / "photos"
        _make_images(media_dir, 1)
        qdrant = _DeltaFakeQdrant()
        _patch_pipeline(monkeypatch, pipeline, qdrant)
        config = _delta_pipeline_config(tmp_path, media_dir)

        # Run 1: indexed + exported + watermark recorded at seq 1.
        pipeline.run_index(config)
        assert qdrant.recorded["index_version_seq"] == 1
        media_id = "sha-img_000.jpg"

        # Give the walked image an exported-looking face (stamp 1).
        setup = SQLiteStore(Path(config.sqlite_path), autocommit=True)
        setup.stamp_seq = 1
        setup.conn.execute(
            "INSERT INTO person(person_id, name, is_labeled) VALUES ('p-1', 'Alice', 1)"
        )
        setup.commit()
        _add_face_with_embedding(setup, media_id, f"{media_id}:f0", person_id=None)
        setup.conn.execute("UPDATE face_embedding SET updated_seq = 1")
        setup.commit()
        setup.close()

        # The #204 write: commit-gated endpoint shape, live patch swallowed
        # (nothing reaches Qdrant), commits AFTER the recorded watermark.
        api_db = SQLiteStore(Path(config.sqlite_path), autocommit=False)
        try:
            api_db.update_face_person(f"{media_id}:f0", "p-1")
            api_db.commit()  # commit_after_sync happy arm — 200 to the user
        finally:
            api_db.close()
        assert (
            _scalar(Path(config.sqlite_path),
                    "SELECT updated_seq FROM face_embedding WHERE face_id=?",
                    (f"{media_id}:f0",)) == 2
        ), "the late commit must carry its out-of-band stamp (seq+1)"

        # Next run (nothing changed on disk): the dirty probe fires and the
        # export runs delta since the recorded watermark.
        pipeline.run_index(config)
        assert qdrant.export_calls == 2, "#204 repair: dirty trigger must fire"
        repair_since = qdrant.since_seqs[-1]
        assert repair_since == 1

        # The row IS exported: the real delta selection at that watermark
        # yields exactly the labeled face, name attached.
        check = SQLiteStore(Path(config.sqlite_path), autocommit=True)
        try:
            rows = list(check.iter_faces(since_seq=repair_since))
        finally:
            check.close()
        assert [(r["face_id"], r["person_name"]) for r in rows] == [
            (f"{media_id}:f0", "Alice")
        ]

        # Watermark advanced — the repair runs once, not on every no-op run.
        assert qdrant.recorded["index_version_seq"] == 2
        pipeline.run_index(config)
        assert qdrant.export_calls == 2
