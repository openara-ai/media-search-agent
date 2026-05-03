from __future__ import annotations

import json
import os
import re
import sqlite3
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
            return response.status, json.loads(response.read().decode("utf-8"))
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
        """Every image-typed media row should have a matching
        image_embedding row (1:1 cardinality, FK ON DELETE CASCADE).
        """
        joined = int(sqlite_conn.execute(
            """
            SELECT COUNT(*) FROM image_embedding ie
            JOIN media m ON m.media_id = ie.media_id
            WHERE m.deleted = 0 AND m.mime LIKE 'image/%'
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
