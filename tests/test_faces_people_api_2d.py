"""
Tests for Phase 2D — Faces / People API additions.

Covers:
- GET /faces?labeled=known  → only labeled faces
- GET /faces?labeled=unknown → only unknown faces
- GET /faces (no param)     → both (existing behaviour)
- GET /people               → includes thumbnail field per person
- list_people() DB method   → includes thumbnail_face_id
- POST /faces/label-batch   → bulk labeling (existing + new person)
- GET /media/{media_id}/info → single-item metadata endpoint
- GET /images/{media_id}    → any indexed file served regardless of media_sources config
"""
import pytest
from pathlib import Path
from types import SimpleNamespace
from fastapi.testclient import TestClient
from msa_indexer.db.sqlite_store import SQLiteStore


# ── Shared fixture ────────────────────────────────────────────────────────────

@pytest.fixture()
def db_and_client(tmp_path, monkeypatch):
    """
    Minimal DB with:
    - 2 people: Alice (face f1 conf 0.9) and Bob (face f2 conf 0.8)
    - 3 unknown faces: f3, f4, f5
    Returns (TestClient, SQLiteStore path).
    """
    db_path = tmp_path / "test2d.db"
    schema = Path(__file__).parents[1] / "src" / "msa_indexer" / "db" / "schema.sql"
    store = SQLiteStore(db_path)
    store.init_schema(schema)

    for i in range(1, 6):
        store.conn.execute(
            "INSERT INTO media(media_id, path, mime) VALUES(?,?,?)",
            (f"m{i}", f"/tmp/p{i}.jpg", "image/jpeg"),
        )
    store.conn.execute("INSERT INTO person(person_id,name) VALUES('p1','Alice')")
    store.conn.execute("INSERT INTO person(person_id,name) VALUES('p2','Bob')")
    # Alice's face
    store.conn.execute(
        "INSERT INTO face(face_id,media_id,person_id,x,y,w,h,confidence) VALUES('f1','m1','p1',0,0,1,1,0.9)"
    )
    # Bob's face
    store.conn.execute(
        "INSERT INTO face(face_id,media_id,person_id,x,y,w,h,confidence) VALUES('f2','m2','p2',0,0,1,1,0.8)"
    )
    # Unknown faces
    for i, fid, mid in [(3, 'f3', 'm3'), (4, 'f4', 'm4'), (5, 'f5', 'm5')]:
        store.conn.execute(
            "INSERT INTO face(face_id,media_id,x,y,w,h,confidence) VALUES(?,?,0,0,1,1,0.5)",
            (fid, mid),
        )
    store.commit()
    store.close()

    cfg = SimpleNamespace(
        sqlite_path=str(db_path),
        server=SimpleNamespace(qdrant_url="http://localhost:6333", qdrant_api_key=None),
        collections=SimpleNamespace(face="face_emb"),
        thumb_dir=tmp_path / "thumbnails",
        face_thumb_dir=tmp_path / "face_thumbnails",
        log_dir="logs",
        log_level="DEBUG",
        media_sources=[],
        qdrant_port=6333,
    )
    (tmp_path / "thumbnails").mkdir()
    (tmp_path / "face_thumbnails").mkdir()

    monkeypatch.chdir(tmp_path)
    (tmp_path / "config.yaml").write_text("")

    from msa_apps.search_api import indexer_manager as _im
    _idle = {"status": "idle", "run_id": None, "started_at": None,
             "finished_at": None, "elapsed_seconds": None, "return_code": None}
    monkeypatch.setattr(_im.indexer_manager, "get_status", lambda: _idle)
    monkeypatch.setattr(_im.indexer_manager, "get_log_lines", lambda tail=50: [])

    from unittest.mock import MagicMock
    mock_qe = MagicMock()
    mock_qe.search.return_value = []

    from msa_apps.search_api.app import create_app
    app = create_app(config_override=cfg, query_engine_override=mock_qe, reset_dependencies=True)
    return TestClient(app), db_path


# ── GET /faces filtering ──────────────────────────────────────────────────────

