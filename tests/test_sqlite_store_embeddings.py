"""
Stage 3 of internal/docs/storage/SQLITE_INCREMENTAL_VISIBILITY_PLAN.md:
SQLite BLOB storage for embeddings.

Verifies:

- Schema creates the three new embedding tables on init_schema.
- upsert_image_embedding / upsert_keyframe_embedding / upsert_face_embedding
  round-trip a vector through SQLite cleanly.
- get_face_embedding returns the raw BLOB or None.
- get_keyframe_id resolves a keyframe's natural key to its auto-id.
- ON DELETE CASCADE removes embedding rows when their parent is deleted.
- Re-upserts replace the existing row rather than failing or duplicating.
"""
from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest

from msa_indexer.db.sqlite_store import SQLiteStore


SCHEMA_PATH = Path(__file__).resolve().parents[1] / "src" / "msa_indexer" / "db" / "schema.sql"


@pytest.fixture
def store(tmp_path):
    """A fresh SQLiteStore with the project schema applied."""
    db_path = tmp_path / "test.sqlite"
    s = SQLiteStore(db_path, autocommit=True)
    s.init_schema(SCHEMA_PATH)
    yield s
    s.close()


@pytest.fixture
def media_row(store):
    """Insert a media row so embedding FK constraints can be satisfied."""
    media_id = "media-1"
    store.upsert_media({
        "media_id": media_id,
        "path": "/tmp/test.jpg",
        "source_name": "test",
        "rel_path": "test.jpg",
        "size_bytes": 100,
        "mime": "image/jpeg",
    })
    return media_id


# ---------------------------------------------------------------------------
# Schema presence
# ---------------------------------------------------------------------------


def test_schema_creates_three_embedding_tables(store):
    rows = store.conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name IN "
        "('image_embedding','keyframe_embedding','face_embedding')"
    ).fetchall()
    names = {r[0] for r in rows}
    assert names == {"image_embedding", "keyframe_embedding", "face_embedding"}


def test_schema_drops_legacy_vec_meta_table_on_fresh_db(store):
    # vec_meta was the original-commit legacy table; new DBs should not
    # carry it (existing DBs harmlessly retain an empty one).
    rows = store.conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='vec_meta'"
    ).fetchall()
    assert rows == []


# ---------------------------------------------------------------------------
# upsert_image_embedding
# ---------------------------------------------------------------------------


def test_upsert_image_embedding_round_trips(store, media_row):
    vec = np.arange(768, dtype=np.float32)
    store.upsert_image_embedding(media_row, vec, model="clip-test-v1")

    row = store.conn.execute(
        "SELECT embedding, embedding_dim, embedding_model FROM image_embedding WHERE media_id=?",
        (media_row,),
    ).fetchone()
    assert row is not None
    blob, dim, model = row
    assert dim == 768
    assert model == "clip-test-v1"
    decoded = np.frombuffer(blob, dtype=np.float32)
    assert decoded.shape == (768,)
    assert np.array_equal(decoded, vec)


def test_upsert_image_embedding_replaces_on_conflict(store, media_row):
    a = np.zeros(768, dtype=np.float32)
    b = np.ones(768, dtype=np.float32)
    store.upsert_image_embedding(media_row, a, model="clip-v1")
    store.upsert_image_embedding(media_row, b, model="clip-v2")
    row = store.conn.execute(
        "SELECT embedding, embedding_model FROM image_embedding WHERE media_id=?",
        (media_row,),
    ).fetchone()
    blob, model = row
    assert model == "clip-v2"
    decoded = np.frombuffer(blob, dtype=np.float32)
    assert np.array_equal(decoded, b)


def test_upsert_image_embedding_accepts_list(store, media_row):
    """Plain Python lists should be coerced to float32 internally."""
    store.upsert_image_embedding(media_row, [1.0, 2.0, 3.0], model="t")
    blob = store.conn.execute(
        "SELECT embedding FROM image_embedding WHERE media_id=?",
        (media_row,),
    ).fetchone()[0]
    assert np.array_equal(
        np.frombuffer(blob, dtype=np.float32),
        np.array([1.0, 2.0, 3.0], dtype=np.float32),
    )


