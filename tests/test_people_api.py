"""Integration tests for people/labeling API endpoints."""
import pytest
from fastapi.testclient import TestClient
from pathlib import Path
import tempfile
import shutil

@pytest.fixture
def client(tmp_path, monkeypatch):
    """FastAPI test client with fresh DB for each test using app factory."""
    # Create a fresh temporary SQLite DB
    db_path = tmp_path / "test.db"
    from msa_indexer.db.sqlite_store import SQLiteStore
    schema_path = Path(__file__).parents[1] / "src" / "msa_indexer" / "db" / "schema.sql"
    store = SQLiteStore(db_path)
    store.init_schema(schema_path)

    # Insert test media and faces
    store.conn.execute(
        "INSERT INTO media(media_id, path, mime) VALUES(?, ?, ?)",
        ("m1", "/tmp/photo1.jpg", "image/jpeg"),
    )
    store.conn.execute(
        "INSERT INTO media(media_id, path, mime) VALUES(?, ?, ?)",
        ("m2", "/tmp/photo2.jpg", "image/jpeg"),
    )
    store.conn.execute(
        "INSERT INTO face(face_id, media_id, x, y, w, h, confidence) VALUES(?, ?, 0.1, 0.1, 0.2, 0.2, 0.9)",
        ("f1", "m1"),
    )
    store.conn.execute(
        "INSERT INTO face(face_id, media_id, x, y, w, h, confidence) VALUES(?, ?, 0.3, 0.3, 0.2, 0.2, 0.85)",
        ("f2", "m2"),
    )
    store.commit()
    store.close()

    # Build test config
    from types import SimpleNamespace
    test_config = SimpleNamespace(
        sqlite_path=str(db_path),
        server=SimpleNamespace(
            qdrant_url="http://localhost:6333",
            qdrant_api_key=None,
        ),
        collections=SimpleNamespace(
            face="face_emb",
        ),
        # Optional dirs used by app mounts
        thumb_dir=tmp_path / "thumbnails",
        face_thumb_dir=tmp_path / "face_thumbnails",
        log_dir=tmp_path / "logs",
        log_level="DEBUG",
    )

    # Ensure directories exist
    (test_config.thumb_dir).mkdir(exist_ok=True)
    (test_config.face_thumb_dir).mkdir(exist_ok=True)
    (test_config.log_dir).mkdir(exist_ok=True)

    # Patch SQLiteStore global config resolver for path resolution
    monkeypatch.setattr("msa_indexer.db.sqlite_store.load_global_config", lambda *_args, **_kw: test_config)
    # Disable Qdrant sync for tests
    monkeypatch.setattr("msa_indexer.db.qdrant_sync._get_qdrant_client", lambda: None)

    # Build app via factory with injected config (no monkeypatching load_config needed)
    from msa_apps.search_api.app import create_app
    app = create_app(config_override=test_config, reset_dependencies=True)
    return TestClient(app)


def test_create_person(client):
    """Test POST /people to create a person."""
    response = client.post("/people", json={"name": "Alice"})
    assert response.status_code == 200
    data = response.json()
    assert data["name"] == "Alice"
    assert "person_id" in data
    assert data["face_count"] == 0


def test_create_duplicate_person(client):
    """Test creating a person with duplicate name."""
    client.post("/people", json={"name": "Bob"})
    response = client.post("/people", json={"name": "Bob"})
    assert response.status_code == 409  # Conflict


def test_list_people(client):
    """Test GET /people."""
    client.post("/people", json={"name": "Alice"})
    client.post("/people", json={"name": "Bob"})
    
    response = client.get("/people")
    assert response.status_code == 200
    data = response.json()
    assert "people" in data
    assert len(data["people"]) == 2
    names = sorted([p["name"] for p in data["people"]])
    assert names == ["Alice", "Bob"]