class TestFacesLabeledFilter:
    def test_no_filter_returns_all(self, db_and_client):
        client, _ = db_and_client
        body = client.get("/faces").json()
        person_ids = [f["person_id"] for f in body["faces"]]
        assert "p1" in person_ids
        assert "p2" in person_ids
        assert person_ids.count(None) == 3

    def test_labeled_known_returns_only_labeled(self, db_and_client):
        client, _ = db_and_client
        body = client.get("/faces?labeled=known").json()
        for face in body["faces"]:
            assert face["person_id"] is not None, "known filter must return only labeled faces"
        names = {f["person_name"] for f in body["faces"]}
        assert names == {"Alice", "Bob"}

    def test_labeled_unknown_returns_only_unknown(self, db_and_client):
        client, _ = db_and_client
        body = client.get("/faces?labeled=unknown").json()
        for face in body["faces"]:
            assert face["person_id"] is None, "unknown filter must return only unlabeled faces"
        assert len(body["faces"]) == 3

    def test_labeled_all_same_as_no_filter(self, db_and_client):
        client, _ = db_and_client
        body_all = client.get("/faces?labeled=all").json()
        body_none = client.get("/faces").json()
        assert len(body_all["faces"]) == len(body_none["faces"])

    def test_known_count_is_correct(self, db_and_client):
        client, _ = db_and_client
        body = client.get("/faces?labeled=known").json()
        assert body["count"] == 2  # Alice + Bob

    def test_unknown_count_is_correct(self, db_and_client):
        client, _ = db_and_client
        body = client.get("/faces?labeled=unknown").json()
        assert body["count"] == 3


# ── GET /people thumbnail ─────────────────────────────────────────────────────

class TestPeopleThumbnail:
    def test_people_includes_thumbnail_key(self, db_and_client):
        client, _ = db_and_client
        body = client.get("/people").json()
        for p in body["people"]:
            assert "thumbnail" in p, f"person {p['name']} missing thumbnail key"

    def test_thumbnail_path_format(self, db_and_client):
        client, _ = db_and_client
        body = client.get("/people").json()
        for p in body["people"]:
            if p["thumbnail"] is not None:
                assert p["thumbnail"].startswith("/face_thumbnails/")
                assert p["thumbnail"].endswith(".jpg")

    def test_thumbnail_is_none_if_no_faces(self, db_and_client):
        """A person with no faces gets thumbnail=None."""
        client, db_path = db_and_client
        store = SQLiteStore(db_path)
        store.conn.execute("INSERT INTO person(person_id,name) VALUES('p3','Charlie')")
        store.commit()
        store.close()

        body = client.get("/people").json()
        charlie = next(p for p in body["people"] if p["name"] == "Charlie")
        assert charlie["thumbnail"] is None


# ── DB layer: list_people thumbnail_face_id ───────────────────────────────────

class TestListPeopleThumbnailFaceId:
    def test_thumbnail_face_id_present(self, db_and_client):
        _, db_path = db_and_client
        store = SQLiteStore(db_path)
        people = store.list_people()
        store.close()
        alice = next(p for p in people if p["name"] == "Alice")
        assert "thumbnail_face_id" in alice
        assert alice["thumbnail_face_id"] == "f1"

    def test_thumbnail_face_id_is_highest_confidence(self, db_and_client):
        _, db_path = db_and_client
        # Add a second Alice face with lower confidence
        store = SQLiteStore(db_path)
        store.conn.execute(
            "INSERT INTO face(face_id,media_id,person_id,x,y,w,h,confidence) VALUES('f1b','m1','p1',0,0,1,1,0.3)"
        )
        store.commit()
        people = store.list_people()
        store.close()
        alice = next(p for p in people if p["name"] == "Alice")
        assert alice["thumbnail_face_id"] == "f1"  # f1 has conf 0.9 > f1b's 0.3


# ── DB layer: get_unique_faces_per_person labeled filter ─────────────────────

