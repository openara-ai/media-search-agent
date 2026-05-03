"""
Regression tests for PR #94 review feedback:

1. ``run_export_dry_run`` must NOT mutate the schema (was calling
   init_schema, which runs the legacy face-table drop/recreate
   migration — a data-loss risk in a documented "no modifications" mode).
2. Face-search / face-suggestions / batch-suggestions endpoints must
   NOT silently create an empty SQLite file when the configured path
   is missing.
3. Per-batch commit env vars must fall back to defaults on
   empty-string / non-numeric input, not crash the indexer at startup.
"""
from __future__ import annotations

import os
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest
from fastapi.testclient import TestClient


SCHEMA_PATH = Path(__file__).resolve().parents[1] / "src" / "msa_indexer" / "db" / "schema.sql"


# ---------------------------------------------------------------------------
# Fix #1: run_export_dry_run must not run migrations on a pre-Stage-3 DB
# ---------------------------------------------------------------------------


def test_run_export_dry_run_does_not_mutate_pre_stage3_schema(tmp_path, caplog):
    """A SQLite DB that lacks the new embedding tables must come back
    untouched from a dry-run. The reviewer's concern is that
    ``init_schema`` is *not* read-only — it includes the legacy
    face-table drop/recreate migration, which would silently destroy
    user data on a pre-Stage-3 database.
    """
    import sqlite3

    db_path = tmp_path / "pre_stage3.sqlite"
    # Build a minimal pre-Stage-3 schema: just media + face + person,
    # with NO image_embedding / keyframe_embedding / face_embedding tables.
    conn = sqlite3.connect(str(db_path))
    try:
        conn.executescript("""
            CREATE TABLE media (media_id TEXT PRIMARY KEY, deleted INTEGER DEFAULT 0, mime TEXT);
            CREATE TABLE person (person_id TEXT PRIMARY KEY, name TEXT);
            CREATE TABLE face (face_id TEXT PRIMARY KEY, media_id TEXT, person_id TEXT);
            CREATE TABLE video_keyframes (id INTEGER PRIMARY KEY, video_id TEXT,
                                          shot_index INTEGER, kf_index INTEGER);
        """)
        conn.commit()
    finally:
        conn.close()

    # Snapshot the schema so we can assert nothing changed
    def _list_tables() -> set[str]:
        c = sqlite3.connect(str(db_path))
        try:
            return {
                r[0] for r in c.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                ).fetchall()
            }
        finally:
            c.close()

    tables_before = _list_tables()

    from msa_indexer.pipeline import run_export_dry_run

    config = SimpleNamespace(sqlite_path=str(db_path))
    run_export_dry_run(config)  # must NOT raise; must NOT mutate schema

    tables_after = _list_tables()
    assert tables_before == tables_after, (
        f"run_export_dry_run mutated the SQLite schema. "
        f"Before: {tables_before}. After: {tables_after}. "
        f"This is the P1 bug from PR #94 review — dry-run must not run "
        f"migrations because init_schema can drop the legacy face table."
    )


# ---------------------------------------------------------------------------
# Fix #2: face endpoints must not silently create empty SQLite files
# ---------------------------------------------------------------------------


class _StubQdrant:
    def search_batch(self, *_a, **_k):
        return [[]]

    def search(self, **_k):
        return []


@pytest.fixture
def api_client_missing_db(monkeypatch, tmp_path):
    """A FastAPI app pointed at a SQLite path that does NOT exist on disk."""
    missing = tmp_path / "does_not_exist" / "media.sqlite"
    assert not missing.exists()
    test_config = SimpleNamespace(
        sqlite_path=str(missing),
        qdrant_path=str(tmp_path / "qdrant"),
        server=SimpleNamespace(),
        collections=SimpleNamespace(face="face_emb"),
    )
    from msa_apps.search_api.app import create_app
    app = create_app(config_override=test_config, reset_dependencies=True)
    monkeypatch.setattr("msa_query.storage.qdrant_client._shared", _StubQdrant())
    monkeypatch.setattr("msa_query.storage.qdrant_client._blocked", False)
    return TestClient(app), missing


def test_face_search_does_not_create_empty_db(api_client_missing_db):
    client, missing = api_client_missing_db
    resp = client.post("/faces/search", json={"face_id": "any", "top_k": 5})
    assert resp.status_code == 500
    assert not missing.exists(), "face-search must not silently create the SQLite file"


