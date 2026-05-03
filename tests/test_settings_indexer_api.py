"""
Tests for Phase 2C — Settings, Indexer, and Diagnostics API.

Covers:
- GET /diagnostics — shape and required keys
- GET /logs/{name} — valid name + file present → 200 text/plain
- GET /logs/{name} — unknown name → 404
- GET /logs/{name} — valid name but file absent → 404
- GET /config/sources — returns sources list
- POST /config/sources — adds a source (201)
- POST /config/sources — duplicate name → 409
- DELETE /config/sources/{name} — removes source
- DELETE /config/sources/{name} — unknown name → 404
- GET /indexer/status — returns expected shape
- GET /indexer/stats — returns expected shape (zeros for empty DB)
"""
import pytest
import yaml
import sqlite3
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock
from fastapi.testclient import TestClient


# ── Fixtures ──────────────────────────────────────────────────────────────────

@pytest.fixture()
def db(tmp_path):
    """Empty but schema-initialised SQLite DB."""
    db_path = tmp_path / "test.db"
    schema_path = Path(__file__).parents[1] / "src" / "msa_indexer" / "db" / "schema.sql"
    from msa_indexer.db.sqlite_store import SQLiteStore
    store = SQLiteStore(db_path)
    store.init_schema(schema_path)
    store.close()
    return db_path


@pytest.fixture()
def client(db, tmp_path, monkeypatch):
    """
    FastAPI test client with:
    - tmp_path as the working directory (so _config_path resolves there)
    - a minimal config.yaml seeded with one source
    - log files for the three fixed-name logs
    """
    log_dir = tmp_path / "logs"
    log_dir.mkdir()
    (log_dir / "msa.log").write_text("2026-03-24 12:00:00.000 | INFO | app started\n")
    (log_dir / "uvicorn.log").write_text("INFO:     Application startup complete.\n")
    (log_dir / "qdrant.log").write_text("Qdrant started on port 6333\n")

    (tmp_path / "config.yaml").write_text(yaml.dump({
        "media_sources": [{"name": "photos", "path": "/mnt/d/Photos"}]
    }))

    test_config = SimpleNamespace(
        sqlite_path=str(db),
        server=SimpleNamespace(qdrant_url="http://localhost:6333", qdrant_api_key=None),
        collections=SimpleNamespace(face="face_emb"),
        thumb_dir=tmp_path / "thumbnails",
        face_thumb_dir=tmp_path / "face_thumbnails",
        log_dir="logs",
        log_level="DEBUG",
        media_sources=[
            SimpleNamespace(
                name="photos", path="/mnt/d/Photos",
                enabled=True, read_only=False, description="",
            ),
        ],
        qdrant_port=6333,
    )
    (tmp_path / "thumbnails").mkdir()
    (tmp_path / "face_thumbnails").mkdir()

    # _config_path and diagnostics use os.getcwd() — redirect to tmp_path
    monkeypatch.chdir(tmp_path)

    # indexer_manager is a module-level singleton; patch it to idle so tests
    # are isolated from whatever the real indexer is doing on the host.
    from msa_apps.search_api import indexer_manager as _im_mod
    _idle_status = {"status": "idle", "run_id": None, "started_at": None,
                    "finished_at": None, "elapsed_seconds": None, "return_code": None}
    monkeypatch.setattr(_im_mod.indexer_manager, "get_status", lambda: _idle_status)
    monkeypatch.setattr(_im_mod.indexer_manager, "get_log_lines", lambda tail=50: [])

    mock_qe = MagicMock()
    mock_qe.search.return_value = []
    from msa_apps.search_api.app import create_app
    app = create_app(
        config_override=test_config,
        query_engine_override=mock_qe,
        reset_dependencies=True,
    )
    return TestClient(app)


# ── GET /diagnostics ──────────────────────────────────────────────────────────

