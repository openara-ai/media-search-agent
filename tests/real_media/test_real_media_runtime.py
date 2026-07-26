from __future__ import annotations

import json
import os
import re
import sqlite3
import time
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

import pytest


def _env_path(name: str) -> Path:
    value = os.environ.get(name)
    if not value:
        pytest.skip(f"Missing required env var: {name}")
    return Path(value)


@pytest.fixture(scope="module")
def workspace_path() -> Path:
    path = _env_path("MSA_REALDATA_WORKSPACE")
    assert path.exists()
    return path


@pytest.fixture(scope="module")
def sqlite_path() -> Path:
    path = _env_path("MSA_REALDATA_SQLITE_PATH")
    assert path.exists(), f"Missing sqlite db: {path}"
    return path


@pytest.fixture(scope="module")
def thumb_dir() -> Path:
    path = _env_path("MSA_REALDATA_THUMB_DIR")
    assert path.exists(), f"Missing thumbnail dir: {path}"
    return path


@pytest.fixture(scope="module")
def face_thumb_dir() -> Path:
    path = _env_path("MSA_REALDATA_FACE_THUMB_DIR")
    assert path.exists(), f"Missing face thumbnail dir: {path}"
    return path


@pytest.fixture(scope="module")
def fixture_root() -> Path:
    path = _env_path("MSA_REALDATA_FIXTURE_ROOT")
    assert path.exists(), f"Missing fixture root: {path}"
    return path


@pytest.fixture(scope="module")
def sqlite_conn(sqlite_path: Path):
    conn = sqlite3.connect(str(sqlite_path))
    try:
        yield conn
    finally:
        conn.close()


@pytest.fixture(scope="module")
def db_counts(sqlite_conn: sqlite3.Connection) -> dict[str, int]:
    queries = {
        "media": "SELECT COUNT(*) FROM media WHERE deleted = 0",
        "images": "SELECT COUNT(*) FROM media WHERE deleted = 0 AND mime LIKE 'image/%'",
        "videos": "SELECT COUNT(*) FROM media WHERE deleted = 0 AND mime LIKE 'video/%'",
        "faces": "SELECT COUNT(*) FROM face",
        "video_keyframes": "SELECT COUNT(*) FROM video_keyframes",
        "gps_media": "SELECT COUNT(*) FROM media WHERE deleted = 0 AND gps_lat IS NOT NULL AND gps_lon IS NOT NULL",
        "image_embedding": "SELECT COUNT(*) FROM image_embedding",
        "keyframe_embedding": "SELECT COUNT(*) FROM keyframe_embedding",
        "face_embedding": "SELECT COUNT(*) FROM face_embedding",
    }
    counts: dict[str, int] = {}
    for key, sql in queries.items():
        counts[key] = int(sqlite_conn.execute(sql).fetchone()[0])
    return counts


def _json_request(url: str, method: str = "GET", payload: dict | None = None) -> tuple[int, object]:
    body = None
    headers = {}
    if payload is not None:
        body = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"
    request = Request(url, data=body, headers=headers, method=method)
    try:
        with urlopen(request, timeout=20) as response:
            raw = response.read().decode("utf-8")
            # 204 No Content (e.g. /track/open) has an empty body — return None.
            return response.status, (json.loads(raw) if raw else None)
    except HTTPError as exc:  # pragma: no cover
        raw_body = exc.read().decode("utf-8", errors="replace")
        try:
            return exc.code, json.loads(raw_body)
        except json.JSONDecodeError as json_exc:
            raise AssertionError(f"HTTP {exc.code} from {url}: {raw_body}") from json_exc
    except URLError as exc:  # pragma: no cover
        raise AssertionError(f"HTTP request failed for {url}: {exc}") from exc


def _api_base_url() -> str | None:
    value = os.environ.get("MSA_REALDATA_BASE_URL", "").strip()
    return value or None


def _face_by_filename(faces: list[dict], filename: str) -> dict:
    for face in faces:
        path = face.get("path")
        if path and Path(path).name == filename:
            return face
    raise AssertionError(f"Could not find face for fixture filename: {filename}")


def _result_filenames(results: list[dict]) -> list[str]:
    filenames: list[str] = []
    for item in results:
        path = item.get("path")
        if path:
            filenames.append(Path(path).name)
    return filenames


def _media_row_by_filename(sqlite_conn: sqlite3.Connection, filename: str) -> tuple:
    row = sqlite_conn.execute(
        """
        SELECT media_id, path, gps_lat, gps_lon, place
        FROM media
        WHERE deleted = 0 AND path LIKE ?
        ORDER BY media_id
        LIMIT 1
        """,
        (f"%{filename}",),
    ).fetchone()
    if row is None:
        raise AssertionError(f"Could not find indexed media row for fixture filename: {filename}")
    return row


