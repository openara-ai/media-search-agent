"""Tests for the thin macOS Tauri bootstrap in installer/macos/shell/install.sh (M-7/S-3).

macOS is short-circuited to a thin path: download the Tauri updater artifact
(MediaSearchAgent_<v>_aarch64.app.tar.gz) -> hard-fail SHA-256 verify -> extract to
~/Applications -> dequarantine -> open (or provision inline for --headless). The Linux branch
is UNCHANGED (main() falls through to the legacy bundle flow), which these tests also assert.
"""

import shutil
import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
INSTALL_SH = REPO_ROOT / "installer" / "macos" / "shell" / "install.sh"


def _text() -> str:
    return INSTALL_SH.read_text(encoding="utf-8")


def test_install_sh_is_syntactically_valid_bash():
    bash = shutil.which("bash")
    assert bash, "bash is required to lint install.sh"
    r = subprocess.run([bash, "-n", str(INSTALL_SH)], capture_output=True, text=True)
    assert r.returncode == 0, f"bash -n failed:\n{r.stderr}"


def test_macos_short_circuits_to_the_thin_bootstrap():
    """main() must dispatch macOS to macos_thin_install and exit, before the legacy path."""
    text = _text()
    assert "function macos_thin_install" in text or "macos_thin_install() {" in text
    # The short-circuit runs after the arch guards and before the legacy 'Platform paths'.
    sc_idx = text.index('if [[ "$OS" == "macos" ]]; then\n    macos_thin_install')
    paths_idx = text.index("# ── Platform paths (ADR-009)")
    assert sc_idx < paths_idx, "the macOS short-circuit must precede the legacy platform-paths flow"


def test_thin_downloads_the_tauri_updater_artifact():
    text = _text()
    assert 'MediaSearchAgent_${bare}_aarch64.app.tar.gz' in text
    assert "releases/download/${tag}" in text


def test_thin_hard_fails_sha256_verify():
    """The unsigned .app.tar.gz is SHA-256 verified with a HARD fail on mismatch AND on a missing/
    404/not-listed checksum (parity with the Windows thin bootstrap)."""
    text = _text()
    assert "verify_thin_sha256" in text
    assert "SHA256SUMS.txt" in text
    assert "Installer SHA256 mismatch" in text
    assert "Refusing to install an unverified installer" in text
    assert "Refusing to install an artifact with no published checksum" in text


def test_legacy_linux_bundle_verify_hard_fails():
    """M-7/S-4: the legacy Linux bundle path (verify_bundle_sha256) also HARD FAILS on a missing /
    404 / not-listed checksum — the last warn-and-continue in install.sh is closed now that every
    release ships SHA256SUMS.txt over all assets. No `skipping integrity check` return remains."""
    text = _text()
    # The function still exists (Linux legacy flow is retained).
    assert "verify_bundle_sha256()" in text
    # Isolate the function body and assert it no longer warn-and-continues past a bad/absent checksum.
    start = text.index("verify_bundle_sha256()")
    end = text.index("\n# ── Version resolution", start)
    body = text[start:end]
    assert "skipping integrity check" not in body, "legacy bundle verify must not warn-and-continue"
    assert "Refusing to install an unverified bundle" in body
    assert "Refusing to install a bundle with no published checksum" in body
    assert "curl or wget is required to verify the download." in body


def test_thin_extracts_dequarantines_and_opens():
    text = _text()
    assert 'tar -xzf "$archive" -C "$HOME/Applications"' in text
    assert "xattr -dr com.apple.quarantine" in text
    assert 'open "$app_bundle"' in text


def test_thin_supports_headless_inline_provisioning():
    text = _text()
    assert "--headless)           OPT_HEADLESS=1" in text
    assert "macos_headless_provision" in text
    # Mirrors the supervisor: app-private uv provisioning with the same UV_* pins.
    assert 'python install "$PYTHON_VERSION"' in text
    assert 'venv "$venv_dir" --python "$PYTHON_VERSION"' in text
    for pin in ("UV_CACHE_DIR", "UV_PYTHON_INSTALL_DIR", "UV_NO_CONFIG", "UV_PYTHON_INSTALL_BIN"):
        assert pin in text, f"headless provisioning must pin {pin}"
    assert "-m app.provision" in text
    assert "install_msa_launcher_headless" in text
    assert "msa api start" in text


def test_headless_uses_identifier_keyed_app_private_dir():
    text = _text()
    assert 'app_private="$HOME/Library/Application Support/${APP_ID}"' in text
    assert 'APP_ID="ai.openara.mediasearchagent"' in text


def test_linux_legacy_flow_is_preserved():
    """The Linux branch must be unchanged: the legacy bundle flow (install_bundle, install_packages,
    systemd service, and the msa-uninstall launcher delegation) still lives in the file."""
    text = _text()
    for token in ("install_bundle", "install_packages", "install_systemd_service",
                  "setup_python", "install_launcher"):
        assert f"{token}()" in text or f"{token} " in text, f"legacy Linux function {token} must remain"
    # The legacy launcher's `msa uninstall` delegation (asserted by test_shell_uninstaller) stays.
    assert "uninstall.sh" in text and "MSA_ROOT" in text