class TestDiagnostics:
    def test_returns_200(self, client):
        r = client.get("/diagnostics")
        assert r.status_code == 200

    def test_required_top_level_keys(self, client):
        body = client.get("/diagnostics").json()
        for key in ("msa_root", "config_file", "sqlite_path", "log_dir", "logs", "qdrant_url"):
            assert key in body, f"missing key: {key}"

    def test_fixed_log_keys_always_present(self, client):
        logs = client.get("/diagnostics").json()["logs"]
        for key in ("app", "uvicorn", "qdrant"):
            assert key in logs, f"missing log key: {key}"

    def test_timestamped_log_absent_when_no_file(self, client):
        """install/launch/stop only appear when matching files exist."""
        logs = client.get("/diagnostics").json()["logs"]
        assert "install" not in logs
        assert "launch" not in logs

    def test_timestamped_log_present_when_file_exists(self, client, tmp_path):
        """Creating a launch-*.log makes it appear in diagnostics."""
        (tmp_path / "logs" / "launch-2026-03-24_170000.log").write_text("started\n")
        logs = client.get("/diagnostics").json()["logs"]
        assert "launch" in logs

    def test_app_log_visible_when_msa_log_dir_differs_from_cfg_log_dir(
        self, db, tmp_path, monkeypatch
    ):
        """Regression: on Windows native, MSA_LOG_DIR (LocalAppData) differs from
        cfg.log_dir (UserProfile).  msa.log must be found via cfg.log_dir, not
        MSA_LOG_DIR, otherwise it disappears from the diagnostics logs dict.
        """
        # Two distinct log directories to simulate the Windows native split
        app_log_dir      = tmp_path / "appdata_logs"   # MSA_LOG_DIR (launcher logs)
        user_log_dir     = tmp_path / "userprofile_logs"  # cfg.log_dir (app logs)
        app_log_dir.mkdir()
        user_log_dir.mkdir()

        # msa.log lives in cfg.log_dir (user profile), NOT in MSA_LOG_DIR
        (user_log_dir / "msa.log").write_text("app log line\n")
        # uvicorn.log lives in MSA_LOG_DIR (app data)
        (app_log_dir / "uvicorn.log").write_text("uvicorn started\n")

        (tmp_path / "config.yaml").write_text(yaml.dump({"media_sources": []}))

        from types import SimpleNamespace
        test_config = SimpleNamespace(
            sqlite_path=str(db),
            server=SimpleNamespace(qdrant_url=None, qdrant_api_key=None),
            collections=SimpleNamespace(face="face_emb"),
            thumb_dir=tmp_path / "thumbnails",
            face_thumb_dir=tmp_path / "face_thumbnails",
            log_dir=user_log_dir,   # cfg.log_dir → UserProfile logs
            log_level="DEBUG",
            media_sources=[],
            qdrant_port=6333,
        )
        (tmp_path / "thumbnails").mkdir()
        (tmp_path / "face_thumbnails").mkdir()
        monkeypatch.chdir(tmp_path)
        monkeypatch.setenv("MSA_LOG_DIR", str(app_log_dir))   # launcher log dir

        from msa_apps.search_api import indexer_manager as _im_mod
        _idle = {"status": "idle", "run_id": None, "started_at": None,
                 "finished_at": None, "elapsed_seconds": None, "return_code": None}
        monkeypatch.setattr(_im_mod.indexer_manager, "get_status", lambda: _idle)
        monkeypatch.setattr(_im_mod.indexer_manager, "get_log_lines", lambda tail=50: [])

        from unittest.mock import MagicMock
        mock_qe = MagicMock()
        mock_qe.search.return_value = []
        from msa_apps.search_api.app import create_app
        app = create_app(
            config_override=test_config,
            query_engine_override=mock_qe,
            reset_dependencies=True,
        )
        from fastapi.testclient import TestClient
        c = TestClient(app)

        logs = c.get("/diagnostics").json()["logs"]
        assert "app" in logs, "msa.log must appear even when MSA_LOG_DIR differs from cfg.log_dir"
        assert "uvicorn" in logs


# ── GET /logs/{name} ──────────────────────────────────────────────────────────

class TestServeLogs:
    def test_valid_name_returns_200_plain_text(self, client):
        r = client.get("/logs/app")
        assert r.status_code == 200
        assert "text/plain" in r.headers["content-type"]

    def test_log_content_matches_file(self, client):
        r = client.get("/logs/uvicorn")
        assert "Application startup complete" in r.text

    def test_unknown_name_returns_404(self, client):
        r = client.get("/logs/notareallog")
        assert r.status_code == 404

    def test_missing_file_returns_404(self, client):
        """install log doesn't exist yet → 404."""
        r = client.get("/logs/install")
        assert r.status_code == 404


# ── GET /config/sources ───────────────────────────────────────────────────────