class TestIndexedArtifacts:
    def test_core_artifacts_exist(self, thumb_dir: Path, face_thumb_dir: Path):
        assert any(thumb_dir.iterdir()), "Expected thumbnails to be generated"
        assert any(face_thumb_dir.iterdir()), "Expected face thumbnails to be generated"

    def test_no_faiss_files_written(self, sqlite_path: Path):
        """Stage 3 of the SQLite incremental visibility plan eliminated
        FAISS storage. A fresh indexer run must not write any .faiss /
        .faiss.ids / .faiss.vecs.npy files into the index directory.

        Skipped when ``--skip-index`` was used in run-local.sh, since we
        then reuse the developer's pre-existing index directory which
        may legitimately contain legacy FAISS files from before the
        migration — the harness didn't run the indexer this invocation
        so the test has nothing to assert about its output.
        """
        if os.environ.get("MSA_REALDATA_INDEX_RAN", "1") != "1":
            pytest.skip("Indexer phase was skipped (--skip-index); "
                        "harness reused existing index/ which may include legacy FAISS files")
        index_dir = sqlite_path.parent
        offenders = sorted(
            p.name for p in index_dir.iterdir()
            if p.suffix == ".faiss"
            or p.name.endswith(".faiss.ids")
            or p.name.endswith(".faiss.vecs.npy")
        )
        assert not offenders, (
            f"Expected no FAISS files in {index_dir}; found: {offenders}. "
            "The pipeline should write embeddings only to SQLite "
            "(image_embedding / keyframe_embedding / face_embedding)."
        )

    def test_database_counts_are_non_zero(self, db_counts: dict[str, int]):
        assert db_counts["media"] > 0
        assert db_counts["images"] > 0
        assert db_counts["videos"] > 0
        assert db_counts["faces"] > 0
        assert db_counts["video_keyframes"] > 0
        assert db_counts["gps_media"] > 0

    def test_embedding_tables_populated(self, db_counts: dict[str, int]):
        """Stage 3: embeddings live in SQLite, with a 1:1 cardinality to
        their parent rows. The fixture has both images and videos with
        face detection enabled, so all three tables should be non-empty.
        """
        assert db_counts["image_embedding"] > 0, (
            "image_embedding table is empty — pipeline did not write image embeddings"
        )
        assert db_counts["keyframe_embedding"] > 0, (
            "keyframe_embedding table is empty — video keyframe embeddings missing"
        )
        assert db_counts["face_embedding"] > 0, (
            "face_embedding table is empty — face embeddings missing"
        )

    def test_image_embedding_matches_image_media_count(
        self, db_counts: dict[str, int], sqlite_conn: sqlite3.Connection
    ):
        """Every image_embedding row should join to an image-typed media
        row (no orphans; FK ON DELETE CASCADE intact). Tombstoned media
        (deleted = 1, from the M-8 deletion sweep / supersede paths —
        e.g. BVT Phase D) legitimately KEEP their embedding rows so a
        reappearing file resurrects without re-embedding, so the join
        deliberately does not filter on deleted.
        """
        joined = int(sqlite_conn.execute(
            """
            SELECT COUNT(*) FROM image_embedding ie
            JOIN media m ON m.media_id = ie.media_id
            WHERE m.mime LIKE 'image/%'
            """
        ).fetchone()[0])
        assert joined == db_counts["image_embedding"], (
            f"image_embedding rows ({db_counts['image_embedding']}) don't all "
            f"join to image media rows ({joined}). Possible orphan or FK breakage."
        )

    def test_face_embedding_matches_face_count(
        self, db_counts: dict[str, int], sqlite_conn: sqlite3.Connection
    ):
        """Every face row should have a matching face_embedding row when
        face recognition is enabled.
        """
        joined = int(sqlite_conn.execute(
            """
            SELECT COUNT(*) FROM face_embedding fe
            JOIN face f ON f.face_id = fe.face_id
            """
        ).fetchone()[0])
        assert joined == db_counts["face_embedding"]
        # All faces should be embedded when face recognition is enabled
        assert db_counts["face_embedding"] == db_counts["faces"], (
            f"face_embedding ({db_counts['face_embedding']}) != "
            f"face ({db_counts['faces']}); some faces missing embeddings"
        )

    def test_keyframe_embedding_matches_keyframe_count(
        self, db_counts: dict[str, int], sqlite_conn: sqlite3.Connection
    ):
        joined = int(sqlite_conn.execute(
            """
            SELECT COUNT(*) FROM keyframe_embedding ke
            JOIN video_keyframes vk ON vk.id = ke.keyframe_id
            """
        ).fetchone()[0])
        assert joined == db_counts["keyframe_embedding"]
        assert db_counts["keyframe_embedding"] == db_counts["video_keyframes"], (
            f"keyframe_embedding ({db_counts['keyframe_embedding']}) != "
            f"video_keyframes ({db_counts['video_keyframes']}); "
            "some keyframes missing embeddings"
        )

    def test_indexed_paths_point_into_fixture_tree(self, sqlite_conn: sqlite3.Connection, fixture_root: Path):
        rows = sqlite_conn.execute(
            "SELECT path FROM media WHERE deleted = 0 ORDER BY media_id LIMIT 20"
        ).fetchall()
        assert rows, "Expected indexed media rows"
        fixture_root_str = str(fixture_root)
        for (path,) in rows:
            assert fixture_root_str in str(path), f"Indexed path does not point into fixture tree: {path}"

    def test_gopro_gps_fixture_indexes_representative_keyframe_gps(self, sqlite_conn: sqlite3.Connection):
        media_id, path, gps_lat, gps_lon, place = _media_row_by_filename(
            sqlite_conn, "trimmed_gopro_gps_01.mp4"
        )
        assert path.endswith("trimmed_gopro_gps_01.mp4")

        rows = sqlite_conn.execute(
            """
            SELECT shot_index, kf_index, timestamp, gps_lat, gps_lon, place
            FROM video_keyframes
            WHERE video_id = ?
            ORDER BY shot_index, kf_index
            """,
            (media_id,),
        ).fetchall()
        assert rows, "Expected representative keyframes for GoPro GPS fixture"

        gps_rows = [row for row in rows if row[3] is not None and row[4] is not None]
        assert gps_rows, "Expected at least one representative keyframe GPS point"

        first_gps = gps_rows[0]
        assert first_gps[2] >= 0
        assert first_gps[3] is not None
        assert first_gps[4] is not None

        # Media-level GPS may be absent for timed-track videos, but if present it should be valid.
        if gps_lat is not None and gps_lon is not None:
            assert -90.0 <= gps_lat <= 90.0
            assert -180.0 <= gps_lon <= 180.0

        # If media.place was backfilled, it should be a non-empty string.
        if place is not None:
            assert isinstance(place, str)
            assert place.strip()