# ---------------------------------------------------------------------------
# upsert_face_embedding + get_face_embedding
# ---------------------------------------------------------------------------


@pytest.fixture
def face_row(store, media_row):
    store.add_faces(media_row, [
        {
            "face_id": "face-1",
            "bbox": (0.1, 0.1, 0.3, 0.3),
            "confidence": 0.9,
        }
    ])
    return "face-1"


def test_upsert_face_embedding_round_trips(store, face_row):
    vec = np.linspace(0.0, 1.0, 512, dtype=np.float32)
    store.upsert_face_embedding(face_row, vec, model="facenet-vggface2")

    blob = store.get_face_embedding(face_row)
    assert blob is not None
    decoded = np.frombuffer(blob, dtype=np.float32)
    assert decoded.shape == (512,)
    assert np.allclose(decoded, vec)


def test_get_face_embedding_returns_none_when_missing(store, face_row):
    assert store.get_face_embedding("nonexistent") is None
    # face exists but no embedding yet
    assert store.get_face_embedding(face_row) is None


# ---------------------------------------------------------------------------
# upsert_keyframe_embedding + get_keyframe_id
# ---------------------------------------------------------------------------


@pytest.fixture
def keyframe_row(store, media_row):
    """Insert a video_keyframes row; return its (video_id, shot_index, kf_index)."""
    # The fixture media_row is image/jpeg but we just need a parent for FK.
    store.add_keyframes(media_row, [
        {
            "shot_index": 0,
            "kf_index": 0,
            "timestamp": 1.5,
            "shot_start": 0.0,
            "shot_end": 3.0,
        }
    ])
    return (media_row, 0, 0)


def test_get_keyframe_id_resolves_natural_key(store, keyframe_row):
    video_id, shot_index, kf_index = keyframe_row
    kf_id = store.get_keyframe_id(video_id, shot_index, kf_index)
    assert isinstance(kf_id, int)
    assert kf_id > 0


def test_get_keyframe_id_returns_none_for_unknown(store, media_row):
    assert store.get_keyframe_id(media_row, 99, 99) is None


def test_upsert_keyframe_embedding_round_trips(store, keyframe_row):
    video_id, shot_index, kf_index = keyframe_row
    kf_id = store.get_keyframe_id(video_id, shot_index, kf_index)

    vec = np.full(768, 0.5, dtype=np.float32)
    store.upsert_keyframe_embedding(kf_id, vec, model="clip-test-v1")

    row = store.conn.execute(
        "SELECT embedding, embedding_dim FROM keyframe_embedding WHERE keyframe_id=?",
        (kf_id,),
    ).fetchone()
    assert row is not None
    blob, dim = row
    assert dim == 768
    decoded = np.frombuffer(blob, dtype=np.float32)
    assert np.array_equal(decoded, vec)


# ---------------------------------------------------------------------------
# ON DELETE CASCADE
# ---------------------------------------------------------------------------


def test_image_embedding_cascade_deletes_with_media(store, media_row):
    store.upsert_image_embedding(media_row, np.zeros(8, dtype=np.float32), model="t")
    assert store.conn.execute(
        "SELECT COUNT(*) FROM image_embedding WHERE media_id=?", (media_row,)
    ).fetchone()[0] == 1
    store.conn.execute("DELETE FROM media WHERE media_id=?", (media_row,))
    store.commit()
    assert store.conn.execute(
        "SELECT COUNT(*) FROM image_embedding WHERE media_id=?", (media_row,)
    ).fetchone()[0] == 0


def test_face_embedding_cascade_deletes_with_face(store, face_row):
    store.upsert_face_embedding(face_row, np.zeros(8, dtype=np.float32), model="t")
    store.conn.execute("DELETE FROM face WHERE face_id=?", (face_row,))
    store.commit()
    assert store.get_face_embedding(face_row) is None


