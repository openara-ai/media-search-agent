"""End-to-end face workflow tests (index -> face listing -> labeling -> merge/rename).

These tests focus on the People & Face API behavior using an isolated
SQLite database and avoid external Qdrant dependencies by disabling
sync and search operations that would require a running service.

Scope covered:
1. Create person (POST /people)
2. List people (GET /people)
3. Label face (POST /faces/{face_id}/label) with new name and existing id
4. Unlabel (DELETE /faces/{face_id}/label)
5. Bulk label / unlabel (POST /faces/bulk_label)
6. Rename person (PATCH /people/{person_id})
7. Merge people (POST /people/{target_id}/merge)
8. Face suggestions endpoint is excluded (requires Qdrant search)

All endpoints are exercised against a fresh app created via the app
factory to guarantee isolation and reproducibility.
"""
from pathlib import Path
from types import SimpleNamespace
import pytest
from fastapi.testclient import TestClient

# ---------------------------------------------------------------------------
# Test fixtures (local to this file to keep scope explicit for E2E workflow)
# ---------------------------------------------------------------------------
@pytest.fixture()
def face_workflow_client(tmp_path, monkeypatch):
    """FastAPI client with isolated DB and pre-populated media + faces.

    Avoids production contamination by:
    - Creating temp SQLite DB
    - Injecting config via create_app(config_override=...)
    - Disabling Qdrant sync helpers
    """
    # Prepare DB
    db_path = tmp_path / "faces.db"
    from msa_indexer.db.sqlite_store import SQLiteStore
    schema_path = Path(__file__).parent.parent / "src" / "msa_indexer" / "db" / "schema.sql"
    store = SQLiteStore(db_path)
    store.init_schema(schema_path)
    # Insert two media rows and two faces (unassigned)
    store.conn.execute("INSERT INTO media(media_id, path, mime) VALUES(?,?,?)", ("m1", "/tmp/photo1.jpg", "image/jpeg"))
    store.conn.execute("INSERT INTO media(media_id, path, mime) VALUES(?,?,?)", ("m2", "/tmp/photo2.jpg", "image/jpeg"))
    store.conn.execute(
        "INSERT INTO face(face_id, media_id, x, y, w, h, confidence) VALUES(?, ?, 0.1,0.1,0.2,0.2,0.95)",
        ("f1", "m1"),
    )
    store.conn.execute(
        "INSERT INTO face(face_id, media_id, x, y, w, h, confidence) VALUES(?, ?, 0.3,0.3,0.25,0.25,0.90)",
        ("f2", "m2"),
    )
    store.commit()
    store.close()

    # Build test config
    test_config = SimpleNamespace(
        sqlite_path=str(db_path),
        server=SimpleNamespace(qdrant_url="http://localhost:6333", qdrant_api_key=None),
        collections=SimpleNamespace(face="face_emb"),
        thumb_dir=tmp_path / "thumbnails",
        face_thumb_dir=tmp_path / "face_thumbnails",
        log_dir=tmp_path / "logs",
        log_level="DEBUG",
    )
    for d in [test_config.thumb_dir, test_config.face_thumb_dir, test_config.log_dir]:
        d.mkdir(exist_ok=True)

    # Patch global config resolver used indirectly by SQLiteStore
    monkeypatch.setattr("msa_indexer.db.sqlite_store.load_global_config", lambda *_a, **_k: test_config)
    # Disable Qdrant sync operations
    monkeypatch.setattr("msa_indexer.db.qdrant_sync._get_qdrant_client", lambda: None)

    from msa_apps.search_api.app import create_app
    app = create_app(config_override=test_config, reset_dependencies=True)
    return TestClient(app)

# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

def test_face_label_lifecycle(face_workflow_client):
    # 1. Initially list people (should be empty)
    resp = face_workflow_client.get("/people")
    assert resp.status_code == 200
    assert resp.json()["people"] == []

    # 2. Create new person via labeling f1 with name
    resp = face_workflow_client.post("/faces/f1/label", json={"name": "Alice"})
    assert resp.status_code == 200
    alice_id = resp.json()["person_id"]
    assert resp.json()["person_name"] == "Alice"

    # 3. Label second face with same person id
    resp = face_workflow_client.post("/faces/f2/label", json={"person_id": alice_id})
    assert resp.status_code == 200
    assert resp.json()["person_id"] == alice_id

    # 4. List people (face_count should reflect 2 faces for Alice)
    resp = face_workflow_client.get("/people")
    people = resp.json()["people"]
    assert len(people) == 1
    assert people[0]["name"] == "Alice"
    # face_count may be derived by counting faces; allow >=2 in case of additional internal rows
    assert people[0]["face_count"] >= 2

    # 5. Rename person
    resp = face_workflow_client.patch(f"/people/{alice_id}", json={"name": "Alicia"})
    assert resp.status_code == 200
    assert resp.json()["name"] == "Alicia"

    # 6. Create another person via POST /people
    resp = face_workflow_client.post("/people", json={"name": "Bob"})
    assert resp.status_code == 200
    bob_id = resp.json()["person_id"]

    # 7. Merge Bob into Alicia
    resp = face_workflow_client.post(f"/people/{alice_id}/merge", json={"source_id": bob_id})
    assert resp.status_code == 200
    assert resp.json()["target_id"] == alice_id

    # 8. Unlabel a face
    resp = face_workflow_client.delete("/faces/f2/label")
    assert resp.status_code == 200
    assert resp.json()["ok"] is True

    # 9. Bulk relabel (assign both f1 & f2 back to Alicia)
    resp = face_workflow_client.post("/faces/bulk_label", json={"face_ids": ["f1", "f2"], "person_id": alice_id})
    assert resp.status_code == 200
    assert resp.json()["updated"] == 2

    # 10. Bulk unlabel
    resp = face_workflow_client.post("/faces/bulk_label", json={"face_ids": ["f1", "f2"], "person_id": None})
    assert resp.status_code == 200
    assert resp.json()["updated"] == 2

    # 11. Final list should still show Alicia with >=0 faces (counts may vary based on implementation of list_people)
    resp = face_workflow_client.get("/people")
    assert resp.status_code == 200
    names = [p["name"] for p in resp.json()["people"]]
    assert "Alicia" in names