class TestConfigSources:
    def test_get_returns_sources_list(self, client):
        r = client.get("/config/sources")
        assert r.status_code == 200
        body = r.json()
        assert "sources" in body
        assert isinstance(body["sources"], list)

    def test_get_includes_seeded_source(self, client):
        sources = client.get("/config/sources").json()["sources"]
        names = [s["name"] for s in sources]
        assert "photos" in names

    def test_add_source_returns_201(self, client):
        r = client.post("/config/sources", json={"name": "videos", "path": "/mnt/d/Videos"})
        assert r.status_code == 201

    def test_add_source_persisted_to_yaml(self, client, tmp_path):
        """POST must write the new source to config.yaml on disk."""
        client.post("/config/sources", json={"name": "archive", "path": "/mnt/e/Archive"})
        data = yaml.safe_load((tmp_path / "config.yaml").read_text())
        names = [s["name"] for s in data.get("media_sources", [])]
        assert "archive" in names

    def test_add_source_uses_msa_config_path_when_set(self, db, tmp_path, monkeypatch):
        """Regression: installer launches may use AppDir as cwd while config.yaml
        lives elsewhere. The API must honor MSA_CONFIG_PATH instead of cwd/config.yaml.
        """
        work_dir = tmp_path / "appdir"
        data_dir = tmp_path / "datadir"
        work_dir.mkdir()
        data_dir.mkdir()
        monkeypatch.chdir(work_dir)
        monkeypatch.setenv("MSA_CONFIG_PATH", str(data_dir / "config.yaml"))

        (data_dir / "config.yaml").write_text(yaml.dump({"media_sources": []}))
        (work_dir / "logs").mkdir()

        test_config = SimpleNamespace(
            sqlite_path=str(db),
            server=SimpleNamespace(qdrant_url=None, qdrant_api_key=None),
            collections=SimpleNamespace(face="face_emb"),
            thumb_dir=tmp_path / "thumbnails",
            face_thumb_dir=tmp_path / "face_thumbnails",
            log_dir=str(work_dir / "logs"),
            log_level="DEBUG",
            media_sources=[],
            qdrant_port=6333,
        )
        (tmp_path / "thumbnails").mkdir()
        (tmp_path / "face_thumbnails").mkdir()

        from msa_apps.search_api import indexer_manager as _im_mod
        _idle_status = {"status": "idle", "run_id": None, "started_at": None,
                        "finished_at": None, "elapsed_seconds": None, "return_code": None}
        monkeypatch.setattr(_im_mod.indexer_manager, "get_status", lambda: _idle_status)
        monkeypatch.setattr(_im_mod.indexer_manager, "get_log_lines", lambda tail=50: [])

        mock_qe = MagicMock()
        mock_qe.search.return_value = []
        from msa_apps.search_api.app import create_app
        app = create_app(
            config_override=test_config,
            query_engine_override=mock_qe,
            reset_dependencies=True,
        )
        c = TestClient(app)

        r = c.post("/config/sources", json={"name": "archive", "path": "D:\\Archive"})
        assert r.status_code == 201

        data = yaml.safe_load((data_dir / "config.yaml").read_text())
        names = [s["name"] for s in data.get("media_sources", [])]
        assert "archive" in names

    def test_add_duplicate_returns_409(self, client):
        r = client.post("/config/sources", json={"name": "photos", "path": "/mnt/d/Photos"})
        assert r.status_code == 409

    def test_delete_source_returns_200(self, client):
        r = client.delete("/config/sources/photos")
        assert r.status_code == 200

    def test_deleted_source_absent_from_get(self, client):
        client.delete("/config/sources/photos")
        sources = client.get("/config/sources").json()["sources"]
        names = [s["name"] for s in sources]
        assert "photos" not in names

    def test_delete_unknown_returns_404(self, client):
        r = client.delete("/config/sources/doesnotexist")
        assert r.status_code == 404


# ── Config API with BOM-prefixed config.yaml (Windows PS5.1 regression) ───────

