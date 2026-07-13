"""Tests for the Windows install path.

M-7/S-3 replaced the fat one-liner (bundle download + venv + torch + tray + Task Scheduler)
with a THIN Tauri bootstrap: fetch setup.exe -> hard-fail SHA-256 verify -> Unblock-File ->
run /S -> launch, plus a -Headless inline-provisioning path. These tests pin the thin contract
and guard against the fat machinery creeping back; the Windows config template (still staged into
the desktop bundle) keeps its YAML-validity coverage.
"""

import re
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parents[1]
WINDOWS_TEMPLATE = REPO_ROOT / "installer" / "windows-native" / "config.windows.yaml.template"
INSTALL_PS1 = REPO_ROOT / "installer" / "windows-native" / "shell" / "install.ps1"


def _install_text() -> str:
    return INSTALL_PS1.read_text(encoding="utf-8")


# ── Windows config template (still staged into the desktop bundle) ────────────


def test_windows_native_installer_config_template_is_valid_yaml():
    """The checked-in Windows template must be valid YAML with required fields."""
    assert WINDOWS_TEMPLATE.exists(), f"Template not found: {WINDOWS_TEMPLATE}"
    config = yaml.safe_load(WINDOWS_TEMPLATE.read_text(encoding="utf-8"))
    assert isinstance(config, dict)
    assert config["media_sources"] == [] or config["media_sources"] is None
    assert config["api"]["port"] == 8000
    assert config["retrieval"]["search_score_trace"] is False


def test_windows_native_installer_example_media_source_is_yaml_safe_when_uncommented():
    """The Windows template example media sources must be valid YAML when uncommented."""
    assert WINDOWS_TEMPLATE.exists(), f"Template not found: {WINDOWS_TEMPLATE}"
    config_text = WINDOWS_TEMPLATE.read_text(encoding="utf-8")
    lines = []
    for line in config_text.splitlines():
        if line == "media_sources: []":
            lines.append("media_sources:")
        elif re.match(r"^#   - ", line) or re.match(r"^#     ", line):
            lines.append(line[1:])
        else:
            lines.append(line)
    config = yaml.safe_load("\n".join(lines))
    sources = config.get("media_sources") or []
    assert isinstance(sources, list) and len(sources) > 0
    for source in sources:
        assert "name" in source
        assert "path" in source


# ── thin bootstrap: download -> verify -> unblock -> /S -> launch ─────────────


def test_thin_installer_downloads_the_tauri_setup_exe():
    """The bootstrap fetches the per-user Tauri NSIS setup.exe, named
    <productName>_<version>_x64-setup.exe, from the release-relative URL."""
    text = _install_text()
    assert 'function Get-SetupExe' in text
    assert '${ProductName}_${bare}_x64-setup.exe' in text
    assert '$baseUrl = "https://github.com/$GithubRepo/releases/download/$tag"' in text.replace("  ", " ") \
        or "releases/download/$tag" in text


def test_thin_installer_hard_fails_on_sha256_mismatch_before_running():
    """The unsigned setup.exe is SHA-256 verified against SHA256SUMS.txt with a HARD fail on
    mismatch, and the verify must run BEFORE the installer is executed."""
    text = _install_text()
    assert "function Test-SetupSha256" in text
    assert "SHA256SUMS.txt" in text
    assert "Get-FileHash $SetupFile -Algorithm SHA256" in text
    assert "Installer SHA256 mismatch" in text
    # Verify happens inside Get-SetupExe, before Invoke-SilentInstall runs the file.
    verify_idx = text.index("Test-SetupSha256 $dest")
    run_idx = text.index("Invoke-SilentInstall $setupExe")
    assert verify_idx < run_idx, "SHA verify must precede running the installer"


def test_thin_installer_hard_fails_on_missing_sha256sums():
    """Unlike the legacy bundle path (which warned-and-continued on a missing SHA256SUMS.txt),
    the thin bootstrap HARD-fails on both HTTP 404 and any other fetch failure - an unverified
    unsigned installer must never be run (spec S-3/S-4: today's warn-and-continue is closed)."""
    text = _install_text()
    sha_idx = text.index("function Test-SetupSha256")
    nxt = text.index("\nfunction ", sha_idx + 1)
    body = text[sha_idx:nxt]
    assert "HTTP 404" in body
    assert "Refusing to install an unverified installer" in body
    # The 404 branch must Write-Fail (hard), not warn-and-continue.
    assert "skipping integrity check" not in body, (
        "the thin bootstrap must NOT warn-and-continue on a missing SHA256SUMS.txt; it hard-fails"
    )
    assert "$_.Exception.Response.StatusCode" in body


def test_thin_installer_unblocks_the_downloaded_setup():
    """Unblock-File clears the Mark-of-the-Web SmartScreen zone so the silent /S run is not blocked."""
    text = _install_text()
    assert "Unblock-File" in text
    unblock_idx = text.index("Unblock-File -Path $dest")
    run_idx = text.index("Invoke-SilentInstall $setupExe")
    assert unblock_idx < run_idx, "Unblock-File must run before the installer is executed"


