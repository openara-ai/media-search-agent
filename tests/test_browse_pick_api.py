"""
Tests for /browse/pick (native folder picker).

The folder picker is a first-run UX gate — users add their first media source
through it. We previously tried Windows FolderBrowserDialog via PowerShell, but
the dialog opened behind the browser on stricter Windows configurations
(foreground-window restrictions). The AttachThreadInput workaround helped some
machines but not all. The path field next to the Browse button accepts free-text
entry, and the in-app /browse modal works identically on every platform, so the
native picker on Windows added flakiness without delivering reliable value.

Current contract:
- macOS uses osascript 'choose folder' (reliable; no focus-rule games).
- Windows, Linux, WSL2 return 405; the UI falls back to the in-app browser.
- Localhost guard rejects remote hosts on every platform.
"""
import subprocess
import sys
from unittest.mock import MagicMock
import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def client():
    from msa_apps.search_api.app import app
    c = TestClient(app, raise_server_exceptions=False)
    # Default Host header so requests pass the /browse/pick localhost guard.
    # Individual tests can override by passing headers={"host": "..."}.
    c.headers.update({"host": "localhost:8000"})
    return c


# ── Localhost guard (platform-independent) ───────────────────────────────────

class TestLocalhostGuard:
    def test_remote_host_is_rejected(self, client):
        r = client.get("/browse/pick", headers={"host": "10.0.0.5:8000"})
        assert r.status_code == 403
        assert "localhost" in r.json()["detail"].lower()

    def test_localhost_is_allowed(self, client, monkeypatch):
        monkeypatch.setattr(sys, "platform", "linux")  # avoid platform branch
        r = client.get("/browse/pick", headers={"host": "localhost:8000"})
        # Linux falls through to 405 — but it is NOT rejected with 403
        assert r.status_code != 403


# ── Non-mac fallback (Windows, Linux, WSL2 all 405) ──────────────────────────

class TestNonMacFallback:
    """Platforms without a reliable native picker must return 405 so the UI
    opens the in-app browser instead. Windows used to have a PowerShell-driven
    FolderBrowserDialog branch — see git history (commit 959d2652 and the
    AttachThreadInput experiment that followed) — but it was removed because
    it was unreliable on configurations where Windows blocks foreground
    transitions from non-foreground processes."""

    def test_windows_returns_405(self, client, monkeypatch):
        monkeypatch.setattr(sys, "platform", "win32")
        r = client.get("/browse/pick")
        assert r.status_code == 405

    def test_linux_returns_405(self, client, monkeypatch):
        monkeypatch.setattr(sys, "platform", "linux")
        r = client.get("/browse/pick")
        assert r.status_code == 405


# ── macOS branch (mocked subprocess) ─────────────────────────────────────────

class TestMacosPicker:
    def test_macos_uses_osascript(self, client, monkeypatch):
        monkeypatch.setattr(sys, "platform", "darwin")
        captured = {}
        def _capture(args, **kwargs):
            captured["args"] = args
            return MagicMock(stdout="/Users/me/Pictures\n", returncode=0)
        monkeypatch.setattr(subprocess, "run", _capture)
        r = client.get("/browse/pick")
        assert r.status_code == 200
        assert r.json() == {"path": "/Users/me/Pictures", "cancelled": False}
        assert captured["args"][0] == "osascript"

    def test_macos_empty_stdout_means_cancelled(self, client, monkeypatch):
        monkeypatch.setattr(sys, "platform", "darwin")
        monkeypatch.setattr(subprocess, "run", MagicMock(return_value=MagicMock(stdout="", returncode=0)))
        r = client.get("/browse/pick")
        assert r.status_code == 200
        assert r.json() == {"path": None, "cancelled": True}

    def test_macos_subprocess_error_returns_500(self, client, monkeypatch):
        monkeypatch.setattr(sys, "platform", "darwin")
        def _raise(*_a, **_kw):
            raise OSError("osascript not found")
        monkeypatch.setattr(subprocess, "run", _raise)
        r = client.get("/browse/pick")
        assert r.status_code == 500
        assert "Picker failed" in r.json()["detail"]
