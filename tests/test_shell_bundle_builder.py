import os
import shutil
import subprocess
import sys
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / "installer" / "macos" / "shell" / "build-bundle.sh"
VERSION_HELPER = REPO_ROOT / "scripts" / "lib" / "version.sh"
# Windows is desktop-app only (M-7): start.ps1/stop.ps1 + the Windows shell
# build-bundle.sh were retired in M-7/S-5.5. Only the thin bootstrap install/uninstall
# scripts remain on the Windows shell surface.
WINDOWS_SHELL_SCRIPTS = [
    REPO_ROOT / "installer" / "windows-native" / "shell" / "install.ps1",
    REPO_ROOT / "installer" / "windows-native" / "shell" / "uninstall.ps1",
]


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _bash_executable() -> str:
    # On Windows, `bash` on PATH typically resolves to the wsl.exe shim, which
    # fails on hosts without an installed WSL distro (the GitHub Windows runner).
    # Git for Windows ships a real bash that we can invoke directly.
    if sys.platform == "win32":
        for cand in (
            r"C:\Program Files\Git\bin\bash.exe",
            r"C:\Program Files (x86)\Git\bin\bash.exe",
        ):
            if os.path.exists(cand):
                return cand
    return shutil.which("bash") or "bash"


def _pep440_version(version: str) -> str:
    # Use POSIX path so Git Bash on Windows doesn't interpret backslashes as escapes.
    script = f"source {VERSION_HELPER.as_posix()}; pep440_version {version!r}"
    return subprocess.check_output([_bash_executable(), "-c", script], text=True).strip()


def test_pep440_version_normalizes_ci_label_without_trailing_dot():
    assert _pep440_version("ci-test") == "0.0.0+ci.test"


def test_pep440_version_normalizes_release_labels():
    assert _pep440_version("0.2.0-test") == "0.2.0+test"
    assert _pep440_version("0.2.0-test-more") == "0.2.0+test.more"
    assert _pep440_version("v0.2.0") == "0.2.0"


def _semver_version(version: str) -> str:
    # Use POSIX path so Git Bash on Windows doesn't interpret backslashes as escapes.
    script = f"source {VERSION_HELPER.as_posix()}; semver_version {version!r}"
    return subprocess.check_output([_bash_executable(), "-c", script], text=True).strip()


def test_semver_version_strips_leading_v_but_keeps_base():
    assert _semver_version("v0.4.0") == "0.4.0"
    assert _semver_version("0.4.0") == "0.4.0"


def test_semver_version_preserves_prerelease_suffix():
    # The Tauri app-version stamp must keep the SemVer pre-release so the updater can order
    # rc1 < rc2 < final; a stripped X.Y.Z made -rc1/-rc2 stamp identically and broke self-update.
    assert _semver_version("v0.4.0-rc1") == "0.4.0-rc1"
    assert _semver_version("v0.4.0-rc2") == "0.4.0-rc2"
    assert _semver_version("v1.2.3-beta.1") == "1.2.3-beta.1"


def test_semver_version_preserves_build_metadata():
    assert _semver_version("v0.4.0+abc") == "0.4.0+abc"


def test_semver_version_contrasts_with_pep440_for_prerelease():
    # semver_version keeps the SemVer pre-release ('-rc1'); pep440_version rewrites it to a
    # PEP 440 local segment ('+rc1'). The Tauri stamp needs the former (orderable), so the two
    # helpers must diverge on a pre-release tag.
    tag = "v0.4.0-rc1"
    assert _semver_version(tag) == "0.4.0-rc1"
    assert _pep440_version(tag) == "0.4.0+rc1"
    assert _semver_version(tag) != _pep440_version(tag)


def test_semver_version_falls_back_to_zero_on_non_semver():
    assert _semver_version("ci-test") == "0.0.0"
    assert _semver_version("main") == "0.0.0"


def test_shell_builder_uses_shared_version_normalizer():
    # Only the macOS/Linux shell builder remains (Windows shell bundle retired, S-5.5).
    macos_text = _read(SCRIPT)
    assert 'source "$REPO_ROOT/scripts/lib/version.sh"' in macos_text
    assert 's=$(echo "$v"' not in macos_text


def test_linux_bundle_uses_binary_mediainfo_package_not_source_tarball():
    text = _read(SCRIPT)

    assert "MediaInfo_CLI_${MEDIAINFO_VERSION}_Lambda_x86_64.zip" in text
    assert "GNU_FromSource" not in text
    assert "mediainfo binary not found in Linux zip package" in text

def test_windows_shell_powershell_scripts_are_ascii_only():
    for script in WINDOWS_SHELL_SCRIPTS:
        raw = script.read_bytes()
        try:
            raw.decode("ascii")
        except UnicodeDecodeError as exc:
            raise AssertionError(
                f"{script} must stay ASCII-only for Windows PowerShell 5.1 compatibility"
            ) from exc


# The legacy Windows shell bundle + its build-bundle.sh were retired in M-7/S-5.5
# (Windows is desktop-app only). The start/stop-script inclusion, working-tree mode,
# and ffprobe-exclusion checks that lived here asserted on that deleted builder.