def test_keyframe_embedding_cascade_deletes_with_keyframe(store, keyframe_row):
    video_id, shot_index, kf_index = keyframe_row
    kf_id = store.get_keyframe_id(video_id, shot_index, kf_index)
    store.upsert_keyframe_embedding(kf_id, np.zeros(8, dtype=np.float32), model="t")
    assert store.conn.execute(
        "SELECT COUNT(*) FROM keyframe_embedding WHERE keyframe_id=?", (kf_id,)
    ).fetchone()[0] == 1
    store.conn.execute("DELETE FROM video_keyframes WHERE id=?", (kf_id,))
    store.commit()
    assert store.conn.execute(
        "SELECT COUNT(*) FROM keyframe_embedding WHERE keyframe_id=?", (kf_id,)
    ).fetchone()[0] == 0


# ---------------------------------------------------------------------------
# Serializer
# ---------------------------------------------------------------------------


def test_serialize_embedding_handles_2d_input(store, media_row):
    """Caller might pass shape (1, 768); we should flatten."""
    vec_2d = np.arange(768, dtype=np.float32).reshape(1, 768)
    store.upsert_image_embedding(media_row, vec_2d, model="t")
    row = store.conn.execute(
        "SELECT embedding, embedding_dim FROM image_embedding WHERE media_id=?",
        (media_row,),
    ).fetchone()
    blob, dim = row
    assert dim == 768
    assert np.frombuffer(blob, dtype=np.float32).shape == (768,)


def test_serialize_embedding_coerces_float64_to_float32(store, media_row):
    vec_64 = np.arange(8, dtype=np.float64)
    store.upsert_image_embedding(media_row, vec_64, model="t")
    blob = store.conn.execute(
        "SELECT embedding FROM image_embedding WHERE media_id=?",
        (media_row,),
    ).fetchone()[0]
    # 8 elements * 4 bytes = 32; float64 would have been 64 bytes
    assert len(blob) == 32


# ---------------------------------------------------------------------------
# delete_faces_for_media: integration with ON DELETE CASCADE on face_embedding
# ---------------------------------------------------------------------------


def test_delete_faces_for_media_removes_face_rows(store, media_row):
    store.add_faces(media_row, [
        {"face_id": "f1", "bbox": (0, 0, 1, 1), "confidence": 0.9},
        {"face_id": "f2", "bbox": (0, 0, 1, 1), "confidence": 0.9},
    ])
    assert store.conn.execute(
        "SELECT COUNT(*) FROM face WHERE media_id=?", (media_row,)
    ).fetchone()[0] == 2

    deleted = store.delete_faces_for_media(media_row)

    assert deleted == 2
    assert store.conn.execute(
        "SELECT COUNT(*) FROM face WHERE media_id=?", (media_row,)
    ).fetchone()[0] == 0


def test_delete_faces_for_media_cascades_to_face_embedding(store, media_row):
    """The reprocess_faces=True path relies on this cascade. If it ever
    breaks, the indexer would silently leave orphan face_embedding rows
    after re-detection.
    """
    store.add_faces(media_row, [
        {"face_id": "f1", "bbox": (0, 0, 1, 1), "confidence": 0.9},
    ])
    store.upsert_face_embedding("f1", np.zeros(8, dtype=np.float32), model="t")
    assert store.conn.execute(
        "SELECT COUNT(*) FROM face_embedding WHERE face_id='f1'"
    ).fetchone()[0] == 1

    store.delete_faces_for_media(media_row)

    # Cascade should have removed the embedding row too
    assert store.conn.execute(
        "SELECT COUNT(*) FROM face_embedding WHERE face_id='f1'"
    ).fetchone()[0] == 0


def test_delete_faces_for_media_returns_zero_when_no_faces(store, media_row):
    """The method must be safe to call when no faces exist — that's the
    no-op fast path the indexer hits when reprocess_faces fires on a
    media row that never had faces detected.
    """
    deleted = store.delete_faces_for_media(media_row)
    assert deleted == 0