def test_face_suggestions_does_not_create_empty_db(api_client_missing_db):
    client, missing = api_client_missing_db
    resp = client.get("/faces/any/suggestions?top_k=5")
    assert resp.status_code == 500
    assert not missing.exists(), "face-suggestions must not silently create the SQLite file"


def test_batch_face_suggestions_does_not_create_empty_db(api_client_missing_db):
    client, missing = api_client_missing_db
    resp = client.post(
        "/faces/suggestions/batch", json={"face_ids": ["any"], "top_k": 3}
    )
    assert resp.status_code == 500
    assert not missing.exists(), (
        "batch face suggestions must not silently create the SQLite file"
    )


# ---------------------------------------------------------------------------
# Fix #4: env var parsing must not crash on bad input
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Codex round-2 P1: run_export must not run the destructive face-table
# migration. Same root cause as the dry-run case but on the actual export
# entry point — was using init_schema, now uses init_schema_no_migrations.
# ---------------------------------------------------------------------------


def test_run_export_does_not_drop_legacy_face_table(tmp_path, monkeypatch):
    """Build a legacy DB shape (face table without person_id column) and
    run ``run_export``. The destructive ``DROP TABLE face`` migration
    must NOT fire — the export-only path should never destroy face data.
    """
    import sqlite3

    db_path = tmp_path / "legacy.sqlite"
    conn = sqlite3.connect(str(db_path))
    try:
        # Realistic pre-Stage-1 shape: media has the additive migration
        # columns (source_name, rel_path, etc. were ALTER-added over time)
        # but face is the *old* shape without person_id, which is exactly
        # the schema the destructive migration would target.
        conn.executescript("""
            CREATE TABLE media (
                media_id TEXT PRIMARY KEY,
                deleted INTEGER DEFAULT 0,
                mime TEXT,
                source_name TEXT,
                rel_path TEXT,
                gps_processed INTEGER DEFAULT 0,
                face_detection_done INTEGER DEFAULT 0,
                object_detection_done INTEGER DEFAULT 0,
                embeddings_version TEXT
            );
            CREATE TABLE face (face_id TEXT PRIMARY KEY, media_id TEXT, x REAL, y REAL, w REAL, h REAL);
            INSERT INTO media (media_id, deleted, mime) VALUES ('m1', 0, 'image/jpeg');
            INSERT INTO face VALUES ('m1:f0', 'm1', 0.1, 0.1, 0.3, 0.3);
        """)
        conn.commit()
    finally:
        conn.close()

    from msa_indexer import pipeline

    # Stub Qdrant + record_qdrant_export_version so we can run end-to-end
    # without touching real services.
    monkeypatch.setattr(pipeline, "_do_qdrant_export", lambda *_a, **_k: False)
    monkeypatch.setattr(pipeline, "record_qdrant_export_version", lambda *_a, **_k: None)
    # The export summary block at the end of run_export uses qdrant_client +
    # load_config; stub them so the test stays hermetic.
    class _StubQdrant:
        def get_collection(self, *_a, **_k):
            raise Exception("not found")
    import qdrant_client
    monkeypatch.setattr(qdrant_client, "QdrantClient", lambda *_a, **_k: _StubQdrant())

    config = SimpleNamespace(sqlite_path=str(db_path))
    pipeline.run_export(config)  # must NOT raise; must NOT drop face table

    # Verify the legacy face row survived — the destructive migration
    # would have wiped it.
    conn = sqlite3.connect(str(db_path))
    try:
        face_row = conn.execute("SELECT face_id, media_id FROM face").fetchone()
        # Verify run_export did NOT silently create the new embedding
        # tables either; it should fail-fast on legacy face schema and
        # tell the user to run msa index run first.
        embedding_tables = {
            r[0] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' "
                "AND name IN ('image_embedding','keyframe_embedding','face_embedding')"
            ).fetchall()
        }
    finally:
        conn.close()
    assert face_row == ("m1:f0", "m1"), (
        "run_export destroyed the legacy face row — the destructive "
        "init_schema migration must NOT fire from export-only mode."
    )
    assert embedding_tables == set(), (
        "run_export silently created embedding tables on a legacy DB. "
        "It should fail-fast and direct the user to 'msa index run' "
        "instead."
    )


# ---------------------------------------------------------------------------
# Codex round-2 P1: porter --dry-run must not run init_schema (which
# would mutate the schema, contradicting the dry-run contract).
# ---------------------------------------------------------------------------


