from types import SimpleNamespace
import numpy as np
from PIL import Image
import pytest


class _FakeSQLiteStore:
    _state = {
        "index_version_seq": 0,
        "index_version_ts": None,
    }

    def __init__(self, _path: str, autocommit: bool = True):
        self.autocommit = autocommit

    def init_schema(self, _path):
        pass

    def get_processing_status(self, media_id: str):
        return {
            "gps_processed": True,
            "object_detection_done": True,
            "face_detection_done": True,
            "embeddings_version": "test-v1" if media_id == "img-id" else "different-version",
        }

    def media_exists(self, _media_id: str) -> bool:
        return True

    def has_image_embedding(self, _media_id: str) -> bool:
        return True

    def media_has_unembedded_keyframes(self, _media_id: str) -> bool:
        return False

    def media_has_unembedded_faces(self, _media_id: str) -> bool:
        return False

    def count_orphan_face_embeddings(self) -> int:
        return 0

    def get_shots_for_video(self, _media_id: str):
        return [{"t_start": 0.0, "t_end": 1.0}]

    def has_keyframes_for_video(self, _media_id: str) -> bool:
        return True

    def upsert_media(self, _row):
        pass

    def get_total_changes(self) -> int:
        return 0

    def get_index_state(self):
        return dict(self._state)

    def bump_index_version(self):
        self.__class__._state["index_version_seq"] += 1
        self.__class__._state["index_version_ts"] = "2026-04-16T00:00:00Z"
        return {
            "index_version_seq": self.__class__._state["index_version_seq"],
            "index_version_ts": self.__class__._state["index_version_ts"],
        }

    def commit(self):
        pass

    def close(self):
        pass


class _FakeClipEmbedder:
    dim = 768

    def __init__(self, *_args, **_kwargs):
        pass


class _FakeFaissStore:
    fail_on_save = False

    def __init__(self, *_args, **_kwargs):
        pass

    def load(self):
        pass

    def add(self, *_args, **_kwargs):
        pass

    def save(self):
        if self.fail_on_save:
            raise RuntimeError("FAISS save failed")


class _FakeSQLiteStoreNeedsProcessing:
    _state = {
        "index_version_seq": 0,
        "index_version_ts": None,
    }

    def __init__(self, _path: str, autocommit: bool = True):
        self.autocommit = autocommit
        self.committed = False
        self.rolled_back = False
        # Track write count so get_total_changes() can simulate growth,
        # which the pipeline uses to detect "anything new written this run".
        self._writes = 0

    def init_schema(self, _path):
        pass

    def get_processing_status(self, _media_id: str):
        return {
            "gps_processed": False,
            "object_detection_done": False,
            "face_detection_done": False,
            "embeddings_version": None,
        }

    def media_exists(self, _media_id: str) -> bool:
        return False

    def has_image_embedding(self, _media_id: str) -> bool:
        return False

    def media_has_unembedded_keyframes(self, _media_id: str) -> bool:
        return False

    def media_has_unembedded_faces(self, _media_id: str) -> bool:
        return False

    def count_orphan_face_embeddings(self) -> int:
        return 0

    def upsert_media(self, _row):
        self._writes += 1

    def mark_embeddings_done(self, _media_id: str, _model_version: str):
        self._writes += 1

    def upsert_image_embedding(self, _media_id, _embedding, model: str):
        self._writes += 1

    def upsert_face_embedding(self, _face_id, _embedding, model: str):
        self._writes += 1

    def upsert_keyframe_embedding(self, _keyframe_id, _embedding, model: str):
        self._writes += 1

    def get_keyframe_id(self, _video_id, _shot_index, _kf_index):
        return 1

    def add_faces(self, _media_id, _faces):
        self._writes += 1

    def add_keyframes(self, _video_id, _entries):
        self._writes += 1

    def update_media_fields(self, _media_id, _fields):
        self._writes += 1

    def mark_face_detection_done(self, _media_id):
        self._writes += 1

    def get_total_changes(self) -> int:
        return self._writes

    def get_index_state(self):
        return dict(self._state)

    def bump_index_version(self):
        self.__class__._state["index_version_seq"] += 1
        self.__class__._state["index_version_ts"] = "2026-04-16T00:00:00Z"
        return {
            "index_version_seq": self.__class__._state["index_version_seq"],
            "index_version_ts": self.__class__._state["index_version_ts"],
        }

    def commit(self):
        self.committed = True

    def rollback(self):
        self.rolled_back = True

    def close(self):
        pass


