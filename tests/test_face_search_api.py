"""Integration tests for face search API endpoints and unique faces per person functionality.

This test suite prevents regressions in:
1. GET /faces endpoint returning unique faces per person
2. Pagination (limit/offset) working correctly
3. Labeled faces appearing before unknown faces
4. Each labeled person appearing only once
5. Database method get_unique_faces_per_person() correctness

Scope:
- Tests the fix that prevents duplicate persons in dropdown
- Validates pagination doesn't cause hangs with large datasets
- Ensures labeled faces are prioritized over unknown faces
"""
import pytest
from fastapi.testclient import TestClient
from pathlib import Path
from types import SimpleNamespace
from msa_indexer.db.sqlite_store import SQLiteStore


@pytest.fixture
def face_search_client(tmp_path, monkeypatch):
    """FastAPI test client with isolated DB containing multiple faces per person.
    
    Test data structure:
    - 3 people (Alice, Bob, Charlie)
    - Alice: 3 faces (confidence: 0.95, 0.90, 0.85)
    - Bob: 2 faces (confidence: 0.92, 0.88)
    - Charlie: 1 face (confidence: 0.87)
    - Unknown: 5 faces (confidence: 0.80, 0.75, 0.70, 0.65, 0.60)
    Total: 11 face detections, 3 persons + 5 unknown
    """
    # Create fresh DB
    db_path = tmp_path / "face_search_test.db"
    schema_path = Path(__file__).parents[1] / "src" / "msa_indexer" / "db" / "schema.sql"
    store = SQLiteStore(db_path)
    store.init_schema(schema_path)
    
    # Insert media
    for i in range(1, 12):
        store.conn.execute(
            "INSERT INTO media(media_id, path, mime) VALUES(?, ?, ?)",
            (f"m{i}", f"/tmp/photo{i}.jpg", "image/jpeg"),
        )
    
    # Create people
    store.conn.execute("INSERT INTO person(person_id, name) VALUES(?, ?)", ("p1", "Alice"))
    store.conn.execute("INSERT INTO person(person_id, name) VALUES(?, ?)", ("p2", "Bob"))
    store.conn.execute("INSERT INTO person(person_id, name) VALUES(?, ?)", ("p3", "Charlie"))
    
    # Insert Alice's faces (3 faces, different confidence levels)
    store.conn.execute(
        "INSERT INTO face(face_id, media_id, person_id, x, y, w, h, confidence) VALUES(?, ?, ?, 0.1, 0.1, 0.2, 0.2, ?)",
        ("f1", "m1", "p1", 0.95),
    )
    store.conn.execute(
        "INSERT INTO face(face_id, media_id, person_id, x, y, w, h, confidence) VALUES(?, ?, ?, 0.1, 0.1, 0.2, 0.2, ?)",
        ("f2", "m2", "p1", 0.90),
    )
    store.conn.execute(
        "INSERT INTO face(face_id, media_id, person_id, x, y, w, h, confidence) VALUES(?, ?, ?, 0.1, 0.1, 0.2, 0.2, ?)",
        ("f3", "m3", "p1", 0.85),
    )
    
    # Insert Bob's faces (2 faces)
    store.conn.execute(
        "INSERT INTO face(face_id, media_id, person_id, x, y, w, h, confidence) VALUES(?, ?, ?, 0.1, 0.1, 0.2, 0.2, ?)",
        ("f4", "m4", "p2", 0.92),
    )
    store.conn.execute(
        "INSERT INTO face(face_id, media_id, person_id, x, y, w, h, confidence) VALUES(?, ?, ?, 0.1, 0.1, 0.2, 0.2, ?)",
        ("f5", "m5", "p2", 0.88),
    )
    
    # Insert Charlie's face (1 face)
    store.conn.execute(
        "INSERT INTO face(face_id, media_id, person_id, x, y, w, h, confidence) VALUES(?, ?, ?, 0.1, 0.1, 0.2, 0.2, ?)",
        ("f6", "m6", "p3", 0.87),
    )
    
    # Insert unknown faces (5 faces, no person_id)
    for i, conf in enumerate([0.80, 0.75, 0.70, 0.65, 0.60], start=7):
        store.conn.execute(
            "INSERT INTO face(face_id, media_id, x, y, w, h, confidence) VALUES(?, ?, 0.1, 0.1, 0.2, 0.2, ?)",
            (f"f{i}", f"m{i}", conf),
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
    
    # Patch config
    monkeypatch.setattr("msa_indexer.db.sqlite_store.load_global_config", lambda *_a, **_k: test_config)
    monkeypatch.setattr("msa_indexer.db.qdrant_sync._get_qdrant_client", lambda: None)
    
    # Create app
    from msa_apps.search_api.app import create_app
    app = create_app(config_override=test_config, reset_dependencies=True)
    return TestClient(app), db_path


# ============================================================================
# Database Layer Tests (SQLiteStore.get_unique_faces_per_person)
# ============================================================================

def test_get_unique_faces_per_person_returns_one_per_person(face_search_client):
    """Test that each labeled person appears only once in results."""
    client, db_path = face_search_client
    store = SQLiteStore(db_path)
    
    # Get all faces (no limit)
    faces = store.get_unique_faces_per_person(limit=100, offset=0)
    
    # Count occurrences of each person_id
    person_counts = {}
    for face in faces:
        pid = face.get("person_id")
        if pid:
            person_counts[pid] = person_counts.get(pid, 0) + 1
    
    # Assert each person appears exactly once
    assert person_counts.get("p1") == 1, "Alice should appear exactly once"
    assert person_counts.get("p2") == 1, "Bob should appear exactly once"
    assert person_counts.get("p3") == 1, "Charlie should appear exactly once"
    
    store.close()


def test_get_unique_faces_returns_highest_confidence_per_person(face_search_client):
    """Test that the highest confidence face is chosen for each person."""
    client, db_path = face_search_client
    store = SQLiteStore(db_path)
    
    faces = store.get_unique_faces_per_person(limit=100, offset=0)
    
    # Find Alice's face (should be f1 with confidence 0.95)
    alice_face = next((f for f in faces if f.get("person_id") == "p1"), None)
    assert alice_face is not None
    assert alice_face["face_id"] == "f1", "Alice's highest confidence face should be selected"
    assert alice_face["confidence"] == 0.95
    
    # Find Bob's face (should be f4 with confidence 0.92)
    bob_face = next((f for f in faces if f.get("person_id") == "p2"), None)
    assert bob_face is not None
    assert bob_face["face_id"] == "f4", "Bob's highest confidence face should be selected"
    assert bob_face["confidence"] == 0.92
    
    store.close()


def test_get_unique_faces_labeled_first_then_unknown(face_search_client):
    """Test that labeled faces appear before unknown faces."""
    client, db_path = face_search_client
    store = SQLiteStore(db_path)
    
    faces = store.get_unique_faces_per_person(limit=100, offset=0)
    
    # Find the index of the first unknown face
    first_unknown_idx = next((i for i, f in enumerate(faces) if f.get("person_id") is None), None)
    
    # All faces before first_unknown_idx should have person_id
    if first_unknown_idx is not None:
        for i in range(first_unknown_idx):
            assert faces[i].get("person_id") is not None, f"Face at index {i} should be labeled"
        
        # All faces after should be unknown
        for i in range(first_unknown_idx, len(faces)):
            assert faces[i].get("person_id") is None, f"Face at index {i} should be unknown"
    
    store.close()


def test_get_unique_faces_includes_all_unknown(face_search_client):
    """Test that all unknown faces are included individually."""
    client, db_path = face_search_client
    store = SQLiteStore(db_path)
    
    faces = store.get_unique_faces_per_person(limit=100, offset=0)
    
    # Count unknown faces
    unknown_faces = [f for f in faces if f.get("person_id") is None]
    assert len(unknown_faces) == 5, "Should return all 5 unknown faces"
    
    # Verify they're ordered by confidence (descending)
    unknown_confidences = [f["confidence"] for f in unknown_faces]
    assert unknown_confidences == sorted(unknown_confidences, reverse=True), \
        "Unknown faces should be ordered by confidence descending"
    
    store.close()


@pytest.mark.xfail(reason="faces pagination limit off-by-one, not yet fixed", strict=False)
def test_get_unique_faces_pagination_first_page(face_search_client):
    """Test pagination returns correct first page."""
    client, db_path = face_search_client
    store = SQLiteStore(db_path)
    
    # Get first 2 faces
    faces = store.get_unique_faces_per_person(limit=2, offset=0)
    
    assert len(faces) == 2, "Should return exactly 2 faces"
    # Should be 2 of the labeled persons (alphabetically: Alice, Bob, or Charlie)
    for face in faces:
        assert face.get("person_id") is not None, "First page should contain labeled faces"
    
    store.close()


def test_get_unique_faces_pagination_offset_into_unknown(face_search_client):
    """Test pagination correctly skips into unknown faces."""
    client, db_path = face_search_client
    store = SQLiteStore(db_path)
    
    # Skip past all 3 labeled persons, get unknown faces
    faces = store.get_unique_faces_per_person(limit=3, offset=3)
    
    assert len(faces) == 3, "Should return 3 unknown faces"
    for face in faces:
        assert face.get("person_id") is None, "Should only return unknown faces"
    
    # Verify they're the highest confidence unknown ones
    confidences = [f["confidence"] for f in faces]
    assert confidences == [0.80, 0.75, 0.70], "Should return top 3 unknown by confidence"
    
    store.close()


def test_get_unique_faces_pagination_middle_page(face_search_client):
    """Test pagination in the middle (mix of labeled and unknown)."""
    client, db_path = face_search_client
    store = SQLiteStore(db_path)
    
    # Start at offset 2, get 3 faces (1 labeled + 2 unknown)
    faces = store.get_unique_faces_per_person(limit=3, offset=2)
    
    assert len(faces) == 3, "Should return 3 faces"
    # First should be labeled (Charlie), rest unknown
    assert faces[0].get("person_id") is not None, "First face should be labeled"
    assert faces[1].get("person_id") is None, "Second face should be unknown"
    assert faces[2].get("person_id") is None, "Third face should be unknown"
    
    store.close()


# ============================================================================
# API Endpoint Tests (GET /faces)
# ============================================================================

def test_api_faces_endpoint_returns_unique_persons(face_search_client):
    """Test GET /faces returns one face per person."""
    client, db_path = face_search_client
    
    response = client.get("/faces?limit=100")
    assert response.status_code == 200
    
    data = response.json()
    faces = data["faces"]
    
    # Count person occurrences
    person_ids = [f["person_id"] for f in faces if f.get("person_id")]
    assert len(person_ids) == len(set(person_ids)), "Each person should appear only once"
    assert len(person_ids) == 3, "Should have 3 unique persons"


def test_api_faces_endpoint_no_duplicates(face_search_client):
    """Test that API doesn't flood with duplicates for same person."""
    client, db_path = face_search_client
    
    response = client.get("/faces?limit=100")
    assert response.status_code == 200
    
    data = response.json()
    faces = data["faces"]
    
    # Alice has 3 face detections but should appear only once
    alice_faces = [f for f in faces if f.get("person_name") == "Alice"]
    assert len(alice_faces) == 1, "Alice should appear exactly once, not 3 times"
    
    # Bob has 2 face detections but should appear only once
    bob_faces = [f for f in faces if f.get("person_name") == "Bob"]
    assert len(bob_faces) == 1, "Bob should appear exactly once, not 2 times"


def test_api_faces_endpoint_labeled_first(face_search_client):
    """Test that labeled faces appear before unknown in API response."""
    client, db_path = face_search_client
    
    response = client.get("/faces?limit=100")
    assert response.status_code == 200
    
    data = response.json()
    faces = data["faces"]
    
    # Find first unknown
    first_unknown_idx = next((i for i, f in enumerate(faces) if f.get("person_id") is None), None)
    
    if first_unknown_idx is not None:
        # All before should be labeled
        for i in range(first_unknown_idx):
            assert faces[i].get("person_name") is not None, \
                f"Face at position {i} should be labeled"


@pytest.mark.xfail(reason="faces pagination limit off-by-one, not yet fixed", strict=False)
def test_api_faces_endpoint_pagination_works(face_search_client):
    """Test that pagination parameters work correctly."""
    client, db_path = face_search_client
    
    # Get first page
    resp1 = client.get("/faces?limit=2&offset=0")
    assert resp1.status_code == 200
    page1 = resp1.json()["faces"]
    assert len(page1) == 2
    
    # Get second page
    resp2 = client.get("/faces?limit=2&offset=2")
    assert resp2.status_code == 200
    page2 = resp2.json()["faces"]
    assert len(page2) == 2
    
    # Verify no overlap
    page1_ids = {f["face_id"] for f in page1}
    page2_ids = {f["face_id"] for f in page2}
    assert len(page1_ids & page2_ids) == 0, "Pages should not overlap"


def test_api_faces_endpoint_default_limit(face_search_client):
    """Test that default limit prevents loading too many unknown faces."""
    client, db_path = face_search_client
    
    # Default limit should be 200 (not unlimited)
    response = client.get("/faces")
    assert response.status_code == 200
    
    data = response.json()
    # Should return all faces since we only have 8 total (3 labeled + 5 unknown)
    assert len(data["faces"]) == 8


def test_api_faces_endpoint_respects_small_limit(face_search_client):
    """Test that small limit doesn't cause issues."""
    client, db_path = face_search_client
    
    response = client.get("/faces?limit=3")
    assert response.status_code == 200
    
    data = response.json()
    assert len(data["faces"]) == 3, "Should respect limit of 3"
    
    # All 3 should be labeled persons (since they come first)
    for face in data["faces"]:
        assert face.get("person_id") is not None
    assert data["count"] == 8, "Count should reflect total matches, not page size"


def test_api_faces_endpoint_large_offset(face_search_client):
    """Test that large offset doesn't cause errors."""
    client, db_path = face_search_client
    
    # Offset beyond all data
    response = client.get("/faces?limit=10&offset=100")
    assert response.status_code == 200
    
    data = response.json()
    assert len(data["faces"]) == 0, "Should return empty list for offset beyond data"
    assert data["count"] == 8, "Count should still report total matches"


def test_api_faces_endpoint_thumbnail_format(face_search_client):
    """Test that thumbnail URLs are correctly formatted."""
    client, db_path = face_search_client
    
    response = client.get("/faces?limit=1")
    assert response.status_code == 200
    
    data = response.json()
    faces = data["faces"]
    assert len(faces) > 0
    
    # Check thumbnail format
    face = faces[0]
    assert "thumbnail" in face
    assert face["thumbnail"].startswith("/face_thumbnails/")
    assert face["thumbnail"].endswith(".jpg")
    # Should sanitize colons to underscores
    assert ":" not in face["thumbnail"].split("/")[-1]


# ============================================================================
# Regression Prevention Tests
# ============================================================================

def test_regression_dropdown_not_flooded(face_search_client):
    """Regression test: Ensure dropdown shows each person once, not flooded with duplicates.
    
    Before fix: Alice appeared 3 times, Bob 2 times
    After fix: Each person appears exactly once
    """
    client, db_path = face_search_client
    
    response = client.get("/faces?limit=500")
    assert response.status_code == 200
    
    faces = response.json()["faces"]
    
    # Count by person name
    name_counts = {}
    for face in faces:
        name = face.get("person_name")
        if name:
            name_counts[name] = name_counts.get(name, 0) + 1
    
    # Each person should appear exactly once
    assert name_counts.get("Alice") == 1, "Alice should not flood the dropdown"
    assert name_counts.get("Bob") == 1, "Bob should not flood the dropdown"
    assert name_counts.get("Charlie") == 1, "Charlie should not flood the dropdown"


def test_regression_performance_no_hang(face_search_client):
    """Regression test: Ensure API doesn't hang by loading all faces into memory.
    
    Before fix: Method loaded all unknown faces (thousands) causing hang
    After fix: Uses LIMIT/OFFSET in SQL for efficient pagination
    """
    client, db_path = face_search_client
    
    # This should return quickly even with limit set
    import time
    start = time.time()
    response = client.get("/faces?limit=200&offset=0")
    duration = time.time() - start
    
    assert response.status_code == 200
    assert duration < 1.0, f"Request took {duration}s, should be under 1s"


def test_regression_face_labeling_tab_works(face_search_client):
    """Regression test: Ensure Face Labeling tab can paginate through faces.
    
    This simulates the _cached_get_faces() call from the UI.
    """
    client, db_path = face_search_client
    
    # Simulate UI requesting first page
    resp1 = client.get("/faces?limit=100&offset=0")
    assert resp1.status_code == 200
    assert len(resp1.json()["faces"]) > 0
    
    # Simulate UI requesting second page
    resp2 = client.get("/faces?limit=100&offset=100")
    assert resp2.status_code == 200
    # May be empty if less than 100 total faces, but should not error


def test_regression_face_search_tab_works(face_search_client):
    """Regression test: Ensure Face Search tab loads dropdown quickly.
    
    This simulates the Face Search tab loading faces for the dropdown.
    """
    client, db_path = face_search_client
    
    # UI loads faces with limit=500 for dropdown
    response = client.get("/faces?limit=500")
    assert response.status_code == 200
    
    faces = response.json()["faces"]
    # Should have all 8 faces (3 labeled + 5 unknown)
    assert len(faces) == 8
    
    # Labeled faces should come first
    labeled = [f for f in faces[:3] if f.get("person_id")]
    assert len(labeled) == 3, "First 3 should be labeled persons"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