def _wait_for_ledger_events(ledger_dir: Path, search_id: str, timeout: float = 15.0) -> list[dict]:
    """Poll the JSONL ledger for every event tagged with `search_id`. `search`/`shown`
    are appended via a BackgroundTask (after the HTTP response), so they trail slightly;
    return as soon as search+shown+open are all present, else the best set seen by timeout.
    """
    deadline = time.monotonic() + timeout
    latest: list[dict] = []
    while True:
        events: list[dict] = []
        for path in sorted(ledger_dir.glob("events-*.jsonl")):
            try:
                lines = path.read_text(encoding="utf-8").splitlines()
            except OSError:
                continue
            for line in lines:
                line = line.strip()
                if not line:
                    continue
                try:
                    ev = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if ev.get("search_id") == search_id:
                    events.append(ev)
        latest = events
        if {"search", "shown", "open"} <= {e.get("ev") for e in events}:
            return events
        if time.monotonic() >= deadline:
            return latest
        time.sleep(0.5)


@pytest.mark.skipif(_api_base_url() is None, reason="API runtime checks skipped because MSA_REALDATA_BASE_URL is not set")
class TestRuntimeApi:
    def test_health_ready(self):
        status, payload = _json_request(f"{_api_base_url()}/health")
        assert status == 200
        assert payload["status"] == "ready"

    def test_search_endpoint_returns_json(self):
        status, payload = _json_request(f"{_api_base_url()}/search", method="POST", payload={"q": "dog"})
        assert status == 200
        assert isinstance(payload.get("results"), list)

    def test_ranker_event_capture_end_to_end(self):
        """The full label-capture loop in the INSTALLED bundle: /search returns a
        search_id, /track/open records an open, and search + shown + open all land in
        the JSONL ledger — with the shown rows carrying the serving-lib feature vector
        (computed at the engine seam even though serving stays flag-off here).

        This is the exact backend path that silently produced zero labels once (a
        stale-cached UI never fired /track/open); the BVT now guards it end to end.
        Skips cleanly if ranker logging isn't configured for the run.
        """
        ledger_dir = os.environ.get("MSA_REALDATA_LEDGER_DIR")
        if not ledger_dir:
            pytest.skip("MSA_REALDATA_LEDGER_DIR not set (ranker event logging not configured)")
        ledger = Path(ledger_dir)

        status, payload = _json_request(
            f"{_api_base_url()}/search", method="POST", payload={"q": "dog"}
        )
        assert status == 200
        search_id = payload.get("search_id")
        assert search_id, "search response must carry search_id (ADR-009) so opens correlate"
        results = payload.get("results") or []
        assert results, "need at least one result to open"
        media_id = results[0]["id"]

        status, _ = _json_request(
            f"{_api_base_url()}/track/open",
            method="POST",
            payload={"search_id": search_id, "media_id": media_id},
        )
        assert status == 204

        events = _wait_for_ledger_events(ledger, search_id, timeout=15.0)
        kinds = {e.get("ev") for e in events}
        assert {"search", "shown", "open"} <= kinds, f"missing ledger event kinds; got {kinds}"
        assert any(
            e.get("media_id") == media_id for e in events if e.get("ev") == "open"
        ), "the opened media_id was not captured as an open event"
        shown = [e for e in events if e.get("ev") == "shown"]
        assert (
            shown
            and isinstance(shown[0].get("features"), dict)
            and "sim" in shown[0]["features"]
        ), "shown rows must carry the serving-lib feature vector"

    def test_search_query_top_k_contains_expected_dog_fixtures(self):
        status, payload = _json_request(f"{_api_base_url()}/search", method="POST", payload={"q": "dog"})
        assert status == 200
        results = payload.get("results")
        assert isinstance(results, list)
        assert results, "Expected search results for dog query"

        top_filenames = _result_filenames(results[:5])
        dog_fixtures = {
            "object_dog_01.jpg",
            "exif_object_dog_01.jpg",
            "video_dog_object_01.webm",
            "video_dog_motion_01.webm",
            "trimmed_video_dog_object_01.webm",
            "trimmed_video_dog_motion_01.webm",
        }
        top_dog_hits = [name for name in top_filenames if name in dog_fixtures]

        assert top_dog_hits, f"Expected at least one dog fixture in top-5 search results, got {top_filenames}"
        assert (
            "object_dog_01.jpg" in top_filenames
            or "exif_object_dog_01.jpg" in top_filenames
        ), f"Expected a dog image fixture in top-5 search results, got {top_filenames}"

    def test_media_list_and_detail(self, fixture_root: Path):
        status, payload = _json_request(f"{_api_base_url()}/media?limit=5")
        assert status == 200
        items = payload.get("items")
        assert isinstance(items, list)
        assert payload.get("count", 0) > 0
        assert items, "Expected at least one media item"
        media_id = items[0]["id"]
        assert str(fixture_root) in str(items[0]["path"])

        info_status, info_payload = _json_request(f"{_api_base_url()}/media/{media_id}/info")
        assert info_status == 200
        assert info_payload["id"] == media_id
        assert str(fixture_root) in str(info_payload["path"])

    def test_video_media_listing_includes_keyframe_tags(self, sqlite_conn: sqlite3.Connection):
        """Videos in /media listing must aggregate object-detection tags from video_keyframes."""
        status, payload = _json_request(f"{_api_base_url()}/media?media_type=video&limit=50")
        assert status == 200
        items = payload.get("items", [])
        assert items, "Expected at least one video in /media listing"

        # Find fixture videos known to contain object-detected subjects
        tagged_fixtures = {
            "video_dog_object_01.webm",
            "trimmed_video_dog_object_01.webm",
            "video_street_objects_01.webm",
            "trimmed_video_street_objects_01.webm",
        }
        fixture_items = [
            item for item in items
            if item.get("path") and Path(item["path"]).name in tagged_fixtures
        ]
        assert fixture_items, (
            f"Expected at least one tagged-fixture video in /media listing; "
            f"got filenames: {[Path(i['path']).name for i in items if i.get('path')]}"
        )
        tagged_items = [item for item in fixture_items if item.get("tags")]
        assert tagged_items, (
            f"Expected at least one tagged-fixture video to have non-empty tags in /media listing; "
            f"fixtures found: {[Path(i['path']).name for i in fixture_items]}, "
            f"tags: {[i.get('tags') for i in fixture_items]}"
        )

    def test_video_media_info_includes_keyframe_tags(self, sqlite_conn: sqlite3.Connection):
        """GET /media/{id}/info for a video must surface object-detection tags from video_keyframes."""
        tagged_fixtures = [
            "video_dog_object_01.webm",
            "trimmed_video_dog_object_01.webm",
            "video_street_objects_01.webm",
            "trimmed_video_street_objects_01.webm",
        ]
        media_id = None
        for filename in tagged_fixtures:
            try:
                row = _media_row_by_filename(sqlite_conn, filename)
                media_id = row[0]
                break
            except AssertionError:
                continue
        assert media_id is not None, (
            f"None of the tagged fixture videos were found in the indexed database: {tagged_fixtures}"
        )

        status, payload = _json_request(f"{_api_base_url()}/media/{media_id}/info")
        assert status == 200
        tags = payload.get("tags")
        assert tags, (
            f"Expected non-empty tags in /media/{media_id}/info for a video with object detection; "
            f"got tags={tags!r}"
        )

    def test_search_results_have_non_null_dates(self):
        """Every search result must have a non-null date — regression for SQLite backfill fix."""
        status, payload = _json_request(
            f"{_api_base_url()}/search", method="POST", payload={"q": "dog"}
        )
        assert status == 200
        results = payload.get("results", [])
        assert results, "Expected search results for 'dog' query"
        missing_date = [r.get("id") for r in results if not r.get("date")]
        assert not missing_date, (
            f"Search results missing date (SQLite backfill not working): {missing_date}"
        )

    def test_gopro_gps_video_shots_endpoint_exposes_keyframe_gps(self, sqlite_conn: sqlite3.Connection):
        media_id, _path, _gps_lat, _gps_lon, _place = _media_row_by_filename(
            sqlite_conn, "trimmed_gopro_gps_01.mp4"
        )

        status, payload = _json_request(f"{_api_base_url()}/videos/{media_id}/shots")
        assert status == 200
        assert payload["video_id"] == media_id
        shots = payload.get("shots")
        assert isinstance(shots, list)
        assert shots, "Expected indexed shots for GoPro GPS fixture"

        keyframes = [kf for shot in shots for kf in shot.get("keyframes", [])]
        assert keyframes, "Expected representative keyframes in /videos/{id}/shots response"

        gps_keyframes = [
            kf for kf in keyframes
            if kf.get("gps_lat") is not None and kf.get("gps_lon") is not None
        ]
        assert gps_keyframes, "Expected at least one keyframe GPS point in shot response"

    def test_faces_endpoint(self, fixture_root: Path):
        status, payload = _json_request(f"{_api_base_url()}/faces?limit=20")
        assert status == 200
        faces = payload.get("faces")
        assert isinstance(faces, list)
        assert payload.get("count", 0) > 0
        assert faces, "Expected at least one face in API response"
        assert str(fixture_root) in str(faces[0]["path"])

    def test_face_labeling_and_similar_search_for_same_person_fixture(self):
        same_person_1 = "face_same_person_01.jpg"
        same_person_2 = "face_same_person_02.jpg"
        label_name = "Sabine Test"

        all_status, all_payload = _json_request(f"{_api_base_url()}/faces?limit=200&labeled=all")
        assert all_status == 200
        all_faces = all_payload.get("faces")
        assert isinstance(all_faces, list)

        face_1 = _face_by_filename(all_faces, same_person_1)
        face_2 = _face_by_filename(all_faces, same_person_2)

        if face_1.get("person_id") is not None:
            unlabel_status, _ = _json_request(f"{_api_base_url()}/faces/{face_1['face_id']}/label", method="DELETE")
            assert unlabel_status == 200
            all_status, all_payload = _json_request(f"{_api_base_url()}/faces?limit=200&labeled=all")
            assert all_status == 200
            all_faces = all_payload.get("faces")
            assert isinstance(all_faces, list)
            face_1 = _face_by_filename(all_faces, same_person_1)
            face_2 = _face_by_filename(all_faces, same_person_2)

        assert face_1["person_id"] is None
        assert face_2["person_id"] is None

        try:
            label_status, label_payload = _json_request(
                f"{_api_base_url()}/faces/{face_1['face_id']}/label",
                method="POST",
                payload={"name": label_name},
            )
            assert label_status == 200
            assert label_payload["face_id"] == face_1["face_id"]
            assert label_payload["person_name"] == label_name

            people_status, people_payload = _json_request(f"{_api_base_url()}/people")
            assert people_status == 200
            people = people_payload.get("people")
            assert isinstance(people, list)
            assert any(person.get("name") == label_name for person in people)

            known_status, known_payload = _json_request(f"{_api_base_url()}/faces?limit=200&labeled=known")
            assert known_status == 200
            known_faces = known_payload.get("faces")
            assert isinstance(known_faces, list)
            labeled_face = _face_by_filename(known_faces, same_person_1)
            assert labeled_face["person_name"] == label_name

            similar_status, similar_payload = _json_request(
                f"{_api_base_url()}/faces/search",
                method="POST",
                payload={"face_id": face_1["face_id"], "top_k": 10},
            )
            assert similar_status == 200
            matches = similar_payload.get("matches")
            assert isinstance(matches, list)
            assert matches, "Expected similar-face matches"

            non_self_matches = [match for match in matches if match.get("face_id") != face_1["face_id"]]
            assert non_self_matches, "Expected at least one non-self similar-face match"

            top_paths = [Path(match["path"]).name for match in non_self_matches[:5] if match.get("path")]
            assert same_person_2 in top_paths, f"Expected {same_person_2} in top similar-face matches, got {top_paths}"
        finally:
            cleanup_status, _ = _json_request(f"{_api_base_url()}/faces/{face_1['face_id']}/label", method="DELETE")
            assert cleanup_status == 200