class _FakeClipEmbedderWithVectors:
    dim = 768

    def __init__(self, *_args, **_kwargs):
        pass

    def image_embed(self, images):
        return [np.zeros(self.dim, dtype=np.float32) for _ in images]


@pytest.fixture(autouse=True)
def _reset_fake_store_state():
    _FakeSQLiteStore._state = {
        "index_version_seq": 0,
        "index_version_ts": None,
    }
    _FakeSQLiteStoreNeedsProcessing._state = {
        "index_version_seq": 0,
        "index_version_ts": None,
    }
    _FakeFaissStore.fail_on_save = False
    yield
    _FakeSQLiteStore._state = {
        "index_version_seq": 0,
        "index_version_ts": None,
    }
    _FakeSQLiteStoreNeedsProcessing._state = {
        "index_version_seq": 0,
        "index_version_ts": None,
    }
    _FakeFaissStore.fail_on_save = False


def test_complete_summary_counts_up_to_date_videos(tmp_path, monkeypatch):
    from msa_indexer import pipeline

    image_path = tmp_path / "image.jpg"
    video_path = tmp_path / "video.mp4"
    image_path.write_bytes(b"img")
    video_path.write_bytes(b"vid")

    summaries: list[dict] = []

    monkeypatch.setattr(pipeline, "resolve_for_access", lambda p: p)
    monkeypatch.setattr(pipeline, "iter_media", lambda *_args, **_kwargs: [image_path, video_path])
    monkeypatch.setattr(pipeline, "sha256_of_file", lambda p: "img-id" if p.suffix.lower() == ".jpg" else "vid-id")
    monkeypatch.setattr(pipeline, "get_video_meta", lambda _p, **_kwargs: {"duration": 5.0})
    monkeypatch.setattr(pipeline, "SQLiteStore", _FakeSQLiteStore)
    monkeypatch.setattr(pipeline, "ClipEmbedder", _FakeClipEmbedder)
    monkeypatch.setattr(pipeline, "_load_historical_perf", lambda _p: (1.0, 30.0, 5.0))
    monkeypatch.setattr(pipeline, "_emit_indexer_summary", lambda **payload: summaries.append(payload))
    monkeypatch.setattr(pipeline, "get_qdrant_export_version", lambda: None)
    monkeypatch.setattr(pipeline, "record_qdrant_export_version", lambda *_args, **_kwargs: None)

    config = SimpleNamespace(
        sqlite_path=str(tmp_path / "test.db"),
        faiss_path=tmp_path / "test.faiss",
        face_faiss_path=tmp_path / "test-face.faiss",
        thumb_dir=tmp_path / "thumbs",
        face_thumb_dir=tmp_path / "face-thumbs",
        log_dir=tmp_path / "logs",
        model_name="ViT-L-14",
        pretrained="openai",
        model_version="test-v1",
        device="cpu",
        enable_object_detection=False,
        enable_face_recognition=False,
        media_sources=[SimpleNamespace(name="photos", path=str(tmp_path), enabled=True)],
    )

    pipeline.run_index(config)

    assert summaries, "expected at least one summary payload"
    complete = summaries[-1]
    assert complete["phase"] == "complete"
    assert complete["total_found"] == 2
    assert complete["already_indexed"] == 2
    assert complete["skipped"] == 2
    assert complete["processed_images"] == 0
    assert complete["processed_videos"] == 0