def test_rename_person(client):
    """Test PATCH /people/{id} to rename."""
    # Create person
    create_resp = client.post("/people", json={"name": "Charlie"})
    person_id = create_resp.json()["person_id"]
    
    # Rename
    response = client.patch(f"/people/{person_id}", json={"name": "Charles"})
    assert response.status_code == 200
    data = response.json()
    assert data["name"] == "Charles"
    assert data["person_id"] == person_id


def test_rename_to_existing_name(client):
    """Test renaming to an existing name."""
    client.post("/people", json={"name": "David"})
    create_resp = client.post("/people", json={"name": "Eve"})
    person_id = create_resp.json()["person_id"]
    
    response = client.patch(f"/people/{person_id}", json={"name": "David"})
    assert response.status_code == 409  # Conflict


def test_merge_people(client):
    """Test POST /people/{target_id}/merge."""
    # Create two people with unique names
    alice_resp = client.post("/people", json={"name": "AliceMerge"})
    alice_id = alice_resp.json()["person_id"]
    
    bob_resp = client.post("/people", json={"name": "BobMerge"})
    bob_id = bob_resp.json()["person_id"]
    
    # Merge Bob into Alice
    response = client.post(f"/people/{alice_id}/merge", json={"source_id": bob_id})
    assert response.status_code == 200
    data = response.json()
    assert data["target_id"] == alice_id
    
    # BobMerge should no longer exist
    people_resp = client.get("/people")
    names = [p["name"] for p in people_resp.json()["people"]]
    assert "AliceMerge" in names
    assert "BobMerge" not in names


def test_label_face_with_person_id(client):
    """Test POST /faces/{face_id}/label with existing person."""
    # Create person
    person_resp = client.post("/people", json={"name": "Frank"})
    person_id = person_resp.json()["person_id"]
    
    # Label face
    response = client.post("/faces/f1/label", json={"person_id": person_id})
    assert response.status_code == 200
    data = response.json()
    assert data["face_id"] == "f1"
    assert data["person_id"] == person_id
    assert data["person_name"] == "Frank"


def test_label_face_with_name(client):
    """Test POST /faces/{face_id}/label with name (create person)."""
    response = client.post("/faces/f2/label", json={"name": "Grace"})
    assert response.status_code == 200
    data = response.json()
    assert data["face_id"] == "f2"
    assert data["person_name"] == "Grace"
    assert "person_id" in data
    
    # Verify person was created
    people_resp = client.get("/people")
    names = [p["name"] for p in people_resp.json()["people"]]
    assert "Grace" in names


def test_unlabel_face(client):
    """Test DELETE /faces/{face_id}/label."""
    # Label first
    person_resp = client.post("/people", json={"name": "Henry"})
    person_id = person_resp.json()["person_id"]
    client.post("/faces/f1/label", json={"person_id": person_id})
    
    # Unlabel
    response = client.delete("/faces/f1/label")
    assert response.status_code == 200
    assert response.json()["ok"] is True


def test_bulk_label(client):
    """Test POST /faces/bulk_label."""
    # Create person
    person_resp = client.post("/people", json={"name": "Ivy"})
    person_id = person_resp.json()["person_id"]
    
    # Bulk assign
    response = client.post(
        "/faces/bulk_label",
        json={"face_ids": ["f1", "f2"], "person_id": person_id}
    )
    assert response.status_code == 200
    data = response.json()
    assert data["updated"] == 2


def test_bulk_unlabel(client):
    """Test POST /faces/bulk_label to unassign."""
    # Assign first
    person_resp = client.post("/people", json={"name": "Jack"})
    person_id = person_resp.json()["person_id"]
    client.post("/faces/bulk_label", json={"face_ids": ["f1", "f2"], "person_id": person_id})
    
    # Bulk unassign
    response = client.post(
        "/faces/bulk_label",
        json={"face_ids": ["f1", "f2"], "person_id": None}
    )
    assert response.status_code == 200
    data = response.json()
    assert data["updated"] == 2