class TestGetUniqueFacesFilter:
    def test_known_only(self, db_and_client):
        _, db_path = db_and_client
        store = SQLiteStore(db_path)
        faces = store.get_unique_faces_per_person(limit=100, offset=0, labeled="known")
        store.close()
        assert all(f["person_id"] is not None for f in faces)
        assert len(faces) == 2

    def test_unknown_only(self, db_and_client):
        _, db_path = db_and_client
        store = SQLiteStore(db_path)
        faces = store.get_unique_faces_per_person(limit=100, offset=0, labeled="unknown")
        store.close()
        assert all(f["person_id"] is None for f in faces)
        assert len(faces) == 3

    def test_all_or_none_returns_both(self, db_and_client):
        _, db_path = db_and_client
        store = SQLiteStore(db_path)
        faces_all  = store.get_unique_faces_per_person(limit=100, offset=0, labeled="all")
        faces_none = store.get_unique_faces_per_person(limit=100, offset=0)
        store.close()
        assert len(faces_all) == len(faces_none) == 5

    def test_known_pagination(self, db_and_client):
        _, db_path = db_and_client
        store = SQLiteStore(db_path)
        page0 = store.get_unique_faces_per_person(limit=1, offset=0, labeled="known")
        page1 = store.get_unique_faces_per_person(limit=1, offset=1, labeled="known")
        store.close()
        assert len(page0) == 1
        assert len(page1) == 1
        assert page0[0]["face_id"] != page1[0]["face_id"]


# ── POST /faces/label-batch ────────────────────────────────────────────────────

class TestLabelFacesBatch:
    """POST /faces/label-batch — single-request bulk labeling."""

    def test_batch_labels_to_existing_person(self, db_and_client):
        """Labels f3, f4, f5 (unknown) to Alice in one request."""
        client, db_path = db_and_client
        res = client.post("/faces/label-batch", json={"face_ids": ["f3", "f4", "f5"], "person_id": "p1"})
        assert res.status_code == 200, res.text
        body = res.json()
        assert body["labeled"] == 3
        assert body["person_id"] == "p1"
        assert body["person_name"] == "Alice"

    def test_batch_labels_create_new_person(self, db_and_client):
        """Labels faces to a brand-new person by name."""
        client, db_path = db_and_client
        res = client.post("/faces/label-batch", json={"face_ids": ["f3", "f4"], "name": "Carol"})
        assert res.status_code == 200, res.text
        body = res.json()
        assert body["labeled"] == 2
        assert body["person_name"] == "Carol"
        assert body["person_id"]  # non-empty UUID

    def test_batch_updates_sqlite(self, db_and_client):
        """Verifies SQLite rows are actually updated after batch label."""
        client, db_path = db_and_client
        client.post("/faces/label-batch", json={"face_ids": ["f3", "f5"], "person_id": "p2"})
        store = SQLiteStore(db_path)
        rows = store.conn.execute(
            "SELECT face_id, person_id FROM face WHERE face_id IN ('f3','f5')"
        ).fetchall()
        store.close()
        for fid, pid in rows:
            assert pid == "p2", f"{fid} should be labeled p2 but got {pid}"

    def test_batch_empty_face_ids_rejected(self, db_and_client):
        """Empty face_ids list must be rejected with 400."""
        client, _ = db_and_client
        res = client.post("/faces/label-batch", json={"face_ids": [], "person_id": "p1"})
        assert res.status_code == 400

    def test_batch_missing_person_rejected(self, db_and_client):
        """Neither person_id nor name → 400."""
        client, _ = db_and_client
        res = client.post("/faces/label-batch", json={"face_ids": ["f3"]})
        assert res.status_code == 400

    def test_batch_large(self, db_and_client):
        """Batch with 500 synthetic face_ids completes without error (tests executemany scale)."""
        client, db_path = db_and_client
        # Insert 500 extra faces into DB
        store = SQLiteStore(db_path)
        store.conn.execute(
            "INSERT OR IGNORE INTO media(media_id,path,mime) VALUES('mbig','/tmp/big.jpg','image/jpeg')"
        )
        face_ids = [f"fBig{i:04d}" for i in range(500)]
        store.conn.executemany(
            "INSERT INTO face(face_id,media_id,x,y,w,h,confidence) VALUES(?,?,0,0,1,1,0.5)",
            [(fid, "mbig") for fid in face_ids],
        )
        store.commit()
        store.close()

        res = client.post("/faces/label-batch", json={"face_ids": face_ids, "person_id": "p1"})
        assert res.status_code == 200, res.text
        assert res.json()["labeled"] == 500

    def test_db_batch_method_directly(self, db_and_client):
        """Unit-test SQLiteStore.update_faces_person_batch in isolation."""
        _, db_path = db_and_client
        store = SQLiteStore(db_path)
        count = store.update_faces_person_batch(["f3", "f4", "f5"], "p2")
        store.close()
        assert count == 3