def test_noop_run_skips_qdrant_export_by_default(tmp_path, monkeypatch):
    from msa_indexer import pipeline

    image_path = tmp_path / "image.jpg"
    video_path = tmp_path / "video.mp4"
    image_path.write_bytes(b"img")
    video_path.write_bytes(b"vid")

    export_calls = []

    monkeypatch.setattr(pipeline, "resolve_for_access", lambda p: p)
    monkeypatch.setattr(pipeline, "iter_media", lambda *_args, **_kwargs: [image_path, video_path])
    monkeypatch.setattr(pipeline, "sha256_of_file", lambda p: "img-id" if p.suffix.lower() == ".jpg" else "vid-id")
    monkeypatch.setattr(pipeline, "get_video_meta", lambda _p, **_kwargs: {"duration": 5.0})
    monkeypatch.setattr(pipeline, "SQLiteStore", _FakeSQLiteStore)
    monkeypatch.setattr(pipeline, "ClipEmbedder", _FakeClipEmbedder)
    monkeypatch.setattr(pipeline, "_load_historical_perf", lambda _p: (1.0, 30.0, 5.0))
    monkeypatch.setattr(pipeline, "_emit_indexer_summary", lambda **_payload: None)
    monkeypatch.setattr(pipeline, "get_qdrant_export_version", lambda: None)
    monkeypatch.setattr(pipeline, "record_qdrant_export_version", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        pipeline,
        "_do_qdrant_export",
        lambda *args, **kwargs: export_calls.append((args, kwargs)),
    )

    config = SimpleNamespace(
        sqlite_path=str(tmp_path / "test.db"),
        faiss_path=tmp_path / "test.faiss",
        face_faiss_path=tmp_path / "test-face.faiss",
        thumb_dir=tmp_path / "thumbs",
        face_thumb_dir=tmp_path / "face-thumbs",
        log_dir=tmp_path / "logs",
        model_name="ViT-L-14",
        pretrained="openai",
        model_version="test-v1",
        device="cpu",
        enable_object_detection=False,
        enable_face_recognition=False,
        media_sources=[SimpleNamespace(name="photos", path=str(tmp_path), enabled=True)],
    )

    pipeline.run_index(config)

    assert export_calls == []