# ---------------------------------------------------------------------------
# Per-batch commit telemetry assertions.
#
# run-local.sh exports MSA_INDEXER_COMMIT_BATCH_FILES=5 and
# MSA_INDEXER_COMMIT_BATCH_SECONDS=5 so the small fixture actually fires the
# per-batch commit path. These tests parse the indexer.log produced by the
# harness and verify the new code paths executed and that the self-checking
# acceptance criteria hold on a real indexing run.
# ---------------------------------------------------------------------------


_BATCH_COMMIT_LINE = re.compile(
    r"BATCH_COMMIT files=(?P<files>\d+) "
    r"commit_ms=(?P<commit_ms>\d+) "
    r"since_last_ms=(?P<since_last_ms>\d+) "
    r"batch_serial=(?P<batch_serial>\d+)"
)
_INDEXER_SUMMARY_LINE = re.compile(r"INDEXER_SUMMARY (?P<payload>\{.*\})")


@pytest.fixture(scope="module")
def indexer_log_path() -> Path:
    return _env_path("MSA_REALDATA_INDEXER_LOG")


@pytest.fixture(scope="module")
def indexer_log_text(indexer_log_path: Path) -> str:
    assert indexer_log_path.exists(), f"Missing indexer log: {indexer_log_path}"
    return indexer_log_path.read_text(errors="replace")


