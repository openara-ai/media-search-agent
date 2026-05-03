"""Tests that the React SPA is served correctly by FastAPI and that the SPA
catch-all does not shadow API routes or the thumbnail static mounts.

The SPA is served via a catch-all @app.get("/{full_path:path}") registered
after all API routes, so API routes always take priority.

Covers:
- GET /          → 200 text/html (SPA root)
- GET /browse    → 200 application/json (API endpoint — returns directory listing)
- GET /settings  → 200 text/html (React Router path, no API collision)
- GET /health    → 200 application/json (API route wins)
- GET /thumbnails/missing → 404 not text/html (thumbnail mount wins, not SPA)

Note: React routes /faces and /people collide with GET API endpoints and will
return JSON rather than HTML on hard refresh. This is a known Phase 2A
limitation; a /api/ prefix migration is planned before Phase 2E.
"""
import pytest
from fastapi.testclient import TestClient


@pytest.fixture(scope="module")
def client():
    from msa_apps.search_api.app import create_app
    app = create_app(reset_dependencies=True)
    return TestClient(app, raise_server_exceptions=False)


class TestSPAServing:
    def test_root_returns_html(self, client):
        """GET / should return the React index.html."""
        r = client.get("/")
        assert r.status_code == 200
        assert "text/html" in r.headers["content-type"]
        assert '<div id="root">' in r.text

    def test_browse_route_returns_json(self, client):
        """/browse is an API endpoint — returns a JSON directory listing for a real path."""
        import tempfile, os
        with tempfile.TemporaryDirectory() as tmp:
            r = client.get(f"/browse?path={tmp}", headers={"host": "localhost"})
            assert r.status_code == 200
            assert "application/json" in r.headers["content-type"]
            data = r.json()
            assert "current" in data
            assert "entries" in data

    def test_settings_route_returns_html(self, client):
        """/settings has no API route — SPA catch-all should serve index.html."""
        r = client.get("/settings")
        assert r.status_code == 200
        assert "text/html" in r.headers["content-type"]
        assert '<div id="root">' in r.text

    def test_js_bundle_served_as_js(self, client):
        """Actual static assets in dist/assets/ should be served directly."""
        import os
        from pathlib import Path
        dist = Path("src/msa_apps/ui/dist/assets")
        js_files = list(dist.glob("*.js")) if dist.is_dir() else []
        if not js_files:
            pytest.skip("No JS bundle found in dist/assets/ — run npm run build first")
        r = client.get(f"/assets/{js_files[0].name}")
        assert r.status_code == 200
        assert "javascript" in r.headers["content-type"]


class TestAPIPriorityOverSPA:
    def test_health_returns_json_not_html(self, client):
        """GET /health must return JSON — API route must win over the SPA catch-all."""
        r = client.get("/health")
        assert r.status_code == 200
        assert "application/json" in r.headers["content-type"]
        assert "ok" in r.json() or "status" in r.json()

    def test_thumbnail_mount_not_shadowed(self, client):
        """GET /thumbnails/nonexistent should 404 from StaticFiles, not return index.html.
        Verifies the /thumbnails mount is checked before the SPA catch-all."""
        r = client.get("/thumbnails/does_not_exist.jpg")
        assert r.status_code == 404
        assert "text/html" not in r.headers.get("content-type", "")