def test_thin_installer_runs_silently_then_launches_the_app():
    """Runs setup.exe /S (current-user, no UAC), then launches the installed app exe."""
    text = _install_text()
    assert "function Invoke-SilentInstall" in text
    assert 'Start-Process -FilePath $setupExe -ArgumentList "/S" -PassThru -Wait' in text
    # GUI path launches the installed Tauri exe (not a tray).
    assert '$appExe = Join-Path $InstallDir "$ProductName.exe"' in text
    assert "Start-Process -FilePath $appExe" in text


def test_thin_installer_resolves_version_from_flag_env_or_latest():
    text = _install_text()
    assert "function Resolve-MsaVersion" in text
    assert "$env:MSA_VERSION" in text
    assert "releases/latest" in text


def test_thin_installer_local_setup_skips_verification():
    """A local -Setup path is trusted (skips the SHA verify), mirroring the legacy -Bundle escape."""
    text = _install_text()
    setup_idx = text.index('if ($Setup) {')
    # In the local branch, Test-SetupSha256 must not be called.
    branch = text[setup_idx:text.index("$bare      = $tag", setup_idx)]
    assert "Test-SetupSha256" not in branch
    assert "Unblock-File" in branch


# ── headless provisioning ─────────────────────────────────────────────────────


def test_thin_installer_supports_headless_inline_provisioning():
    """-Headless provisions inline (mirrors the supervisor: uv python install -> uv venv with the
    same UV_* pins -> `python -m app.provision`) and installs the msa launcher, instead of launching
    the GUI."""
    text = _install_text()
    assert "[switch] $Headless," in text
    assert "function Invoke-HeadlessProvision" in text
    # Mirrors the supervisor's app-private uv provisioning.
    assert "python install $PythonVersion" in text
    assert "venv $venvDir --python $PythonVersion" in text
    # Same UV_* app-private pins as the vendored supervisor.
    for pin in ("UV_CACHE_DIR", "UV_PYTHON_INSTALL_DIR", "UV_NO_CONFIG", "UV_PYTHON_INSTALL_BIN"):
        assert f"$env:{pin}" in text, f"headless provisioning must pin {pin}"
    # Runs the shim's runnable provision entry.
    assert "-m app.provision" in text
    # Installs the msa CLI launcher targeting the app-private venv + adds it to PATH.
    assert "function Install-MsaLauncher" in text
    assert "msa.cmd" in text
    assert 'SetEnvironmentVariable("Path"' in text
    # Prints the browser-mode start instruction.
    assert "msa api start" in text


def test_launcher_path_append_is_entrywise_not_fragile_substring():
    """Install-MsaLauncher must append $launcherDir to the user PATH by comparing entries
    (split on ';', case-insensitive, trailing '\\' trimmed) and must not write a leading ';'
    when the user PATH is empty (Copilot #1). The old `-notlike "*$launcherDir*"` substring
    membership test could false-match an unrelated entry and skip the install."""
    text = _install_text()
    # The fragile substring membership test is gone.
    assert '-notlike "*$launcherDir*"' not in text
    # Entry-wise comparison over the split PATH, case-insensitive, trailing-backslash trimmed.
    assert "$userPath -split ';'" in text
    assert "-ieq $target" in text
    assert "$launcherDir.TrimEnd('\\')" in text
    # Empty-PATH guard: append without a leading ';'.
    assert 'if ($userPath) { "$userPath;$launcherDir" } else { $launcherDir }' in text


def test_headless_uses_the_app_private_identifier_dir_not_the_legacy_appdir():
    """The venv must be provisioned into the identifier-keyed app-private dir (matching the GUI
    supervisor), NOT the legacy %LOCALAPPDATA%\\MediaSearchAgent AppDir."""
    text = _install_text()
    assert '$AppId         = "ai.openara.mediasearchagent"' in text
    assert '$AppPrivateDir = Join-Path $env:LOCALAPPDATA $AppId' in text
    assert '$venvDir    = Join-Path $AppPrivateDir ".venv"' in text


# ── thinning regression guard: the fat machinery must be gone ─────────────────


def test_thin_installer_dropped_the_fat_bundle_machinery():
    """Regression guard for the S-3 thinning: the bootstrap must no longer do bundle extraction,
    venv/torch provisioning at install time, tray install, or Task Scheduler registration - that is
    now the Tauri app's job (supervisor + first-run shim)."""
    text = _install_text()
    forbidden = [
        "Expand-Archive",            # no bundle unzip
        "Install-Torch",             # no torch install in the bootstrap
        "Install-TrayApp",           # no tray
        "MediaSearchAgentTray",      # no tray exe
        "Register-ScheduledTask",    # no Task Scheduler
        "Install-TaskScheduler",
        "New-ScheduledTaskTrigger",
        "requirements-windows.txt",  # no requirements handling here
    ]
    for token in forbidden:
        assert token not in text, f"thin bootstrap must not contain '{token}' (fat-installer machinery)"


def test_thin_installer_is_ascii_and_lf_only():
    """PowerShell 5.1 reads BOM-less files as the system ANSI codepage; non-ASCII bytes break the
    lexer. install.ps1 must be ASCII + LF (also enforced by scripts/check_ps1_ascii.py)."""
    data = INSTALL_PS1.read_bytes()
    assert b"\r" not in data, "install.ps1 must use LF line endings only"
    bad = [(i, b) for i, b in enumerate(data) if b > 0x7F]
    assert not bad, f"install.ps1 must be ASCII only; first non-ASCII byte at offset {bad[0][0] if bad else '-'}"