@pytest.fixture(scope="module")
def commit_stats_payload(indexer_log_text: str) -> dict:
    payloads: list[dict] = []
    for match in _INDEXER_SUMMARY_LINE.finditer(indexer_log_text):
        try:
            data = json.loads(match.group("payload"))
        except json.JSONDecodeError:
            continue
        if isinstance(data, dict) and data.get("phase") == "commit_stats":
            payloads.append(data)
    assert payloads, (
        "expected at least one INDEXER_SUMMARY phase=commit_stats line in indexer.log"
    )
    # Latest one wins if the run emitted more than one.
    return payloads[-1]


class TestPerBatchCommitTelemetry:
    def test_batch_commit_lines_emitted(self, indexer_log_text: str):
        matches = list(_BATCH_COMMIT_LINE.finditer(indexer_log_text))
        assert matches, (
            "expected at least one BATCH_COMMIT line in indexer.log; the per-batch "
            "commit path was not exercised. Verify MSA_INDEXER_COMMIT_BATCH_FILES "
            "and MSA_INDEXER_COMMIT_BATCH_SECONDS are set low enough for the fixture."
        )
        for m in matches:
            assert int(m.group("files")) > 0
            assert int(m.group("commit_ms")) >= 0
            assert int(m.group("since_last_ms")) >= 0

        # Batch serial numbers should be monotonically non-decreasing.
        serials = [int(m.group("batch_serial")) for m in matches]
        assert serials == sorted(serials), f"batch_serial out of order: {serials}"

    def test_commit_stats_summary_shape(self, commit_stats_payload: dict):
        expected_keys = {
            "phase",
            "total_commits",
            "total_commit_ms",
            "wall_clock_ms",
            "commit_pct",
            "p50_inter_commit_ms",
            "p95_inter_commit_ms",
            "max_inter_commit_ms",
        }
        assert expected_keys.issubset(commit_stats_payload.keys()), (
            f"commit_stats missing keys: {expected_keys - commit_stats_payload.keys()}"
        )
        assert commit_stats_payload["total_commits"] > 0, (
            "expected total_commits > 0 with the harness threshold overrides"
        )
        assert commit_stats_payload["wall_clock_ms"] > 0
        assert 0.0 <= commit_stats_payload["commit_pct"] <= 1.0

    def test_acceptance_criteria_met_on_real_data(
        self, commit_stats_payload: dict, indexer_log_text: str
    ):
        """The plan's acceptance criterion: commit overhead under 1% and
        median inter-commit time under 30s. If either is missed, the
        indexer logs a WARNING; assert that warning didn't fire.
        """
        commit_pct = float(commit_stats_payload["commit_pct"])
        p50 = int(commit_stats_payload["p50_inter_commit_ms"])

        assert commit_pct < 0.01, (
            f"commit_pct={commit_pct:.4f} exceeds 1%. The default per-batch "
            f"commit thresholds may need tuning for this workload. "
            f"See INDEXER_SUMMARY commit_stats: {commit_stats_payload}"
        )
        assert p50 < 30_000, (
            f"p50_inter_commit_ms={p50} exceeds 30s. The default M=15s may be "
            f"too coarse on slow file mixes. See: {commit_stats_payload}"
        )

        # Defense-in-depth: if the criteria pass, the self-checking WARNING
        # in pipeline.py should not have fired.
        offending = [
            line
            for line in indexer_log_text.splitlines()
            if "WARNING" in line and "Commit overhead" in line
        ]
        assert not offending, (
            f"unexpected commit-overhead WARNING in indexer.log: {offending}"
        )


