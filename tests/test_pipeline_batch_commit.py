"""
Stage 1 of the SQLite incremental visibility plan: per-batch commits.

Verifies that the indexer pipeline:

- Commits every N files (driven by MSA_INDEXER_COMMIT_BATCH_FILES env var)
- Commits every M seconds (driven by MSA_INDEXER_COMMIT_BATCH_SECONDS) even
  if N hasn't been reached yet
- Emits a BATCH_COMMIT log line per commit
- Emits an INDEXER_SUMMARY phase=commit_stats line at end of run
- Does not trigger per-batch commits when all files are skipped

See internal/docs/storage/SQLITE_INCREMENTAL_VISIBILITY_PLAN.md for the design and
the rationale behind the default thresholds.
"""
from __future__ import annotations

import re
from types import SimpleNamespace

import numpy as np
import pytest
from loguru import logger
from PIL import Image


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class _RecordingFakeSQLite:
    """SQLite stub that counts commit() calls and pretends every file is new."""

    _state = {"index_version_seq": 0, "index_version_ts": None}

    def __init__(self, _path: str, autocommit: bool = True):
        self.autocommit = autocommit
        self.commit_count = 0
        self.rolled_back = False

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
        pass

    def upsert_image_embedding(self, _media_id, _embedding, model: str):
        pass

    def mark_embeddings_done(self, _media_id: str, _model_version: str):
        pass

    def get_total_changes(self) -> int:
        return 1

    def get_index_state(self):
        return dict(self._state)

    def bump_index_version(self):
        self.__class__._state["index_version_seq"] += 1
        self.__class__._state["index_version_ts"] = "2026-04-27T00:00:00Z"
        return {
            "index_version_seq": self.__class__._state["index_version_seq"],
            "index_version_ts": self.__class__._state["index_version_ts"],
        }

    def commit(self):
        self.commit_count += 1

    def rollback(self):
        self.rolled_back = True

    def close(self):
        pass


# Captured by the run_index call. Refreshed per test by the autouse fixture.
_LAST_STORE: list[_RecordingFakeSQLite] = []


def _make_store(*args, **kwargs):
    s = _RecordingFakeSQLite(*args, **kwargs)
    _LAST_STORE.append(s)
    return s


class _FakeClipEmbedderWithVectors:
    dim = 768

    def __init__(self, *_args, **_kwargs):
        pass

    def image_embed(self, images):
        return [np.zeros(self.dim, dtype=np.float32) for _ in images]


class _FakeFaissStore:
    def __init__(self, *_args, **_kwargs):
        pass

    def load(self):
        pass

    def add(self, *_args, **_kwargs):
        pass

    def save(self):
        pass


