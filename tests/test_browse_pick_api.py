"""
Tests for /browse/pick (native folder picker).

Why this matters: the folder picker is a first-run UX gate — users add their
first media source through it. A previous bug had the Windows
FolderBrowserDialog opening behind the browser, so the request looked hung
until the subprocess timed out 120s later. The picker is now invoked with a
hidden TopMost owner form, -STA threading, and CREATE_NO_WINDOW to suppress
the PowerShell console flash. These tests pin that contract:

- Windows branch invokes PowerShell with -STA and CREATE_NO_WINDOW
- Windows script creates a TopMost owner and passes it to ShowDialog
- Cancellation, timeout, and generic errors map to the right HTTP responses
- Localhost guard rejects remote hosts
- Linux returns 405 so the UI falls back to the in-app browser
- Smoke test on real Windows validates that the script's setup steps
  (Add-Type, TopMost form, FolderBrowserDialog construction) actually run
  under PowerShell's -STA mode

The smoke test runs only when sys.platform == "win32"; everywhere else it is
skipped. The mock-based tests run on every platform.
"""
import subprocess
import sys
import pytest
from unittest.mock import MagicMock
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


# ── Linux fallback ───────────────────────────────────────────────────────────

class TestLinuxFallback:
    def test_linux_returns_405(self, client, monkeypatch):
        """Non-mac, non-Windows platforms must return 405 so the UI falls back."""
        monkeypatch.setattr(sys, "platform", "linux")
        r = client.get("/browse/pick")
        assert r.status_code == 405


# ── Windows branch (mocked subprocess) ───────────────────────────────────────

class TestWindowsPicker:
    @pytest.fixture
    def fake_win(self, monkeypatch):
        """Pretend we're running on Windows so the win32 branch is exercised."""
        monkeypatch.setattr(sys, "platform", "win32")

    def test_success_returns_selected_path(self, client, fake_win, monkeypatch):
        mock_run = MagicMock(return_value=MagicMock(stdout="C:\\Users\\me\\Pictures\n", returncode=0))
        monkeypatch.setattr(subprocess, "run", mock_run)
        r = client.get("/browse/pick")
        assert r.status_code == 200
        body = r.json()
        assert body["cancelled"] is False
        assert body["path"] == "C:\\Users\\me\\Pictures"

    def test_empty_stdout_means_cancelled(self, client, fake_win, monkeypatch):
        mock_run = MagicMock(return_value=MagicMock(stdout="", returncode=0))
        monkeypatch.setattr(subprocess, "run", mock_run)
        r = client.get("/browse/pick")
        assert r.status_code == 200
        assert r.json() == {"path": None, "cancelled": True}

    def test_timeout_returns_504_with_helpful_message(self, client, fake_win, monkeypatch):
        """The original bug: dialog hidden behind browser → subprocess timeout.
        Users must see a hint that explains what happened and points at the fallback."""
        def _raise_timeout(*_a, **_kw):
            raise subprocess.TimeoutExpired(cmd="powershell", timeout=300)
        monkeypatch.setattr(subprocess, "run", _raise_timeout)
        r = client.get("/browse/pick")
        assert r.status_code == 504
        detail = r.json()["detail"].lower()
        assert "timed out" in detail
        assert "behind" in detail or "in-app" in detail

    def test_generic_error_returns_500(self, client, fake_win, monkeypatch):
        def _raise(*_a, **_kw):
            raise OSError("powershell not found")
        monkeypatch.setattr(subprocess, "run", _raise)
        r = client.get("/browse/pick")
        assert r.status_code == 500
        assert "Picker failed" in r.json()["detail"]

    def test_subprocess_invoked_with_sta_and_no_window(self, client, fake_win, monkeypatch):
        """Pin the fix: -STA threading and CREATE_NO_WINDOW must be passed.
        Removing either was the precondition for the original z-order bug."""
        captured = {}
        def _capture(args, **kwargs):
            captured["args"] = args
            captured["kwargs"] = kwargs
            return MagicMock(stdout="", returncode=0)
        monkeypatch.setattr(subprocess, "run", _capture)
        r = client.get("/browse/pick")
        assert r.status_code == 200

        argv = captured["args"]
        assert argv[0] == "powershell"
        assert "-STA" in argv
        assert "-NoProfile" in argv
        assert "-NonInteractive" in argv

        kw = captured["kwargs"]
        assert kw.get("creationflags", 0) != 0, "CREATE_NO_WINDOW must be set"
        # CREATE_NO_WINDOW = 0x08000000; allow OR-ed flags but require this bit
        assert kw["creationflags"] & 0x08000000 == 0x08000000

        # Timeout must be generous enough that a slow first-run cold cache
        # (Add-Type loading System.Windows.Forms for the first time) doesn't
        # punish the user before they have a chance to interact with the dialog.
        assert kw.get("timeout", 0) >= 180

    def test_powershell_script_uses_topmost_owner(self, client, fake_win, monkeypatch):
        """Pin the actual fix: the PS script must create a TopMost owner form
        and pass it to ShowDialog. Without this, the dialog opens behind the
        browser and the request hangs until the subprocess timeout fires."""
        captured = {}
        def _capture(args, **kwargs):
            captured["script"] = args[-1]
            return MagicMock(stdout="", returncode=0)
        monkeypatch.setattr(subprocess, "run", _capture)
        client.get("/browse/pick")

        script = captured["script"]
        assert "TopMost=$true" in script
        # ShowDialog must receive the owner form, not be called argument-less
        assert "ShowDialog($owner)" in script
        # And the owner must be activated before the dialog appears
        assert "$owner.Activate()" in script
        # FolderBrowserDialog still the chosen control
        assert "FolderBrowserDialog" in script


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


# ── Windows-only smoke test (real PowerShell) ────────────────────────────────

@pytest.mark.skipif(sys.platform != "win32", reason="real PowerShell smoke test — Windows only")
class TestWindowsRealPowerShell:
    """Run the picker's setup steps under real PowerShell, without ShowDialog.

    This catches breakage that mock-based tests can't see:
      - PowerShell missing or not on PATH
      - -STA flag rejected by the installed PowerShell
      - System.Windows.Forms or System.Drawing not available
      - TopMost owner form construction failing
      - FolderBrowserDialog not constructible

    ShowDialog is intentionally skipped — we cannot drive a modal dialog from
    a non-interactive test runner. The setup steps are where the recent fix
    lives, so they're what we validate end-to-end on Windows.
    """

    def test_picker_setup_runs_under_real_powershell(self):
        # Mirror the real script up to (but not including) ShowDialog.
        ps = (
            "Add-Type -AssemblyName System.Windows.Forms; "
            "Add-Type -AssemblyName System.Drawing; "
            "$owner = New-Object System.Windows.Forms.Form -Property @{TopMost=$true; "
            "ShowInTaskbar=$false; Opacity=0; Size=New-Object System.Drawing.Size(1,1)}; "
            "$owner.Show(); $owner.Activate(); "
            "$d = New-Object System.Windows.Forms.FolderBrowserDialog; "
            "$d.Description = 'Select a media folder'; "
            "$d.UseDescriptionForTitle = $true; "
            "$d.RootFolder = 'MyComputer'; "
            "$owner.Close(); $owner.Dispose(); "
            "Write-Output 'OK'"
        )
        creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
        result = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-STA", "-Command", ps],
            capture_output=True, text=True, timeout=60,
            creationflags=creationflags,
        )
        assert result.returncode == 0, (
            f"PowerShell picker setup failed.\n"
            f"stdout: {result.stdout!r}\n"
            f"stderr: {result.stderr!r}"
        )
        assert "OK" in result.stdout
