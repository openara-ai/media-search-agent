"""
Tests for internal/scripts/port_faiss_to_sqlite.py — the non-destructive
migration tool that reads pre-Stage-3 FAISS files and writes the
vectors into the new SQLite embedding tables, preserving face_id
values and manual labels.
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pytest


SRC_DIR = Path(__file__).resolve().parents[1] / "src"
SCRIPTS_DIR = Path(__file__).resolve().parents[1] / "internal" / "scripts"
SCHEMA_PATH = SRC_DIR / "msa_indexer" / "db" / "schema.sql"

if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))


@pytest.fixture
def workspace(tmp_path):
    """Create an index directory with both an empty SQLite DB and the
    legacy FAISS file paths the porter expects.
    """
    index_dir = tmp_path / "index"
    index_dir.mkdir()
    return {
        "root": tmp_path,
        "sqlite_path": index_dir / "media.sqlite",
        "faiss_path": index_dir / "image_vec.faiss",
        "face_faiss_path": index_dir / "face_vec.faiss",
    }


def _seed_sqlite(sqlite_path: Path):
    """Seed the SQLite DB with one image, one face, and one video keyframe."""
    from msa_indexer.db.sqlite_store import SQLiteStore

    db = SQLiteStore(sqlite_path, autocommit=True)
    db.init_schema(SCHEMA_PATH)

    db.upsert_media({
        "media_id": "img_a",
        "path": "/tmp/img_a.jpg",
        "source_name": "test",
        "rel_path": "img_a.jpg",
        "size_bytes": 100,
        "mime": "image/jpeg",
    })
    db.upsert_media({
        "media_id": "vid_a",
        "path": "/tmp/vid_a.mp4",
        "source_name": "test",
        "rel_path": "vid_a.mp4",
        "size_bytes": 1000,
        "mime": "video/mp4",
    })
    db.add_faces("img_a", [
        {
            "face_id": "img_a:f0",
            "bbox": (0.1, 0.1, 0.3, 0.3),
            "confidence": 0.9,
        }
    ])
    db.add_keyframes("vid_a", [
        {
            "shot_index": 0,
            "kf_index": 0,
            "timestamp": 1.5,
            "shot_start": 0.0,
            "shot_end": 3.0,
        }
    ])
    db.close()


def _seed_image_faiss(faiss_path: Path):
    """Add one image vector + one keyframe vector to the FAISS image index."""
    from msa_indexer.db.faiss_store import FaissStore

    vec = FaissStore(768, faiss_path)
    image_vec = np.full(768, 0.5, dtype=np.float32)
    keyframe_vec = np.full(768, 0.25, dtype=np.float32)
    vec.add(["img_a", "vf:vid_a:0:0"], np.stack([image_vec, keyframe_vec], axis=0))
    vec.save()


def _seed_face_faiss(face_faiss_path: Path):
    """Add one face vector to the face FAISS sidecar."""
    from msa_indexer.db.face_faiss_store import FaceFaissStore

    f = FaceFaissStore(dim=512, path=face_faiss_path)
    face_vec = np.full(512, 0.75, dtype=np.float32)
    f.add(["img_a:f0"], face_vec[None, :])
    f.save()


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


def test_porter_ports_image_face_and_keyframe_embeddings(workspace):
    _seed_sqlite(workspace["sqlite_path"])
    _seed_image_faiss(workspace["faiss_path"])
    _seed_face_faiss(workspace["face_faiss_path"])

    from port_faiss_to_sqlite import main as porter_main

    rc = porter_main([
        "--sqlite-path", str(workspace["sqlite_path"]),
        "--faiss-path", str(workspace["faiss_path"]),
        "--face-faiss-path", str(workspace["face_faiss_path"]),
    ])
    assert rc == 0

    from msa_indexer.db.sqlite_store import SQLiteStore
    db = SQLiteStore(workspace["sqlite_path"])
    try:
        # image_embedding row landed
        assert db.conn.execute(
            "SELECT COUNT(*) FROM image_embedding WHERE media_id='img_a'"
        ).fetchone()[0] == 1
        # face_embedding row landed
        blob = db.get_face_embedding("img_a:f0")
        assert blob is not None
        decoded = np.frombuffer(blob, dtype=np.float32)
        # face vectors are L2-normalized by FaceFaissStore.add, so we
        # can't assert exact equality with the seeded constant — just
        # check shape and unit norm.
        assert decoded.shape == (512,)
        assert abs(float(np.linalg.norm(decoded)) - 1.0) < 1e-5
        # keyframe_embedding row landed
        assert db.conn.execute(
            "SELECT COUNT(*) FROM keyframe_embedding"
        ).fetchone()[0] == 1
    finally:
        db.close()


# ---------------------------------------------------------------------------
# Idempotency
# ---------------------------------------------------------------------------


def test_porter_is_idempotent(workspace):
    _seed_sqlite(workspace["sqlite_path"])
    _seed_image_faiss(workspace["faiss_path"])
    _seed_face_faiss(workspace["face_faiss_path"])

    from port_faiss_to_sqlite import main as porter_main

    args = [
        "--sqlite-path", str(workspace["sqlite_path"]),
        "--faiss-path", str(workspace["faiss_path"]),
        "--face-faiss-path", str(workspace["face_faiss_path"]),
    ]
    assert porter_main(args) == 0
    assert porter_main(args) == 0  # second run

    from msa_indexer.db.sqlite_store import SQLiteStore
    db = SQLiteStore(workspace["sqlite_path"])
    try:
        # Counts should match what was in FAISS, not double up
        assert db.conn.execute("SELECT COUNT(*) FROM image_embedding").fetchone()[0] == 1
        assert db.conn.execute("SELECT COUNT(*) FROM face_embedding").fetchone()[0] == 1
        assert db.conn.execute("SELECT COUNT(*) FROM keyframe_embedding").fetchone()[0] == 1
    finally:
        db.close()


# ---------------------------------------------------------------------------
# Label preservation — the headline reason this script exists
# ---------------------------------------------------------------------------


def test_porter_preserves_face_id_and_person_id_label(workspace):
    """Critical contract: a manually-labeled face survives the port
    with both its face_id AND its person_id intact. The destructive
    --reprocess-faces path would lose the person_id; the porter must not.
    """
    from msa_indexer.db.sqlite_store import SQLiteStore

    _seed_sqlite(workspace["sqlite_path"])
    _seed_image_faiss(workspace["faiss_path"])
    _seed_face_faiss(workspace["face_faiss_path"])

    # Pretend the user labeled this face before running the migration.
    db = SQLiteStore(workspace["sqlite_path"], autocommit=True)
    try:
        person = db.create_person("Alice")
        db.conn.execute(
            "UPDATE face SET person_id = ? WHERE face_id = 'img_a:f0'",
            (person["person_id"],),
        )
        db.commit()  # autocommit only fires on helper methods, not raw execute
    finally:
        db.close()

    from port_faiss_to_sqlite import main as porter_main
    assert porter_main([
        "--sqlite-path", str(workspace["sqlite_path"]),
        "--faiss-path", str(workspace["faiss_path"]),
        "--face-faiss-path", str(workspace["face_faiss_path"]),
    ]) == 0

    db = SQLiteStore(workspace["sqlite_path"])
    try:
        row = db.conn.execute(
            "SELECT face_id, person_id FROM face WHERE face_id='img_a:f0'"
        ).fetchone()
    finally:
        db.close()
    assert row is not None
    face_id, person_id = row
    assert face_id == "img_a:f0", "porter must not change face_id"
    assert person_id is not None, (
        "person_id must survive the port — that's the whole point of "
        "the non-destructive porter"
    )


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------


def test_porter_dry_run_does_not_write(workspace):
    _seed_sqlite(workspace["sqlite_path"])
    _seed_image_faiss(workspace["faiss_path"])
    _seed_face_faiss(workspace["face_faiss_path"])

    from port_faiss_to_sqlite import main as porter_main
    assert porter_main([
        "--sqlite-path", str(workspace["sqlite_path"]),
        "--faiss-path", str(workspace["faiss_path"]),
        "--face-faiss-path", str(workspace["face_faiss_path"]),
        "--dry-run",
    ]) == 0

    from msa_indexer.db.sqlite_store import SQLiteStore
    db = SQLiteStore(workspace["sqlite_path"])
    try:
        assert db.conn.execute("SELECT COUNT(*) FROM image_embedding").fetchone()[0] == 0
        assert db.conn.execute("SELECT COUNT(*) FROM face_embedding").fetchone()[0] == 0
        assert db.conn.execute("SELECT COUNT(*) FROM keyframe_embedding").fetchone()[0] == 0
    finally:
        db.close()


def test_porter_handles_missing_faiss_files_gracefully(workspace):
    """A SQLite DB with no FAISS files alongside should produce an
    empty port without raising — that's the "fresh install" case where
    nothing needs to be migrated.
    """
    _seed_sqlite(workspace["sqlite_path"])

    from port_faiss_to_sqlite import main as porter_main
    rc = porter_main([
        "--sqlite-path", str(workspace["sqlite_path"]),
        "--faiss-path", str(workspace["faiss_path"]),  # doesn't exist
        "--face-faiss-path", str(workspace["face_faiss_path"]),  # doesn't exist
    ])
    assert rc == 0


def test_porter_returns_nonzero_when_sqlite_missing(workspace):
    from port_faiss_to_sqlite import main as porter_main
    rc = porter_main([
        "--sqlite-path", str(workspace["sqlite_path"]),  # doesn't exist
    ])
    assert rc == 2