def test_video_gps_track_extraction_is_limited_to_gopro_names(tmp_path, monkeypatch):
    from msa_indexer import pipeline

    regular_video = tmp_path / "IMG_0001.mp4"
    gopro_video = tmp_path / "GX010123.MP4"
    renamed_gopro_video = tmp_path / "trimmed_gopro_gps_01.mp4"
    regular_video.write_bytes(b"vid")
    gopro_video.write_bytes(b"vid")
    renamed_gopro_video.write_bytes(b"vid")

    meta_calls = []
    gps_track_calls = []

    monkeypatch.setattr(pipeline, "resolve_for_access", lambda p: p)
    monkeypatch.setattr(
        pipeline,
        "iter_media",
        lambda *_args, **_kwargs: [regular_video, gopro_video, renamed_gopro_video],
    )
    monkeypatch.setattr(
        pipeline,
        "sha256_of_file",
        lambda p: {
            regular_video: "regular-id",
            gopro_video: "gopro-id",
            renamed_gopro_video: "renamed-gopro-id",
        }[p],
    )
    monkeypatch.setattr(pipeline, "SQLiteStore", _FakeSQLiteStore)
    monkeypatch.setattr(pipeline, "ClipEmbedder", _FakeClipEmbedder)
    monkeypatch.setattr(pipeline, "_load_historical_perf", lambda _p: (1.0, 30.0, 5.0))
    monkeypatch.setattr(pipeline, "_emit_indexer_summary", lambda **_payload: None)
    monkeypatch.setattr(pipeline, "get_qdrant_export_version", lambda: None)
    monkeypatch.setattr(pipeline, "record_qdrant_export_version", lambda *_args, **_kwargs: None)

    def fake_get_video_meta(path, *, allow_exiftool_gps=True):
        meta_calls.append((path.name, allow_exiftool_gps))
        return {"duration": 5.0}

    def fake_extract_video_gps_track(path):
        gps_track_calls.append(path.name)
        return [{
            "t_offset_sec": 0.0,
            "gps_lat": 1.0,
            "gps_lon": 2.0,
            "gps_alt": None,
            "gps_datetime_utc": None,
            "gps_fix": 3,
        }]

    monkeypatch.setattr(pipeline, "get_video_meta", fake_get_video_meta)
    monkeypatch.setattr(pipeline, "extract_video_gps_track", fake_extract_video_gps_track)

    config = SimpleNamespace(
        sqlite_path=str(tmp_path / "test.db"),
        faiss_path=tmp_path / "test.faiss",
        face_faiss_path=tmp_path / "test-face.faiss",
        thumb_dir=tmp_path / "thumbs",
        face_thumb_dir=tmp_path / "face-thumbs",
        log_dir=tmp_path / "logs",
        model_name="ViT-L-14",
        pretrained="openai",
        model_version="different-version",
        device="cpu",
        enable_object_detection=False,
        enable_face_recognition=False,
        reprocess_gps=True,
        media_sources=[SimpleNamespace(name="videos", path=str(tmp_path), enabled=True)],
    )

    pipeline.run_index(config)

    assert meta_calls == [
        ("IMG_0001.mp4", True),
        ("GX010123.MP4", True),
        ("trimmed_gopro_gps_01.mp4", True),
    ]
    assert gps_track_calls == ["GX010123.MP4", "trimmed_gopro_gps_01.mp4"]