# ── GET /media/{media_id}/info ─────────────────────────────────────────────────

class TestMediaInfo:
    """GET /media/{media_id}/info — single-item metadata endpoint."""

    def test_returns_required_fields(self, db_and_client):
        """Response includes id, path, type, date, gps_lat, gps_lon, place, duration."""
        client, db_path = db_and_client
        # Enrich m1 with metadata
        store = SQLiteStore(db_path)
        store.conn.execute(
            "UPDATE media SET ts_utc=?, gps_lat=?, gps_lon=?, place=? WHERE media_id='m1'",
            ("2023-06-15T10:00:00", 37.7749, -122.4194, "San Francisco"),
        )
        store.commit()
        store.close()

        res = client.get("/media/m1/info")
        assert res.status_code == 200, res.text
        body = res.json()
        assert body["id"] == "m1"
        assert body["type"] == "image"          # mime = image/jpeg
        assert body["date"] == "2023-06-15T10:00:00"
        assert body["gps_lat"] == pytest.approx(37.7749)
        assert body["gps_lon"] == pytest.approx(-122.4194)
        assert body["place"] == "San Francisco"

    def test_type_image_from_mime(self, db_and_client):
        client, _ = db_and_client
        body = client.get("/media/m1/info").json()
        assert body["type"] == "image"

    def test_missing_media_returns_404(self, db_and_client):
        client, _ = db_and_client
        res = client.get("/media/does-not-exist/info")
        assert res.status_code == 404

    def test_deleted_media_returns_404(self, db_and_client):
        client, db_path = db_and_client
        store = SQLiteStore(db_path)
        store.conn.execute("UPDATE media SET deleted=1 WHERE media_id='m2'")
        store.commit()
        store.close()
        res = client.get("/media/m2/info")
        assert res.status_code == 404

    def test_nullable_fields_present(self, db_and_client):
        """Fields with no data (GPS, place) must still appear in the response as null."""
        client, _ = db_and_client
        body = client.get("/media/m1/info").json()
        assert "gps_lat" in body
        assert "gps_lon" in body
        assert "place" in body
        assert "duration" in body


# ── GET /images/{media_id} — path serving without source restriction ───────────