# ---------------------------------------------------------------------------
# M-8/S-1 — incremental indexing (BVT Phases C/D, plan §6.2). The harness
# (run-local.sh / the bundle validators) performs the mutations and the
# counter assertions; this class carries the API-side contracts so both BVT
# layers reuse them. Gated on the harness signalling that the phases ran.
# ---------------------------------------------------------------------------

_INCREMENTAL_RAN = os.environ.get("MSA_REALDATA_INCREMENTAL", "0") == "1"
_SEED_MOVED_NAME = "incremental_seed_image_02.jpg"
_SEED_MOVED_REL = "incremental/moved/incremental_seed_image_02.jpg"
_SEED_DELETED_VIDEO = "incremental_seed_video_01.mp4"


@pytest.mark.skipif(
    not _INCREMENTAL_RAN,
    reason="incremental Phases C/D did not run (MSA_REALDATA_INCREMENTAL != 1)",
)
class TestIncrementalIndexing:
    def _live_row_by_rel(self, sqlite_conn, name: str):
        return sqlite_conn.execute(
            "SELECT media_id, rel_path, path FROM media WHERE rel_path LIKE ? AND deleted = 0",
            (f"%{name}",),
        ).fetchall()

    def test_moved_image_row_points_at_new_location(self, sqlite_conn: sqlite3.Connection):
        live = self._live_row_by_rel(sqlite_conn, _SEED_MOVED_NAME)
        assert live, f"no live media row for moved seed {_SEED_MOVED_NAME}"
        assert live[0][1] == _SEED_MOVED_REL, (
            f"moved image rel_path is {live[0][1]!r}, expected {_SEED_MOVED_REL!r}"
        )

    def test_deleted_video_tombstoned_in_db(self, sqlite_conn: sqlite3.Connection):
        rows = sqlite_conn.execute(
            "SELECT media_id, deleted FROM media WHERE rel_path LIKE ?",
            (f"%{_SEED_DELETED_VIDEO}",),
        ).fetchall()
        assert rows, f"no media row at all for deleted seed {_SEED_DELETED_VIDEO}"
        assert all(r[1] for r in rows), (
            f"deleted seed video still has live rows: {[r[0] for r in rows if not r[1]]}"
        )

    @pytest.mark.skipif(
        _api_base_url() is None,
        reason="API runtime checks skipped because MSA_REALDATA_BASE_URL is not set",
    )
    def test_moved_image_served_at_new_path_via_api(self, sqlite_conn: sqlite3.Connection):
        live = self._live_row_by_rel(sqlite_conn, _SEED_MOVED_NAME)
        assert live, f"no live media row for moved seed {_SEED_MOVED_NAME}"
        media_id = live[0][0]
        status, payload = _json_request(f"{_api_base_url()}/media/{media_id}/info")
        assert status == 200
        api_path = str(payload.get("path") or "")
        assert api_path.replace("\\", "/").endswith(_SEED_MOVED_REL), (
            f"/media info path {api_path!r} does not reflect the move to {_SEED_MOVED_REL!r}"
        )

    @pytest.mark.skipif(
        _api_base_url() is None,
        reason="API runtime checks skipped because MSA_REALDATA_BASE_URL is not set",
    )
    def test_deleted_video_absent_from_media_listing(self, sqlite_conn: sqlite3.Connection):
        rows = sqlite_conn.execute(
            "SELECT media_id FROM media WHERE rel_path LIKE ?",
            (f"%{_SEED_DELETED_VIDEO}",),
        ).fetchall()
        assert rows, f"no media row for deleted seed {_SEED_DELETED_VIDEO}"
        deleted_ids = {str(r[0]) for r in rows}
        status, payload = _json_request(
            f"{_api_base_url()}/media?media_type=video&limit=200"
        )
        assert status == 200
        listed_ids = {str(item.get("id")) for item in payload.get("items", [])}
        leaked = deleted_ids & listed_ids
        assert not leaked, f"tombstoned video leaked into /media listing: {leaked}"

    @pytest.mark.skipif(
        _api_base_url() is None,
        reason="API runtime checks skipped because MSA_REALDATA_BASE_URL is not set",
    )
    def test_deleted_video_absent_from_search(self, sqlite_conn: sqlite3.Connection):
        """R6: the tombstoned video's Qdrant points survive until the delta
        export ships, so the query-path deleted-media filter must drop it."""
        rows = sqlite_conn.execute(
            "SELECT media_id FROM media WHERE rel_path LIKE ?",
            (f"%{_SEED_DELETED_VIDEO}",),
        ).fetchall()
        assert rows, f"no media row for deleted seed {_SEED_DELETED_VIDEO}"
        deleted_ids = {str(r[0]) for r in rows}
        status, payload = _json_request(
            f"{_api_base_url()}/search",
            method="POST",
            payload={"q": "riding outdoors video"},
        )
        assert status == 200
        results = payload.get("results", payload if isinstance(payload, list) else [])
        result_ids = {str(r.get("id")) for r in results}
        leaked = deleted_ids & result_ids
        assert not leaked, f"tombstoned video leaked into /search results: {leaked}"


