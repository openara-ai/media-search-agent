"""
Tests for Phase 2F Item 6 — startup readiness signal.

/health must return {"status": "starting"} until the app is ready,
then {"status": "ready"} once the lifespan startup completes.

Also covers the lifespan model-setup sequencing added in Phase 1C:
- SetupManager.start_if_needed() is called at startup, not at WebSocket connection time.
- /ws/setup is a pure progress subscriber and must not call start_if_needed().
"""
import pytest
from types import SimpleNamespace
from unittest.mock import patch
from fastapi.testclient import TestClient


@pytest.fixture
def health_client():
    """Minimal TestClient that uses the real app without resetting global state."""
    from msa_apps.search_api.app import app
    return TestClient(app, raise_server_exceptions=True)


class TestHealthEndpoint:
    def test_health_returns_json(self, health_client):
        r = health_client.get("/health")
        assert r.status_code == 200
        assert "application/json" in r.headers["content-type"]

    def test_health_has_status_field(self, health_client):
        r = health_client.get("/health")
        body = r.json()
        assert "status" in body
        assert body["status"] in ("starting", "ready")

    def test_health_starting_when_not_ready(self, health_client):
        """Force _ready=False — health must report starting."""
        health_client.app.state._ready = False
        r = health_client.get("/health")
        assert r.json() == {"status": "starting"}

    def test_health_ready_when_flag_set(self, health_client):
        """Force _ready=True — health must report ready."""
        health_client.app.state._ready = True
        r = health_client.get("/health")
        assert r.json() == {"status": "ready"}

    def test_lifespan_sets_ready(self):
        """TestClient used as context manager runs the lifespan; app must be ready inside."""
        from msa_apps.search_api.app import app
        from msa_apps.search_api import setup_models as _sm
        app.state._skip_instance_lock = True
        try:
            with patch.object(_sm.get_manager(), "start_if_needed"):
                with TestClient(app) as client:
                    assert getattr(client.app.state, "_ready", False) is True
                    r = client.get("/health")
                    assert r.json() == {"status": "ready"}
        finally:
            del app.state._skip_instance_lock


# ---------------------------------------------------------------------------
# Lifespan model-setup sequencing
# ---------------------------------------------------------------------------

class TestLifespanModelSetup:
    """_lifespan must kick off SetupManager at startup regardless of client connections."""

    def test_lifespan_calls_start_if_needed_with_models_dir(self, tmp_path):
        """When app.state.S is set, start_if_needed is called with S.models_dir."""
        from msa_apps.search_api.app import app
        from msa_apps.search_api import setup_models as _sm

        fake_cfg = SimpleNamespace(models_dir=tmp_path / "models", log_dir=str(tmp_path))
        app.state.S = fake_cfg
        app.state._skip_instance_lock = True
        try:
            with patch.object(_sm.get_manager(), "start_if_needed") as mock_start:
                with TestClient(app):
                    pass
            mock_start.assert_called_once_with(fake_cfg.models_dir)
        finally:
            del app.state.S
            del app.state._skip_instance_lock

    def test_lifespan_still_ready_when_config_unavailable(self, tmp_path):
        """If config cannot be resolved, startup completes and start_if_needed is not called."""
        from msa_apps.search_api.app import app
        from msa_apps.search_api import setup_models as _sm

        app.state._skip_instance_lock = True
        had_S = hasattr(app.state, "S")
        saved_S = getattr(app.state, "S", None)
        if had_S:
            del app.state.S
        try:
            with patch("msa_settings.load_config", side_effect=RuntimeError("no config.yaml")), \
                 patch.object(_sm.get_manager(), "start_if_needed") as mock_start:
                with TestClient(app) as client:
                    assert getattr(client.app.state, "_ready", False) is True
            mock_start.assert_not_called()
        finally:
            if had_S:
                app.state.S = saved_S
            del app.state._skip_instance_lock

    def test_ws_setup_does_not_call_start_if_needed(self, tmp_path):
        """Connecting to /ws/setup must not trigger start_if_needed — it is a pure
        progress subscriber. The download is owned by the lifespan, not the WebSocket."""
        from msa_apps.search_api.app import app
        from msa_apps.search_api import setup_models as _sm

        fake_cfg = SimpleNamespace(models_dir=tmp_path / "models", log_dir=str(tmp_path))
        app.state.S = fake_cfg
        app.state._skip_instance_lock = True
        try:
            with patch.object(_sm.get_manager(), "start_if_needed") as mock_start:
                with TestClient(app) as client:
                    calls_after_lifespan = mock_start.call_count  # 1: called by lifespan
                    with client.websocket_connect("/ws/setup") as ws:
                        ws.receive_json()  # receive one progress update then disconnect
                # No additional calls beyond the one made at startup
                assert mock_start.call_count == calls_after_lifespan
        finally:
            del app.state.S
            del app.state._skip_instance_lock