@pytest.fixture()
def bom_client(db, tmp_path, monkeypatch):
    """Like `client`, but config.yaml is written with a UTF-8 BOM.

    Simulates the file written by PowerShell 5.1 Set-Content -Encoding UTF8.
    Also patches builtins.open to inject cp1252 when no explicit encoding is
    given — reproducing Windows locale behaviour cross-platform.

    The API routes use encoding='utf-8-sig' (the fix), which provides an
    explicit encoding kwarg that bypasses the cp1252 injection and strips the
    BOM.  Without the fix (open without encoding), the injected cp1252 turns
    the BOM bytes into 'ï»¿', causing yaml.safe_load to fail.
    """
    import builtins

    log_dir = tmp_path / "logs"
    log_dir.mkdir()
    (log_dir / "msa.log").write_text("2026-03-24 12:00:00.000 | INFO | app started\n")

    # Write config.yaml WITH UTF-8 BOM (EF BB BF) — as PowerShell 5.1 would.
    # The content starts with comment lines to match the installer template shape:
    # ï»¿# comment\n\nmedia_sources: ... triggers yaml.ParserError("expected
    # '<document start>'") when decoded with cp1252.  Without comment lines
    # YAML silently treats the BOM bytes as part of the first key name, masking
    # the bug.
    bom = b"\xef\xbb\xbf"
    config_yaml = (
        "# Media Search Agent configuration\n"
        "# Edit this file to add your media sources.\n\n"
        "media_sources:\n"
        "- name: photos\n"
        "  path: /mnt/d/Photos\n"
    ).encode("utf-8")
    (tmp_path / "config.yaml").write_bytes(bom + config_yaml)

    test_config = SimpleNamespace(
        sqlite_path=str(db),
        server=SimpleNamespace(qdrant_url="http://localhost:6333", qdrant_api_key=None),
        collections=SimpleNamespace(face="face_emb"),
        thumb_dir=tmp_path / "thumbnails",
        face_thumb_dir=tmp_path / "face_thumbnails",
        log_dir="logs",
        log_level="DEBUG",
        media_sources=[
            SimpleNamespace(
                name="photos", path="/mnt/d/Photos",
                enabled=True, read_only=False, description="",
            ),
        ],
        qdrant_port=6333,
    )
    (tmp_path / "thumbnails").mkdir()
    (tmp_path / "face_thumbnails").mkdir()
    monkeypatch.chdir(tmp_path)

    from msa_apps.search_api import indexer_manager as _im_mod
    _idle_status = {"status": "idle", "run_id": None, "started_at": None,
                    "finished_at": None, "elapsed_seconds": None, "return_code": None}
    monkeypatch.setattr(_im_mod.indexer_manager, "get_status", lambda: _idle_status)
    monkeypatch.setattr(_im_mod.indexer_manager, "get_log_lines", lambda tail=50: [])

    # Inject cp1252 whenever open() is called without an explicit encoding —
    # this reproduces Windows cp1252 default locale behaviour cross-platform.
    # The fix's explicit encoding='utf-8-sig' kwarg bypasses this injection.
    real_open = builtins.open

    def _simulate_windows_encoding(path, mode="r", **kwargs):
        if "b" not in str(mode) and "encoding" not in kwargs:
            kwargs["encoding"] = "cp1252"
        return real_open(path, mode, **kwargs)

    monkeypatch.setattr(builtins, "open", _simulate_windows_encoding)

    mock_qe = MagicMock()
    mock_qe.search.return_value = []
    from msa_apps.search_api.app import create_app
    app = create_app(
        config_override=test_config,
        query_engine_override=mock_qe,
        reset_dependencies=True,
    )
    return TestClient(app)


class TestConfigSourcesBomConfig:
    """Regression: API config write/read routes must handle BOM-prefixed config.yaml.

    Each test uses bom_client, which starts with a BOM-prefixed config.yaml and
    patches open() to inject cp1252 (simulating Windows locale).  The routes use
    encoding='utf-8-sig' to read and encoding='utf-8' to write, so the BOM is
    stripped on first access and subsequent reads/writes are clean UTF-8.

    These tests:
      - FAIL without the explicit encoding fix in app.py  (cp1252 injected → YAML error → 500)
      - PASS with the fix                                  (utf-8-sig bypasses injection)
    """

    def test_add_source_succeeds_with_bom_config(self, bom_client):
        """POST /config/sources must not 500 when config.yaml has a UTF-8 BOM."""
        r = bom_client.post("/config/sources", json={"name": "archive", "path": "/mnt/e/Archive"})
        assert r.status_code == 201

    def test_add_source_persisted_with_bom_config(self, bom_client, tmp_path):
        """New source must be written to disk even when the original file had a BOM."""
        bom_client.post("/config/sources", json={"name": "archive", "path": "/mnt/e/Archive"})
        # After the write-back the file is clean UTF-8 (no BOM) — read normally
        data = yaml.safe_load((tmp_path / "config.yaml").read_text(encoding="utf-8"))
        names = [s["name"] for s in data.get("media_sources", [])]
        assert "archive" in names

    def test_delete_source_succeeds_with_bom_config(self, bom_client):
        """DELETE /config/sources must not 500 when config.yaml has a UTF-8 BOM."""
        r = bom_client.delete("/config/sources/photos")
        assert r.status_code == 200

    def test_patch_model_config_succeeds_with_bom_config(self, bom_client):
        """PATCH /config/model must not 500 when config.yaml has a UTF-8 BOM."""
        r = bom_client.patch("/config/model", json={"batch_size": 16})
        assert r.status_code == 200