class TestPathServing:
    """
    Verify that /images and /videos serve any file that is indexed (deleted=0)
    regardless of whether its path falls under a currently configured media source.
    Previously a 403 was returned for files outside the configured source roots.
    """

    def _make_client_with_file(self, tmp_path, monkeypatch):
        """Helper: create a real JPEG on disk, index it, and return a TestClient."""
        img_path = tmp_path / "outside_source" / "photo.jpg"
        img_path.parent.mkdir(parents=True)
        # Minimal valid JPEG bytes (1×1 white pixel)
        img_path.write_bytes(
            bytes([
                0xFF, 0xD8, 0xFF, 0xE0, 0x00, 0x10, 0x4A, 0x46, 0x49, 0x46, 0x00, 0x01,
                0x01, 0x00, 0x00, 0x01, 0x00, 0x01, 0x00, 0x00, 0xFF, 0xDB, 0x00, 0x43,
                0x00, 0x08, 0x06, 0x06, 0x07, 0x06, 0x05, 0x08, 0x07, 0x07, 0x07, 0x09,
                0x09, 0x08, 0x0A, 0x0C, 0x14, 0x0D, 0x0C, 0x0B, 0x0B, 0x0C, 0x19, 0x12,
                0x13, 0x0F, 0x14, 0x1D, 0x1A, 0x1F, 0x1E, 0x1D, 0x1A, 0x1C, 0x1C, 0x20,
                0x24, 0x2E, 0x27, 0x20, 0x22, 0x2C, 0x23, 0x1C, 0x1C, 0x28, 0x37, 0x29,
                0x2C, 0x30, 0x31, 0x34, 0x34, 0x34, 0x1F, 0x27, 0x39, 0x3D, 0x38, 0x32,
                0x3C, 0x2E, 0x33, 0x34, 0x32, 0xFF, 0xC0, 0x00, 0x0B, 0x08, 0x00, 0x01,
                0x00, 0x01, 0x01, 0x01, 0x11, 0x00, 0xFF, 0xC4, 0x00, 0x1F, 0x00, 0x00,
                0x01, 0x05, 0x01, 0x01, 0x01, 0x01, 0x01, 0x01, 0x00, 0x00, 0x00, 0x00,
                0x00, 0x00, 0x00, 0x00, 0x01, 0x02, 0x03, 0x04, 0x05, 0x06, 0x07, 0x08,
                0x09, 0x0A, 0x0B, 0xFF, 0xC4, 0x00, 0xB5, 0x10, 0x00, 0x02, 0x01, 0x03,
                0x03, 0x02, 0x04, 0x03, 0x05, 0x05, 0x04, 0x04, 0x00, 0x00, 0x01, 0x7D,
                0xFF, 0xDA, 0x00, 0x08, 0x01, 0x01, 0x00, 0x00, 0x3F, 0x00, 0xFB, 0xFF,
                0xD9,
            ])
        )

        db_path = tmp_path / "srv_test.db"
        schema = Path(__file__).parents[1] / "src" / "msa_indexer" / "db" / "schema.sql"
        store = SQLiteStore(db_path)
        store.init_schema(schema)
        store.conn.execute(
            "INSERT INTO media(media_id, path, mime) VALUES('srv1', ?, 'image/jpeg')",
            (str(img_path),),
        )
        store.commit()
        store.close()

        # media_sources intentionally does NOT include img_path's parent
        cfg = SimpleNamespace(
            sqlite_path=str(db_path),
            server=SimpleNamespace(qdrant_url="http://localhost:6333", qdrant_api_key=None),
            collections=SimpleNamespace(face="face_emb"),
            thumb_dir=tmp_path / "thumbnails",
            face_thumb_dir=tmp_path / "face_thumbnails",
            log_dir="logs",
            log_level="DEBUG",
            media_sources=[],   # empty — file is "outside" any configured source
            qdrant_port=6333,
        )
        (tmp_path / "thumbnails").mkdir(exist_ok=True)
        (tmp_path / "face_thumbnails").mkdir(exist_ok=True)
        monkeypatch.chdir(tmp_path)
        (tmp_path / "config.yaml").write_text("")

        from msa_apps.search_api import indexer_manager as _im
        _idle = {"status": "idle", "run_id": None, "started_at": None,
                 "finished_at": None, "elapsed_seconds": None, "return_code": None}
        monkeypatch.setattr(_im.indexer_manager, "get_status", lambda: _idle)
        monkeypatch.setattr(_im.indexer_manager, "get_log_lines", lambda tail=50: [])

        from unittest.mock import MagicMock
        mock_qe = MagicMock()
        mock_qe.search.return_value = []

        from msa_apps.search_api.app import create_app
        client = TestClient(create_app(config_override=cfg, query_engine_override=mock_qe, reset_dependencies=True))
        return client

    def test_image_outside_source_served(self, tmp_path, monkeypatch):
        """Indexed image outside any media_source must be served (not 403/404)."""
        client = self._make_client_with_file(tmp_path, monkeypatch)
        res = client.get("/images/srv1")
        assert res.status_code == 200, f"Expected 200, got {res.status_code}: {res.text}"
        assert "image" in res.headers.get("content-type", "")

    def test_unknown_media_id_returns_404(self, tmp_path, monkeypatch):
        """Non-existent media_id must return 404."""
        client = self._make_client_with_file(tmp_path, monkeypatch)
        res = client.get("/images/no-such-id")
        assert res.status_code == 404
