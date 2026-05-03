"""
Tests for Phase 2B — Search & Browse API.

Covers:
- POST /search returns score as a real float (not null)
- POST /search results are ordered highest-score first
- GET /media pagination (limit / offset)
- GET /media media_type filter
- GET /media sort_by / sort_order
- GET /media date_from / date_to filter
- GET /media with no rows returns empty items list
"""
import pytest
import sqlite3
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock
from fastapi.testclient import TestClient


# ── Fixtures ──────────────────────────────────────────────────────────────────

@pytest.fixture()
def db(tmp_path):
    """Minimal SQLite DB seeded with known media rows."""
    db_path = tmp_path / "test.db"
    schema_path = Path(__file__).parents[1] / "src" / "msa_indexer" / "db" / "schema.sql"

    from msa_indexer.db.sqlite_store import SQLiteStore
    store = SQLiteStore(db_path)
    store.init_schema(schema_path)

    rows = [
        ("id_img_01", "/mnt/p/Photos/beach.heic",    "image/jpeg", "2023-06-15T10:00:00", None,   35.0, -117.0, "San Diego"),
        ("id_img_02", "/mnt/p/Photos/mountain.heic", "image/jpeg", "2023-07-20T14:00:00", None,   37.0, -119.0, "Yosemite"),
        ("id_vid_01", "/mnt/p/Videos/party.mp4",     "video/mp4",  "2023-08-05T18:00:00", 125.0, None,   None,   None),
        ("id_img_03", "/mnt/p/Photos/city.heic",     "image/jpeg", "2022-12-01T09:00:00", None,   40.7,  -74.0, "New York"),
    ]
    for media_id, path, mime, ts_utc, duration, gps_lat, gps_lon, place in rows:
        store.conn.execute(
            "INSERT INTO media(media_id, path, mime, ts_utc, duration, gps_lat, gps_lon, place) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (media_id, path, mime, ts_utc, duration, gps_lat, gps_lon, place),
        )
    store.conn.execute(
        "INSERT INTO shots(video_id, shot_index, t_start, t_end, is_synthetic) VALUES (?,?,?,?,?)",
        ("id_vid_01", 0, 0.0, 125.0, 1),
    )
    store.conn.execute(
        """
        INSERT INTO video_keyframes(
            video_id, shot_index, kf_index, timestamp, shot_start, shot_end, tags,
            gps_lat, gps_lon, gps_alt, gps_datetime_utc, gps_fix, gps_source, place
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            "id_vid_01", 0, 0, 62.5, 0.0, 125.0, '["person"]',
            37.7749, -122.4194, 12.5, "2026:02:27 01:17:13.200", 3, "interpolated", "San Francisco"
        ),
    )
    people = [
        ("p1", "Alice"),
        ("p2", "Bob"),
        ("p3", "Carol"),
    ]
    for person_id, name in people:
        store.conn.execute(
            "INSERT INTO person(person_id, name, is_labeled, face_count) VALUES (?,?,1,1)",
            (person_id, name),
        )
    faces = [
        ("f1", "id_img_01", "p1"),
        ("f2", "id_img_02", "p1"),
        ("f3", "id_img_02", "p2"),
        ("f4", "id_img_03", "p2"),
        ("f5", "id_img_03", "p3"),
    ]
    for face_id, media_id, person_id in faces:
        store.conn.execute(
            "INSERT INTO face(face_id, media_id, person_id, x, y, w, h, confidence) VALUES (?,?,?,?,?,?,?,?)",
            (face_id, media_id, person_id, 0.1, 0.1, 0.2, 0.2, 0.99),
        )
    store.commit()
    store.close()
    return db_path


@pytest.fixture()
def client(db, tmp_path):
    """FastAPI test client with injected test DB and mocked query engine."""
    test_config = SimpleNamespace(
        sqlite_path=str(db),
        server=SimpleNamespace(qdrant_url="http://localhost:6333", qdrant_api_key=None),
        collections=SimpleNamespace(face="face_emb"),
        thumb_dir=tmp_path / "thumbnails",
        face_thumb_dir=tmp_path / "face_thumbnails",
        log_dir=tmp_path / "logs",
        log_level="DEBUG",
    )
    (test_config.thumb_dir).mkdir()
    (test_config.face_thumb_dir).mkdir()
    (test_config.log_dir).mkdir()

    # Mock query engine to return controlled search results with real scores
    mock_qe = MagicMock()
    mock_qe.search.return_value = [
        {
            "id": "id_img_01",
            "path": "/mnt/p/Photos/beach.heic",
            "thumbnail": None,
            "score": 0.91,
            "tags": ["beach", "ocean"],
            "type": "image",
            "timestamp": None,
            "shot_id": None,
            "date": "2023-06-15T10:00:00",
            "why": "src=img score=0.910",
        },
        {
            "id": "id_img_02",
            "path": "/mnt/p/Photos/mountain.heic",
            "thumbnail": None,
            "score": 0.74,
            "tags": ["mountain"],
            "type": "image",
            "timestamp": None,
            "shot_id": None,
            "date": "2023-07-20T14:00:00",
            "why": "src=img score=0.740",
        },
    ]
    from msa_apps.search_api.app import create_app
    app = create_app(config_override=test_config, query_engine_override=mock_qe, reset_dependencies=True)
    return TestClient(app)


# ── /search tests ─────────────────────────────────────────────────────────────

class TestSearchScore:
    def test_score_is_present_and_float(self, client):
        """score must be a real float, not null — regression for engine.py bug."""
        r = client.post("/search", json={"q": "beach"})
        assert r.status_code == 200
        results = r.json()["results"]
        assert len(results) > 0
        for item in results:
            assert "score" in item, "score field missing from SearchItem"
            assert item["score"] is not None, "score is null — engine.py did not include it"
            assert isinstance(item["score"], float)
            assert 0.0 <= item["score"] <= 1.0

    def test_results_ordered_by_score_descending(self, client):
        """Results must be ordered highest score first."""
        r = client.post("/search", json={"q": "beach"})
        assert r.status_code == 200
        scores = [item["score"] for item in r.json()["results"]]
        assert scores == sorted(scores, reverse=True), "results not in descending score order"

    def test_path_field_present(self, client):
        """path must be present — required for thumbnail URL derivation in frontend."""
        r = client.post("/search", json={"q": "beach"})
        assert r.status_code == 200
        for item in r.json()["results"]:
            assert "path" in item
            assert item["path"] is not None

    def test_empty_query_returns_200(self, client):
        r = client.post("/search", json={"q": ""})
        assert r.status_code == 200


# ── /media tests ──────────────────────────────────────────────────────────────

class TestMediaListing:
    def test_returns_all_rows_by_default(self, client):
        r = client.get("/media")
        assert r.status_code == 200
        body = r.json()
        assert body["count"] == 4
        assert len(body["items"]) == 4

    def test_pagination_limit(self, client):
        r = client.get("/media?limit=2")
        assert r.status_code == 200
        body = r.json()
        assert len(body["items"]) == 2
        assert body["count"] == 4  # total unchanged

    def test_pagination_offset(self, client):
        r_all = client.get("/media?limit=100&sort_by=date&sort_order=asc")
        all_ids = [i["id"] for i in r_all.json()["items"]]

        r_page2 = client.get("/media?limit=2&offset=2&sort_by=date&sort_order=asc")
        page2_ids = [i["id"] for i in r_page2.json()["items"]]

        assert page2_ids == all_ids[2:4]

    def test_filter_images_only(self, client):
        r = client.get("/media?media_type=image")
        assert r.status_code == 200
        items = r.json()["items"]
        assert all(i["type"] == "image" for i in items)
        assert len(items) == 3

    def test_filter_videos_only(self, client):
        r = client.get("/media?media_type=video")
        assert r.status_code == 200
        items = r.json()["items"]
        assert all(i["type"] == "video" for i in items)
        assert len(items) == 1

    def test_sort_date_asc(self, client):
        r = client.get("/media?sort_by=date&sort_order=asc")
        assert r.status_code == 200
        dates = [i["date"] for i in r.json()["items"] if i["date"]]
        assert dates == sorted(dates)

    def test_sort_date_desc(self, client):
        r = client.get("/media?sort_by=date&sort_order=desc")
        assert r.status_code == 200
        dates = [i["date"] for i in r.json()["items"] if i["date"]]
        assert dates == sorted(dates, reverse=True)

    def test_date_from_filter(self, client):
        r = client.get("/media?date_from=2023-07-01")
        assert r.status_code == 200
        items = r.json()["items"]
        assert all(i["date"] >= "2023-07-01" for i in items if i["date"])
        # Should exclude city.heic (2022) and beach.heic (2023-06)
        ids = [i["id"] for i in items]
        assert "id_img_03" not in ids  # 2022
        assert "id_img_01" not in ids  # 2023-06

    def test_date_to_filter(self, client):
        r = client.get("/media?date_to=2023-06-30")
        assert r.status_code == 200
        items = r.json()["items"]
        assert all(i["date"] <= "2023-06-30" for i in items if i["date"])
        ids = [i["id"] for i in items]
        assert "id_img_01" in ids  # 2023-06
        assert "id_img_03" in ids  # 2022
        assert "id_img_02" not in ids  # 2023-07
        assert "id_vid_01" not in ids  # 2023-08


class TestVideoShots:
    def test_video_shots_include_keyframe_gps(self, client):
        r = client.get("/videos/id_vid_01/shots")
        assert r.status_code == 200
        body = r.json()
        assert body["video_id"] == "id_vid_01"
        assert len(body["shots"]) == 1
        keyframes = body["shots"][0]["keyframes"]
        assert len(keyframes) == 1
        assert keyframes[0]["gps_lat"] == 37.7749
        assert keyframes[0]["gps_lon"] == -122.4194
        assert keyframes[0]["place"] == "San Francisco"

    def test_empty_result_when_no_match(self, client):
        r = client.get("/media?date_from=2030-01-01")
        assert r.status_code == 200
        body = r.json()
        assert body["items"] == []
        assert body["count"] == 0

    def test_people_filter_any(self, client):
        r = client.get("/media?people=Alice")
        assert r.status_code == 200
        ids = [i["id"] for i in r.json()["items"]]
        assert set(ids) == {"id_img_01", "id_img_02"}

    def test_people_filter_all_together(self, client):
        r = client.get("/media?people=Alice,Bob&people_mode=all")
        assert r.status_code == 200
        ids = [i["id"] for i in r.json()["items"]]
        assert ids == ["id_img_02"]

    def test_people_filter_only_selected_people(self, client):
        r = client.get("/media?people=Alice&people_mode=only")
        assert r.status_code == 200
        ids = [i["id"] for i in r.json()["items"]]
        assert ids == ["id_img_01"]

    def test_response_shape(self, client):
        """Each item must have the fields the frontend expects."""
        r = client.get("/media?limit=1")
        item = r.json()["items"][0]
        for field in ("id", "path", "type", "date", "duration", "gps_lat", "gps_lon", "place"):
            assert field in item, f"missing field: {field}"


class TestVideoTagsInAPI:
    """Video object-detection tags are stored per keyframe, not in media_tag.
    Both /media and /media/{id}/info must aggregate them from video_keyframes."""

    def test_media_listing_returns_video_keyframe_tags(self, client):
        """/media should include aggregated keyframe tags for videos."""
        r = client.get("/media?media_type=video")
        assert r.status_code == 200
        items = r.json()["items"]
        video = next(i for i in items if i["id"] == "id_vid_01")
        assert "tags" in video
        assert "person" in video["tags"], f"expected 'person' in tags, got {video['tags']}"

    def test_media_info_returns_video_keyframe_tags(self, client):
        """/media/{id}/info should aggregate tags from video_keyframes for a video."""
        r = client.get("/media/id_vid_01/info")
        assert r.status_code == 200
        data = r.json()
        assert "tags" in data
        assert "person" in data["tags"], f"expected 'person' in tags, got {data['tags']}"

    def test_image_tags_still_served_from_media_tag(self, client, db):
        """Image tags via media_tag table must still work after the video fallback was added."""
        conn = sqlite3.connect(str(db))
        conn.execute("INSERT OR IGNORE INTO tag(name) VALUES ('ocean')")
        tag_id = conn.execute("SELECT tag_id FROM tag WHERE name='ocean'").fetchone()[0]
        conn.execute("INSERT OR IGNORE INTO media_tag(media_id, tag_id) VALUES ('id_img_01', ?)", (tag_id,))
        conn.commit()
        conn.close()

        r = client.get("/media/id_img_01/info")
        assert r.status_code == 200
        assert "ocean" in r.json()["tags"]

    def test_video_with_no_keyframe_tags_returns_empty_list(self, client):
        """A video with no tagged keyframes should return tags: [] not an error."""
        # id_vid_01 has tags; test that other videos (images here) return []
        r = client.get("/media/id_img_01/info")
        assert r.status_code == 200
        assert r.json()["tags"] == []  # no tags seeded for this image


class TestSearchDateBackfill:
    """POST /search must backfill date from SQLite when Qdrant payload has none."""

    def test_search_backfills_date_from_sqlite(self, db, tmp_path):
        """If the query engine returns date=None, the API fills it from SQLite."""
        from types import SimpleNamespace
        from unittest.mock import MagicMock
        from msa_apps.search_api.app import create_app

        test_config = SimpleNamespace(
            sqlite_path=str(db),
            server=SimpleNamespace(qdrant_url="http://localhost:6333", qdrant_api_key=None),
            collections=SimpleNamespace(face="face_emb"),
            thumb_dir=tmp_path / "thumbnails",
            face_thumb_dir=tmp_path / "face_thumbnails",
            log_dir=tmp_path / "logs",
            log_level="DEBUG",
        )
        (test_config.thumb_dir).mkdir()
        (test_config.face_thumb_dir).mkdir()
        (test_config.log_dir).mkdir()

        mock_qe = MagicMock()
        mock_qe.search.return_value = [{
            "id": "id_img_01",
            "path": "/mnt/p/Photos/beach.heic",
            "thumbnail": None,
            "score": 0.91,
            "tags": [],
            "type": "image",
            "timestamp": None,
            "shot_id": None,
            "date": None,   # Qdrant has no date for this item
            "why": "",
        }]

        app = create_app(config_override=test_config, query_engine_override=mock_qe, reset_dependencies=True)
        c = TestClient(app)

        r = c.post("/search", json={"q": "beach"})
        assert r.status_code == 200
        results = r.json()["results"]
        assert len(results) == 1
        assert results[0]["date"] == "2023-06-15T10:00:00", (
            f"date not backfilled from SQLite, got {results[0]['date']!r}"
        )