_PORTER_SCRIPT = (
    Path(__file__).resolve().parents[1] / "internal" / "scripts" / "port_faiss_to_sqlite.py"
)


@pytest.mark.skipif(
    not _PORTER_SCRIPT.exists(),
    reason="port_faiss_to_sqlite.py is in internal/scripts/ (private repo only)",
)
def test_porter_dry_run_does_not_create_embedding_tables(tmp_path, monkeypatch):
    """A porter dry-run on a DB without the embedding tables should NOT
    create them. Calling init_schema would, even when no embeddings are
    inserted — that violates the documented "no writes" contract.
    """
    import sqlite3
    import sys
    SCRIPTS_DIR = _PORTER_SCRIPT.parent
    if str(SCRIPTS_DIR) not in sys.path:
        sys.path.insert(0, str(SCRIPTS_DIR))

    db_path = tmp_path / "no_emb_tables.sqlite"
    # Minimal pre-Stage-3 schema: media + face, no embedding tables.
    conn = sqlite3.connect(str(db_path))
    try:
        conn.executescript("""
            CREATE TABLE media (media_id TEXT PRIMARY KEY, deleted INTEGER DEFAULT 0, mime TEXT);
            CREATE TABLE face (face_id TEXT PRIMARY KEY, media_id TEXT, person_id TEXT);
        """)
        conn.commit()
    finally:
        conn.close()

    def _embedding_tables_present(p: Path) -> set[str]:
        c = sqlite3.connect(str(p))
        try:
            return {
                r[0] for r in c.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' "
                    "AND name IN ('image_embedding','keyframe_embedding','face_embedding')"
                ).fetchall()
            }
        finally:
            c.close()

    assert _embedding_tables_present(db_path) == set()

    from port_faiss_to_sqlite import main as porter_main
    rc = porter_main([
        "--sqlite-path", str(db_path),
        "--faiss-path", str(tmp_path / "image_vec.faiss"),  # missing
        "--face-faiss-path", str(tmp_path / "face_vec.faiss"),  # missing
        "--dry-run",
    ])
    assert rc == 0
    assert _embedding_tables_present(db_path) == set(), (
        "Porter dry-run created embedding tables — that violates the "
        "documented dry-run contract. Should probe sqlite_master and "
        "exit cleanly instead."
    )


@pytest.mark.parametrize(
    "files_val,seconds_val",
    [
        ("", ""),                  # empty strings (the reviewer's example)
        ("not-an-int", "abc"),     # non-numeric
        ("   ", "  "),             # whitespace only
    ],
)
def test_indexer_env_var_parsing_falls_back_on_bad_input(
    tmp_path, monkeypatch, files_val, seconds_val
):
    """The pipeline used to parse MSA_INDEXER_COMMIT_BATCH_FILES /
    MSA_INDEXER_COMMIT_BATCH_SECONDS via raw int() / float() and crash
    at startup on empty / non-numeric values. After the fix, it should
    log a warning and fall back to defaults (200 / 15.0).
    """
    monkeypatch.setenv("MSA_INDEXER_COMMIT_BATCH_FILES", files_val)
    monkeypatch.setenv("MSA_INDEXER_COMMIT_BATCH_SECONDS", seconds_val)

    # Stub everything heavy so run_index can complete with no real work
    from msa_indexer import pipeline
    from test_indexer_summary import (  # sibling test module
        _FakeSQLiteStore,
        _FakeClipEmbedder,
    )
    monkeypatch.setattr(pipeline, "resolve_for_access", lambda p: p)
    monkeypatch.setattr(pipeline, "iter_media", lambda *_a, **_k: [])
    monkeypatch.setattr(pipeline, "SQLiteStore", _FakeSQLiteStore)
    monkeypatch.setattr(pipeline, "ClipEmbedder", _FakeClipEmbedder)
    monkeypatch.setattr(pipeline, "_load_historical_perf", lambda _p: (1.0, 30.0, 5.0))
    monkeypatch.setattr(pipeline, "_emit_indexer_summary", lambda **_p: None)
    monkeypatch.setattr(pipeline, "get_qdrant_export_version", lambda: None)
    monkeypatch.setattr(pipeline, "record_qdrant_export_version", lambda *_a, **_k: None)

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

    # Should NOT raise even with garbage env values.
    pipeline.run_index(config)