class _FakeSkipAllSQLite:
    """SQLite stub that pretends every file is already fully indexed."""

    _state = {"index_version_seq": 0, "index_version_ts": None}

    def __init__(self, _path: str, autocommit: bool = True):
        self.autocommit = autocommit
        self.commit_count = 0

    def init_schema(self, _path):
        pass

    def get_processing_status(self, _media_id: str):
        return {
            "gps_processed": True,
            "object_detection_done": True,
            "face_detection_done": True,
            "embeddings_version": "test-v1",
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

    def upsert_media(self, _row):
        pass

    def get_total_changes(self) -> int:
        return 0

    def get_index_state(self):
        return dict(self._state)

    def bump_index_version(self):
        return {"index_version_seq": 0, "index_version_ts": None}

    def commit(self):
        self.commit_count += 1

    def rollback(self):
        pass

    def close(self):
        pass


_LAST_SKIP_STORE: list[_FakeSkipAllSQLite] = []


def _make_skip_store(*args, **kwargs):
    s = _FakeSkipAllSQLite(*args, **kwargs)
    _LAST_SKIP_STORE.append(s)
    return s


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _reset_fake_state():
    _LAST_STORE.clear()
    _LAST_SKIP_STORE.clear()
    _RecordingFakeSQLite._state = {"index_version_seq": 0, "index_version_ts": None}
    _FakeSkipAllSQLite._state = {"index_version_seq": 0, "index_version_ts": None}
    yield


@pytest.fixture
def captured_log_messages():
    """Capture loguru INFO+ messages into a list for the duration of the test."""
    messages: list[str] = []
    handler_id = logger.add(
        lambda msg: messages.append(str(msg)),
        level="INFO",
        format="{level}|{message}",
    )
    yield messages
    logger.remove(handler_id)


def _make_image_files(tmp_path, count: int) -> list:
    paths = []
    for i in range(count):
        p = tmp_path / f"img_{i:03d}.jpg"
        Image.new("RGB", (8, 8), color="white").save(p)
        paths.append(p)
    return paths


def _common_pipeline_patches(monkeypatch, pipeline, image_paths, store_factory):
    monkeypatch.setattr(pipeline, "resolve_for_access", lambda p: p)
    monkeypatch.setattr(
        pipeline, "iter_media", lambda *_args, **_kwargs: list(image_paths)
    )
    monkeypatch.setattr(pipeline, "sha256_of_file", lambda p: f"id-{p.name}")
    monkeypatch.setattr(pipeline, "get_exif_basic", lambda _p: {})
    monkeypatch.setattr(pipeline, "write_thumbnail", lambda *_a, **_k: None)
    monkeypatch.setattr(pipeline, "SQLiteStore", store_factory)
    monkeypatch.setattr(pipeline, "ClipEmbedder", _FakeClipEmbedderWithVectors)
    monkeypatch.setattr(pipeline, "_load_historical_perf", lambda _p: (1.0, 30.0, 5.0))
    monkeypatch.setattr(pipeline, "get_qdrant_export_version", lambda: None)
    monkeypatch.setattr(
        pipeline, "record_qdrant_export_version", lambda *_a, **_k: None
    )
    monkeypatch.setattr(pipeline, "_do_qdrant_export", lambda *_a, **_k: None)


def _make_config(tmp_path):
    return SimpleNamespace(
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
        media_sources=[
            SimpleNamespace(name="photos", path=str(tmp_path), enabled=True)
        ],
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_batch_commits_fire_every_n_files(tmp_path, monkeypatch):
    """With N=2 and 5 processed files, expect exactly 2 BATCH_COMMIT calls
    (after files 2 and 4) plus one final commit during finalize.
    """
    from msa_indexer import pipeline

    monkeypatch.setenv("MSA_INDEXER_COMMIT_BATCH_FILES", "2")
    monkeypatch.setenv("MSA_INDEXER_COMMIT_BATCH_SECONDS", "9999")

    image_paths = _make_image_files(tmp_path, 5)
    _common_pipeline_patches(monkeypatch, pipeline, image_paths, _make_store)

    summaries: list[dict] = []
    monkeypatch.setattr(
        pipeline, "_emit_indexer_summary", lambda **payload: summaries.append(payload)
    )

    pipeline.run_index(_make_config(tmp_path))

    assert _LAST_STORE, "expected SQLiteStore factory to be called"
    store = _LAST_STORE[0]
    # 2 per-batch commits (after files 2 and 4) + 1 final commit at end of run
    assert store.commit_count == 3, (
        f"expected 3 commits (2 batch + 1 final), got {store.commit_count}"
    )

    commit_stats = next(
        (s for s in summaries if s.get("phase") == "commit_stats"), None
    )
    assert commit_stats is not None, "expected commit_stats INDEXER_SUMMARY"
    assert commit_stats["total_commits"] == 2, (
        f"per-batch commit count should be 2, got {commit_stats['total_commits']}"
    )


def test_batch_commit_emits_log_line(tmp_path, monkeypatch, captured_log_messages):
    """Each per-batch commit produces a BATCH_COMMIT log line containing
    files=, commit_ms=, since_last_ms=, batch_serial= fields.
    """
    from msa_indexer import pipeline

    monkeypatch.setenv("MSA_INDEXER_COMMIT_BATCH_FILES", "2")
    monkeypatch.setenv("MSA_INDEXER_COMMIT_BATCH_SECONDS", "9999")

    image_paths = _make_image_files(tmp_path, 4)
    _common_pipeline_patches(monkeypatch, pipeline, image_paths, _make_store)
    monkeypatch.setattr(pipeline, "_emit_indexer_summary", lambda **_p: None)

    pipeline.run_index(_make_config(tmp_path))

    batch_lines = [m for m in captured_log_messages if "BATCH_COMMIT" in m]
    assert len(batch_lines) == 2, (
        f"expected 2 BATCH_COMMIT lines for 4 files at N=2, got "
        f"{len(batch_lines)}: {batch_lines}"
    )
    for line in batch_lines:
        assert re.search(r"files=\d+", line), line
        assert re.search(r"commit_ms=\d+", line), line
        assert re.search(r"since_last_ms=\d+", line), line
        assert re.search(r"batch_serial=\d+", line), line


def test_commit_stats_summary_has_expected_fields(tmp_path, monkeypatch):
    from msa_indexer import pipeline

    monkeypatch.setenv("MSA_INDEXER_COMMIT_BATCH_FILES", "2")
    monkeypatch.setenv("MSA_INDEXER_COMMIT_BATCH_SECONDS", "9999")

    image_paths = _make_image_files(tmp_path, 4)
    _common_pipeline_patches(monkeypatch, pipeline, image_paths, _make_store)

    summaries: list[dict] = []
    monkeypatch.setattr(
        pipeline, "_emit_indexer_summary", lambda **payload: summaries.append(payload)
    )

    pipeline.run_index(_make_config(tmp_path))

    commit_stats = next(
        (s for s in summaries if s.get("phase") == "commit_stats"), None
    )
    assert commit_stats is not None
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
    assert expected_keys.issubset(commit_stats.keys()), (
        f"commit_stats missing keys: {expected_keys - commit_stats.keys()}"
    )
    assert commit_stats["total_commits"] == 2
    assert commit_stats["wall_clock_ms"] >= 0
    assert 0.0 <= commit_stats["commit_pct"] <= 1.0


def test_skipped_run_emits_zero_commit_stats(tmp_path, monkeypatch):
    """When every file is already fully indexed, no per-batch commits
    happen but a commit_stats summary is still emitted with zeros.
    """
    from msa_indexer import pipeline

    monkeypatch.setenv("MSA_INDEXER_COMMIT_BATCH_FILES", "2")
    monkeypatch.setenv("MSA_INDEXER_COMMIT_BATCH_SECONDS", "9999")

    image_paths = _make_image_files(tmp_path, 4)
    _common_pipeline_patches(
        monkeypatch, pipeline, image_paths, _make_skip_store
    )

    summaries: list[dict] = []
    monkeypatch.setattr(
        pipeline, "_emit_indexer_summary", lambda **payload: summaries.append(payload)
    )

    pipeline.run_index(_make_config(tmp_path))

    commit_stats = next(
        (s for s in summaries if s.get("phase") == "commit_stats"), None
    )
    assert commit_stats is not None
    assert commit_stats["total_commits"] == 0
    assert commit_stats["total_commit_ms"] == 0
    assert commit_stats["p50_inter_commit_ms"] == 0


def test_zero_files_emits_zero_commit_stats(tmp_path, monkeypatch):
    """An empty source produces no commits and a commit_stats summary
    populated entirely with zeros (not NaN, not None).
    """
    from msa_indexer import pipeline

    _common_pipeline_patches(monkeypatch, pipeline, [], _make_store)

    summaries: list[dict] = []
    monkeypatch.setattr(
        pipeline, "_emit_indexer_summary", lambda **payload: summaries.append(payload)
    )

    pipeline.run_index(_make_config(tmp_path))

    commit_stats = next(
        (s for s in summaries if s.get("phase") == "commit_stats"), None
    )
    assert commit_stats is not None
    assert commit_stats["total_commits"] == 0
    assert commit_stats["total_commit_ms"] == 0
    assert commit_stats["p50_inter_commit_ms"] == 0
    assert commit_stats["p95_inter_commit_ms"] == 0
    assert commit_stats["max_inter_commit_ms"] == 0
    assert commit_stats["commit_pct"] == 0.0