def test_changed_run_auto_exports_to_qdrant(tmp_path, monkeypatch):
    from msa_indexer import pipeline

    image_path = tmp_path / "image.jpg"
    Image.new("RGB", (8, 8), color="white").save(image_path)

    export_calls = []

    monkeypatch.setattr(pipeline, "resolve_for_access", lambda p: p)
    monkeypatch.setattr(pipeline, "iter_media", lambda *_args, **_kwargs: [image_path])
    monkeypatch.setattr(pipeline, "sha256_of_file", lambda _p: "img-id")
    monkeypatch.setattr(pipeline, "get_exif_basic", lambda _p: {})
    monkeypatch.setattr(pipeline, "write_thumbnail", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(pipeline, "SQLiteStore", _FakeSQLiteStoreNeedsProcessing)
    monkeypatch.setattr(pipeline, "ClipEmbedder", _FakeClipEmbedderWithVectors)
    monkeypatch.setattr(pipeline, "_load_historical_perf", lambda _p: (1.0, 30.0, 5.0))
    monkeypatch.setattr(pipeline, "_emit_indexer_summary", lambda **_payload: None)
    monkeypatch.setattr(pipeline, "get_qdrant_export_version", lambda: None)
    monkeypatch.setattr(pipeline, "record_qdrant_export_version", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        pipeline,
        "_do_qdrant_export",
        lambda *args, **kwargs: export_calls.append((args, kwargs)),
    )

    config = SimpleNamespace(
        sqlite_path=str(tmp_path / "test.db"),
        faiss_path=tmp_path / "test.faiss",
        face_faiss_path=tmp_path / "test-face.faiss",
        thumb_dir=tmp_path / "thumbs",
        face_thumb_dir=tmp_path / "face-thumbs",
        log_dir=tmp_path / "logs",
        model_name="ViT-L-14",
        pretrained="openai",
        model_version="test-v1",
        device="cpu",
        enable_object_detection=False,
        enable_face_recognition=False,
        media_sources=[SimpleNamespace(name="photos", path=str(tmp_path), enabled=True)],
    )

    pipeline.run_index(config)

    assert len(export_calls) == 1


def test_force_export_runs_even_when_no_processing_was_needed(tmp_path, monkeypatch):
    from msa_indexer import pipeline

    image_path = tmp_path / "image.jpg"
    video_path = tmp_path / "video.mp4"
    image_path.write_bytes(b"img")
    video_path.write_bytes(b"vid")

    export_calls = []

    monkeypatch.setattr(pipeline, "resolve_for_access", lambda p: p)
    monkeypatch.setattr(pipeline, "iter_media", lambda *_args, **_kwargs: [image_path, video_path])
    monkeypatch.setattr(pipeline, "sha256_of_file", lambda p: "img-id" if p.suffix.lower() == ".jpg" else "vid-id")
    monkeypatch.setattr(pipeline, "get_video_meta", lambda _p, **_kwargs: {"duration": 5.0})
    monkeypatch.setattr(pipeline, "SQLiteStore", _FakeSQLiteStore)
    monkeypatch.setattr(pipeline, "ClipEmbedder", _FakeClipEmbedder)
    monkeypatch.setattr(pipeline, "_load_historical_perf", lambda _p: (1.0, 30.0, 5.0))
    monkeypatch.setattr(pipeline, "_emit_indexer_summary", lambda **_payload: None)
    monkeypatch.setattr(pipeline, "get_qdrant_export_version", lambda: None)
    monkeypatch.setattr(pipeline, "record_qdrant_export_version", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        pipeline,
        "_do_qdrant_export",
        lambda *args, **kwargs: export_calls.append((args, kwargs)),
    )

    config = SimpleNamespace(
        sqlite_path=str(tmp_path / "test.db"),
        faiss_path=tmp_path / "test.faiss",
        face_faiss_path=str(tmp_path / "test-face.faiss"),
        thumb_dir=tmp_path / "thumbs",
        face_thumb_dir=tmp_path / "face-thumbs",
        log_dir=tmp_path / "logs",
        model_name="ViT-L-14",
        pretrained="openai",
        model_version="test-v1",
        device="cpu",
        enable_object_detection=False,
        enable_face_recognition=False,
        export_to_qdrant=True,
        media_sources=[SimpleNamespace(name="photos", path=str(tmp_path), enabled=True)],
    )

    pipeline.run_index(config)

    assert len(export_calls) == 1


def test_stop_requested_still_exports_after_local_index_changes(tmp_path, monkeypatch):
    from msa_indexer import pipeline

    image_path = tmp_path / "image.jpg"
    Image.new("RGB", (8, 8), color="white").save(image_path)

    export_calls = []

    class _StopAfterFirstFile:
        def __init__(self):
            self.calls = 0

        def is_set(self):
            self.calls += 1
            return self.calls >= 2

    stop_event = _StopAfterFirstFile()

    monkeypatch.setattr(pipeline, "resolve_for_access", lambda p: p)
    monkeypatch.setattr(pipeline, "iter_media", lambda *_args, **_kwargs: [image_path])
    monkeypatch.setattr(pipeline, "sha256_of_file", lambda _p: "img-id")
    monkeypatch.setattr(pipeline, "get_exif_basic", lambda _p: {})
    monkeypatch.setattr(pipeline, "write_thumbnail", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(pipeline, "SQLiteStore", _FakeSQLiteStoreNeedsProcessing)
    monkeypatch.setattr(pipeline, "ClipEmbedder", _FakeClipEmbedderWithVectors)
    monkeypatch.setattr(pipeline, "_load_historical_perf", lambda _p: (1.0, 30.0, 5.0))
    monkeypatch.setattr(pipeline, "_emit_indexer_summary", lambda **_payload: None)
    monkeypatch.setattr(pipeline, "get_qdrant_export_version", lambda: None)
    monkeypatch.setattr(pipeline, "record_qdrant_export_version", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        pipeline,
        "_do_qdrant_export",
        lambda *args, **kwargs: export_calls.append((args, kwargs)),
    )

    config = SimpleNamespace(
        sqlite_path=str(tmp_path / "test.db"),
        faiss_path=tmp_path / "test.faiss",
        face_faiss_path=tmp_path / "test-face.faiss",
        thumb_dir=tmp_path / "thumbs",
        face_thumb_dir=tmp_path / "face-thumbs",
        log_dir=tmp_path / "logs",
        model_name="ViT-L-14",
        pretrained="openai",
        model_version="test-v1",
        device="cpu",
        enable_object_detection=False,
        enable_face_recognition=False,
        media_sources=[SimpleNamespace(name="photos", path=str(tmp_path), enabled=True)],
    )

    pipeline.run_index(config, stop_event=stop_event)

    assert len(export_calls) == 1


def test_stale_qdrant_state_triggers_export_even_without_local_changes(tmp_path, monkeypatch):
    from msa_indexer import pipeline

    image_path = tmp_path / "image.jpg"
    video_path = tmp_path / "video.mp4"
    image_path.write_bytes(b"img")
    video_path.write_bytes(b"vid")

    export_calls = []
    recorded_versions = []

    _FakeSQLiteStore._state = {
        "index_version_seq": 3,
        "index_version_ts": "2026-04-16T00:00:00Z",
    }

    monkeypatch.setattr(pipeline, "resolve_for_access", lambda p: p)
    monkeypatch.setattr(pipeline, "iter_media", lambda *_args, **_kwargs: [image_path, video_path])
    monkeypatch.setattr(pipeline, "sha256_of_file", lambda p: "img-id" if p.suffix.lower() == ".jpg" else "vid-id")
    monkeypatch.setattr(pipeline, "SQLiteStore", _FakeSQLiteStore)
    monkeypatch.setattr(pipeline, "ClipEmbedder", _FakeClipEmbedder)
    monkeypatch.setattr(pipeline, "_load_historical_perf", lambda _p: (1.0, 30.0, 5.0))
    monkeypatch.setattr(pipeline, "_emit_indexer_summary", lambda **_payload: None)
    monkeypatch.setattr(
        pipeline,
        "get_qdrant_export_version",
        lambda: {"index_version_seq": 2, "index_version_ts": "2026-04-15T00:00:00Z"},
    )
    monkeypatch.setattr(
        pipeline,
        "record_qdrant_export_version",
        lambda seq, ts: recorded_versions.append((seq, ts)),
    )
    monkeypatch.setattr(
        pipeline,
        "_do_qdrant_export",
        lambda *args, **kwargs: (export_calls.append((args, kwargs)) or True),
    )

    config = SimpleNamespace(
        sqlite_path=str(tmp_path / "test.db"),
        faiss_path=tmp_path / "test.faiss",
        face_faiss_path=tmp_path / "test-face.faiss",
        thumb_dir=tmp_path / "thumbs",
        face_thumb_dir=tmp_path / "face-thumbs",
        log_dir=tmp_path / "logs",
        model_name="ViT-L-14",
        pretrained="openai",
        model_version="test-v1",
        device="cpu",
        enable_object_detection=False,
        enable_face_recognition=False,
        media_sources=[SimpleNamespace(name="photos", path=str(tmp_path), enabled=True)],
    )

    pipeline.run_index(config)

    assert len(export_calls) == 1
    assert recorded_versions == [(3, "2026-04-16T00:00:00Z")]


# Removed: test_faiss_save_failure_rolls_back_before_sqlite_commit
#
# That test verified the end-of-run FAISS save's rollback contract with
# SQLite. Stage 3 of the SQLite incremental visibility plan eliminated
# the end-of-run FAISS save entirely — embeddings are now written to
# SQLite as BLOBs in image_embedding / keyframe_embedding /
# face_embedding tables per file, and the per-batch commit's rollback
# behavior is exercised by the pipeline's commit-failure try/except path.