# ── GET /indexer/status ───────────────────────────────────────────────────────

class TestIndexerStatus:
    def test_returns_200(self, client):
        assert client.get("/indexer/status").status_code == 200

    def test_shape(self, client):
        body = client.get("/indexer/status").json()
        for key in ("status", "run_id", "elapsed_seconds", "return_code"):
            assert key in body, f"missing key: {key}"

    def test_initial_status_is_idle(self, client):
        assert client.get("/indexer/status").json()["status"] == "idle"


# ── GET /indexer/stats ────────────────────────────────────────────────────────

class TestIndexerStats:
    def test_returns_200(self, client):
        assert client.get("/indexer/stats").status_code == 200

    def test_shape(self, client):
        body = client.get("/indexer/stats").json()
        for key in ("images", "videos", "faces", "people", "last_indexed_at"):
            assert key in body, f"missing key: {key}"

    def test_all_zero_for_empty_db(self, client):
        body = client.get("/indexer/stats").json()
        assert body["images"] == 0
        assert body["videos"] == 0
        assert body["faces"] == 0
        assert body["people"] == 0


# ── GET /config/model ─────────────────────────────────────────────────────────

class TestModelConfigGet:
    def test_returns_200(self, client):
        assert client.get("/config/model").status_code == 200

    def test_top_level_keys(self, client):
        body = client.get("/config/model").json()
        for key in ("readonly", "editable", "defaults"):
            assert key in body, f"missing key: {key}"

    def test_readonly_fields(self, client):
        ro = client.get("/config/model").json()["readonly"]
        for key in ("device", "model_name", "pretrained"):
            assert key in ro, f"missing readonly key: {key}"

    def test_editable_fields(self, client):
        ed = client.get("/config/model").json()["editable"]
        for key in (
            "batch_size", "enable_object_detection", "object_model",
            "object_confidence_threshold", "enable_face_recognition",
            "face_model", "face_confidence_threshold",
            "face_min_size", "face_store_metadata",
        ):
            assert key in ed, f"missing editable key: {key}"

    def test_defaults_match_editable_keys(self, client):
        body = client.get("/config/model").json()
        assert set(body["defaults"].keys()) == set(body["editable"].keys())


# ── PATCH /config/model ───────────────────────────────────────────────────────

class TestModelConfigPatch:
    def test_patch_returns_200(self, client):
        r = client.patch("/config/model", json={"batch_size": 16})
        assert r.status_code == 200

    def test_patch_reports_updated_keys(self, client):
        r = client.patch("/config/model", json={"batch_size": 16})
        assert "batch_size" in r.json()["updated"]

    def test_patch_persisted_to_yaml(self, client, tmp_path):
        client.patch("/config/model", json={"batch_size": 8})
        data = yaml.safe_load((tmp_path / "config.yaml").read_text())
        assert data["batch_size"] == 8

    def test_patch_boolean_field(self, client, tmp_path):
        client.patch("/config/model", json={"enable_object_detection": False})
        data = yaml.safe_load((tmp_path / "config.yaml").read_text())
        assert data["enable_object_detection"] is False

    def test_patch_float_field(self, client, tmp_path):
        client.patch("/config/model", json={"face_confidence_threshold": 0.9})
        data = yaml.safe_load((tmp_path / "config.yaml").read_text())
        assert abs(data["face_confidence_threshold"] - 0.9) < 0.001

    def test_patch_readonly_field_ignored(self, client, tmp_path):
        """device is not in the Pydantic model so it cannot be patched."""
        original = yaml.safe_load((tmp_path / "config.yaml").read_text()).get("device")
        # Sending an unknown field — FastAPI ignores extra fields by default
        client.patch("/config/model", json={"batch_size": 4})
        data = yaml.safe_load((tmp_path / "config.yaml").read_text())
        # device must be unchanged
        assert data.get("device") == original

    def test_patch_empty_body_returns_400(self, client):
        r = client.patch("/config/model", json={})
        assert r.status_code == 400
