"""
Stage 3 of internal/docs/storage/SQLITE_INCREMENTAL_VISIBILITY_PLAN.md updated this
test to seed face vectors in the SQLite face_embedding table instead of a
FaceFaissStore mock — the API now reads embeddings from SQLite directly.
"""
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest
from fastapi.testclient import TestClient


SCHEMA_PATH = Path(__file__).resolve().parents[1] / "src" / "msa_indexer" / "db" / "schema.sql"


class DummyHit:
    def __init__(self, score, payload):
        self.score = score
        self.payload = payload


class DummyQdrantClient:
    def __init__(self, *args, **kwargs):
        pass

    def search_batch(self, searches):
        # Each search returns hits from two people with varied scores
        return [
            [
                DummyHit(0.9, {"person_id": "p1", "person_name": "Alice"}),
                DummyHit(0.8, {"person_id": "p1", "person_name": "Alice"}),
                DummyHit(0.7, {"person_id": "p2", "person_name": "Bob"}),
            ]
            for _ in searches
        ]

    def search(self, **kwargs):  # Fallback path
        return [
            DummyHit(0.95, {"person_id": "p1", "person_name": "Alice"}),
            DummyHit(0.60, {"person_id": "p2", "person_name": "Bob"}),
        ]


@pytest.fixture
def seeded_db(tmp_path):
    """A SQLite DB with two face_embedding rows for f1 and f2.

    f_missing is intentionally absent so the test can exercise the
    'missing' return path. We have to stage the parent media + face
    rows because face_embedding has FKs through face -> media.
    """
    from msa_indexer.db.sqlite_store import SQLiteStore

    db_path = tmp_path / "test.sqlite"
    db = SQLiteStore(db_path, autocommit=True)
    db.init_schema(SCHEMA_PATH)
    db.upsert_media({
        "media_id": "m1",
        "path": str(tmp_path / "img1.jpg"),
        "source_name": "test",
        "rel_path": "img1.jpg",
        "size_bytes": 100,
        "mime": "image/jpeg",
    })
    db.add_faces("m1", [
        {"face_id": "f1", "bbox": (0, 0, 1, 1), "confidence": 0.9},
        {"face_id": "f2", "bbox": (0, 0, 1, 1), "confidence": 0.9},
    ])
    db.upsert_face_embedding("f1", np.ones(512, dtype="float32"), model="t")
    db.upsert_face_embedding("f2", np.ones(512, dtype="float32") * 2, model="t")
    db.close()
    return db_path


@pytest.fixture
def client(monkeypatch, seeded_db):
    test_config = SimpleNamespace(
        sqlite_path=str(seeded_db),
        qdrant_path="/tmp/qdrant_test",
        server=SimpleNamespace(),
        collections=SimpleNamespace(face="face_emb"),
    )
    from msa_apps.search_api.app import create_app
    app = create_app(config_override=test_config, reset_dependencies=True)

    monkeypatch.setattr("msa_query.storage.qdrant_client._shared", DummyQdrantClient())
    monkeypatch.setattr("msa_query.storage.qdrant_client._blocked", False)
    return TestClient(app)


def test_batch_face_suggestions_basic(client):
    resp = client.post(
        "/faces/suggestions/batch",
        json={"face_ids": ["f1", "f2", "f_missing"], "top_k": 2},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert "results" in data
    assert "missing" in data
    assert "f_missing" in data["missing"]
    for fid in ["f1", "f2"]:
        assert fid in data["results"]
        suggestions = data["results"][fid]["suggestions"]
        assert len(suggestions) == 2
        # Sorted by face_count then score; p1 should precede p2
        assert suggestions[0]["person_id"] == "p1"


def test_batch_face_suggestions_empty(client):
    resp = client.post(
        "/faces/suggestions/batch",
        json={"face_ids": ["unknown"], "top_k": 3},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["results"] == {}
    assert "unknown" in data["missing"]
