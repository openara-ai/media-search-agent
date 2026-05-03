from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
SWIFT_LAUNCHER = REPO_ROOT / "installer" / "macos" / "launcher_app" / "main.swift"
MACOS_BUILD = REPO_ROOT / "installer" / "macos" / "build.sh"
MACOS_SHELL_INSTALL = REPO_ROOT / "installer" / "macos" / "shell" / "install.sh"
START_SH = REPO_ROOT / "scripts" / "start.sh"


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def test_swift_launcher_opens_launch_url():
    text = _read(SWIFT_LAUNCHER)

    assert 'kLaunchURL   = "http://localhost:\\(kAPIPort)/?launch=1"' in text
    assert 'NSWorkspace.shared.open(url)' in text


def test_swift_launcher_waits_for_ready_before_opening_browser():
    """The Swift launcher polls /health and opens the browser only when ready."""
    text = _read(SWIFT_LAUNCHER)

    assert 'waitForReadyThenOpen' in text
    assert 'checkHealth' in text
    assert 'runShellSync(paths.startSH, args: ["--no-browser"])' in text
    assert "appendLauncherLog" in text
    assert "catch {" in text
    assert "p.waitUntilExit()" in text
    assert "exited with status" in text


def test_swift_launcher_is_one_shot_browser_launcher_on_launch():
    """On launch: start or replace the API, then open the browser automatically."""
    text = _read(SWIFT_LAUNCHER)

    assert "DispatchQueue.global(qos: .userInitiated).async" in text
    assert 'runShellSync(paths.startSH, args: ["--no-browser"])' in text
    assert "DispatchQueue.main.async" in text
    assert 'waitForReadyThenOpen(url)' in text


def test_macos_build_uses_swift_launcher():
    """build.sh compiles the Swift launcher directly — no Platypus dependency."""
    text = _read(MACOS_BUILD)

    assert '_build_swift_launcher' in text
    assert 'swiftc -framework AppKit' in text
    assert 'launcher_app/main.swift' in text


def test_macos_build_only_archives_runtime_script_subset():
    text = _read(MACOS_BUILD)

    assert "PACKAGE_PATHS=(" in text
    assert "README.md" in text
    assert "scripts/setup.sh" in text
    assert "scripts/start.sh" in text
    assert "scripts/stop.sh" in text
    assert "scripts/lib/common.sh" in text
    assert 'archive --format=tar HEAD "${PACKAGE_PATHS[@]}"' in text
    assert "src/ scripts/" not in text


def test_macos_build_requires_mediainfo_for_arm64_and_dylib():
    """build.sh is arm64-only; it must thin the universal binary and extract the dylib."""
    text = _read(MACOS_BUILD)

    assert '! -f "$BIN_DIR/mediainfo" || ! -f "$LIB_DIR/libmediainfo.dylib"' in text
    assert 'lipo -thin arm64' in text
    assert '"$LIB_DIR/libmediainfo.dylib"' in text

def test_start_sh_uses_launch_splash_url_when_opening_browser():
    text = _read(START_SH)

    assert '_launch_url()' in text
    assert 'echo "http://localhost:$port/?launch=1"' in text


def test_start_sh_validates_uvicorn_ownership_before_stopping_processes():
    text = _read(START_SH)

    assert "_is_msa_uvicorn_pid()" in text
    assert '[[ "$args" == *"uvicorn"*' in text
    assert '[[ "$args" == *"$VENV"* ]] || return 1' in text
    assert '_pid_listens_on_port "$pid" "$API_PORT"' in text
    assert 'lsof -nP -tiTCP:"$port" -sTCP:LISTEN' in text
    assert "xargs kill -9" not in text


def test_macos_shell_installer_unloads_launch_agent_by_label():
    text = _read(MACOS_SHELL_INSTALL)

    assert 'LAUNCH_AGENT_LABEL="ai.openara.mediasearchagent"' in text
    assert 'launchctl bootout "gui/$uid/$LAUNCH_AGENT_LABEL"' in text
    assert 'launchctl remove "$LAUNCH_AGENT_LABEL"' in text
    assert 'if launchctl load "$plist"; then' in text
    assert 'log_warn "LaunchAgent load failed for $plist"' in text
