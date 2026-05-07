import re
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parents[1]
WINDOWS_TEMPLATE = REPO_ROOT / "installer" / "windows-native" / "config.windows.yaml.template"
WINDOWS_START = REPO_ROOT / "installer" / "windows-native" / "start.ps1"
TRAY_PROGRAM = REPO_ROOT / "installer" / "windows-native" / "tray" / "Program.cs"


def _start_text() -> str:
    return WINDOWS_START.read_text(encoding="utf-8")


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
    # Simulate a user uncommenting the example sources:
    # replace the empty-list default with a block and strip the comment markers
    lines = []
    for line in config_text.splitlines():
        if line == "media_sources: []":
            lines.append("media_sources:")
        elif re.match(r"^#   - ", line) or re.match(r"^#     ", line):
            lines.append(line[1:])  # strip leading #
        else:
            lines.append(line)
    uncommented = "\n".join(lines)

    config = yaml.safe_load(uncommented)
    sources = config.get("media_sources") or []
    assert isinstance(sources, list), "media_sources must be a list when uncommented"
    assert len(sources) > 0, "at least one example source must be present in the template"
    for source in sources:
        assert "name" in source
        assert "path" in source


def test_shell_installer_falls_back_to_run_key_when_task_scheduler_is_denied():
    text = (
        REPO_ROOT / "installer" / "windows-native" / "shell" / "install.ps1"
    ).read_text(encoding="utf-8")

    assert "function Install-RunKeyAutoStart" in text
    assert "HKCU:\\Software\\Microsoft\\Windows\\CurrentVersion\\Run" in text
    assert "Task Scheduler registration failed" in text
    assert "Falling back to per-user Run registry auto-start" in text
    assert "function Get-AutoStartCommand" in text


def test_shell_installer_creates_start_menu_shortcuts_for_start_and_stop():
    text = (
        REPO_ROOT / "installer" / "windows-native" / "shell" / "install.ps1"
    ).read_text(encoding="utf-8")

    assert "function Install-StartMenuShortcuts" in text
    assert "WScript.Shell" in text
    assert "Microsoft\\Windows\\Start Menu\\Programs\\Media Search Agent" in text
    assert "Media Search Agent.lnk" in text
    assert "Stop Media Search Agent.lnk" in text
    assert "start.ps1" in text
    assert "stop.ps1" in text


def test_shell_installer_copies_bundled_tools_into_launcher_bin():
    text = (
        REPO_ROOT / "installer" / "windows-native" / "shell" / "install.ps1"
    ).read_text(encoding="utf-8")

    assert 'foreach ($tool in @("exiftool.exe")) {' in text
    assert 'Copy-Item (Join-Path $bundleDir "bin\\$tool") (Join-Path $LauncherDir $tool) -Force' in text
    assert 'Write-Ok "Bundled tools installed to $LauncherDir"' in text


def test_windows_tray_cli_opens_in_user_data_folder():
    """The tray's 'Open CLI' menu must open cmd.exe in the user data folder
    (where config.yaml lives), not the install's bin/ directory."""
    text = TRAY_PROGRAM.read_text(encoding="utf-8")

    # OnOpenCmd builds a ProcessStartInfo for cmd.exe ...
    assert "private void OnOpenCmd()" in text
    assert 'new ProcessStartInfo("cmd.exe")' in text
    # ... explicitly set WorkingDirectory to the user data folder ...
    assert "WorkingDirectory = _paths.DataDir" in text
    # ... and create the directory first so the launch cannot fail.
    assert "Directory.CreateDirectory(_paths.DataDir)" in text


def test_windows_native_start_script_shows_startup_window():
    text = _start_text()

    assert "function Show-StartupWindow" in text
    assert "function Update-StartupWindow" in text
    assert 'Text = "Starting Media Search Agent..."' in text
    assert 'Text = "Open Logs"' in text
    assert "This app runs locally on your computer." in text
    assert 'MessageBoxIcon]::Error' in text