# ---------------------------------------------------------------------------
# M-8/S-2 — search availability through a full indexing run (plan §6.2).
# The Qdrant lock window shrinks to the export step via the sentinel-file
# handshake; this gate proves it end-to-end: with the API up and fixtures
# indexed, mutate one staged seed, start a run through the API, and keep
# searching for the whole run. Gated on the harness setting
# MSA_REALDATA_SEARCH_DURING_INDEXING=1 (run-local.sh and the three bundle
# validators do; plain pytest / --skip-index stay green via skip).
# ---------------------------------------------------------------------------

_SEARCH_DURING_INDEXING = os.environ.get("MSA_REALDATA_SEARCH_DURING_INDEXING", "0") == "1"
_S2_MUTATE_NAME = "incremental_seed_image_01.jpg"
# Phases where the handshake guarantees search serves the pre-run index.
# "exporting" (lock window), "export_blocked", and "complete" (emitted just
# before the export phase marker and re-emitted after it) are excluded from
# the non-empty requirement; None means the first summary hasn't landed yet.
_S2_PRE_EXPORT_PHASES = {"counting", "analyzing", "processing", "commit_stats", None}
_S2_RUN_DEADLINE_SECONDS = 1500


@pytest.mark.skipif(
    not _SEARCH_DURING_INDEXING,
    reason="search-during-indexing gate did not run (MSA_REALDATA_SEARCH_DURING_INDEXING != 1)",
)
@pytest.mark.skipif(
    _api_base_url() is None,
    reason="search-during-indexing gate requires a live API (MSA_REALDATA_BASE_URL)",
)
class TestSearchDuringIndexing:
    @pytest.fixture(scope="class")
    def run_evidence(self, fixture_root: Path) -> dict:
        """Drive one full API-started indexing run while polling /search.

        Orchestration: EXIF re-inject one staged seed (the S-1 Phase D
        idiom, distinct Artist value so the content hash changes), POST
        /indexer/start, then sample /search every 2 s bracketed by
        /indexer/status reads until the run leaves 'running'. Evidence is
        returned for the assertion methods below.
        """
        import subprocess

        base = _api_base_url()

        status, payload = _json_request(f"{base}/indexer/status")
        assert status == 200, f"/indexer/status failed: {payload}"
        assert payload.get("status") != "running", (
            "an indexer run is already active — the gate needs to own the whole run"
        )

        seed = fixture_root / "incremental" / _S2_MUTATE_NAME
        assert seed.exists(), (
            f"staged seed fixture missing: {seed} — was incremental-phases.sh seed run?"
        )
        subprocess.run(
            [
                "exiftool", "-overwrite_original",
                "-Artist=MSA-BVT-S2-SearchDuringIndexing", str(seed),
            ],
            check=True, capture_output=True,
        )

        status, payload = _json_request(f"{base}/indexer/start", method="POST")
        assert status == 200, f"POST /indexer/start failed: {payload}"

        samples: list[dict] = []
        final_status: dict | None = None
        deadline = time.monotonic() + _S2_RUN_DEADLINE_SECONDS
        while time.monotonic() < deadline:
            st_code, st = _json_request(f"{base}/indexer/status")
            assert st_code == 200
            if st.get("status") != "running":
                final_status = st
                break
            phase_before = (st.get("summary") or {}).get("phase")
            s_code, s_payload = _json_request(
                f"{base}/search", method="POST", payload={"q": "dog"}
            )
            st2_code, st2 = _json_request(f"{base}/indexer/status")
            assert st2_code == 200
            phase_after = (st2.get("summary") or {}).get("phase")
            results = s_payload.get("results") if isinstance(s_payload, dict) else None
            samples.append(
                {
                    "phase_before": phase_before,
                    "phase_after": phase_after,
                    "http_status": s_code,
                    "result_count": len(results) if isinstance(results, list) else -1,
                }
            )
            time.sleep(2)
        assert final_status is not None, (
            f"indexer run did not finish within {_S2_RUN_DEADLINE_SECONDS}s"
        )

        lg_code, lg_payload = _json_request(f"{base}/indexer/log?lines=4000")
        assert lg_code == 200
        log_lines = list(lg_payload.get("lines") or [])

        post_code, post_payload = _json_request(
            f"{base}/search", method="POST", payload={"q": "landscape"}
        )

        return {
            "samples": samples,
            "final": final_status,
            "log": log_lines,
            "post_search_status": post_code,
            "post_search": post_payload,
        }

    def test_every_search_response_is_200(self, run_evidence: dict):
        bad = [s for s in run_evidence["samples"] if s["http_status"] != 200]
        assert not bad, f"non-200 /search responses during the run: {bad}"

    def test_search_nonempty_during_pre_export_phases(self, run_evidence: dict):
        """Search must serve the pre-run index for the whole long tail of the
        run — only the export window may return empty results."""
        pre_export = [
            s
            for s in run_evidence["samples"]
            if s["phase_before"] in _S2_PRE_EXPORT_PHASES
            and s["phase_after"] in _S2_PRE_EXPORT_PHASES
        ]
        assert pre_export, (
            "no /search sample landed fully inside a pre-export phase — with "
            "model cold-load in the subprocess this window is minutes long, so "
            f"an empty set means the gate is broken. samples={run_evidence['samples']}"
        )
        empty = [s for s in pre_export if s["result_count"] <= 0]
        assert not empty, (
            f"/search returned empty results during pre-export phases: {empty} "
            "(the API surrendered the Qdrant lock outside the export window)"
        )

    def test_run_reached_complete_with_counters(self, run_evidence: dict):
        final = run_evidence["final"]
        assert final.get("status") == "complete", f"terminal status: {final}"
        summary = final.get("summary") or {}
        assert summary.get("phase") == "complete", (
            f"terminal summary phase is {summary.get('phase')!r} — the pipeline "
            "must re-emit the complete payload after the export (plan §3.4)"
        )
        for key in ("total_found", "files_hashed", "fingerprint_hits"):
            assert key in summary, f"terminal complete summary lost counter {key!r}"

    def test_mutated_content_searchable_after_run(self, run_evidence: dict):
        assert run_evidence["post_search_status"] == 200
        results = (run_evidence["post_search"] or {}).get("results")
        assert isinstance(results, list) and results, "post-run /search returned no results"
        names = _result_filenames(results)
        assert _S2_MUTATE_NAME in names, (
            f"mutated seed {_S2_MUTATE_NAME} not in post-run search results: {names}"
        )

    def test_handshake_lines_present_in_order(self, run_evidence: dict):
        lines = run_evidence["log"]

        def first_index(needle: str) -> int:
            for i, line in enumerate(lines):
                if needle in line:
                    return i
            return -1

        i_request = first_index("Qdrant handoff: request written")
        i_granted = first_index("Qdrant handoff: granted")
        i_export = first_index("Exporting indexed items to Qdrant")
        i_released = first_index("Qdrant handoff: released")
        assert i_request >= 0, "missing handshake line: request written"
        assert i_granted >= 0, (
            "missing handshake line: granted — the API watcher never answered "
            "(the run would have proceeded on the timeout path)"
        )
        assert i_export >= 0, "missing export line"
        assert i_released >= 0, "missing handshake line: released"
        assert i_request < i_granted < i_export < i_released, (
            f"handshake lines out of order: request={i_request} granted={i_granted} "
            f"export={i_export} released={i_released}"
        )

    def test_no_embedded_lock_contention_in_run_log(self, run_evidence: dict):
        contended = [l for l in run_evidence["log"] if "already accessed by another instance" in l]
        assert not contended, (
            f"embedded-Qdrant lock contention during the run: {contended}"
        )
