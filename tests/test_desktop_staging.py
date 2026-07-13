"""Tests for the desktop backend staging wrapper (scripts/stage-desktop-backend.sh, M-7/S-1 §1.5).

Split into static assertions (the download/pins contract, bash-only, shim-safety) and a
network-free functional run of ``--source-only`` that stages the backend tree, stamps the
git-tag version, and lays out the ranker wheel — the pieces the shim's provision.py consumes.
"""

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / "scripts" / "stage-desktop-backend.sh"
VERSIONS_ENV = REPO_ROOT / "scripts" / "versions.env"


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _bash_executable() -> str:
    if sys.platform == "win32":
        for cand in (r"C:\Program Files\Git\bin\bash.exe", r"C:\Program Files (x86)\Git\bin\bash.exe"):
            if os.path.exists(cand):
                return cand
    return shutil.which("bash") or "bash"


# ── static contract ──────────────────────────────────────────────────────────


def test_script_exists_and_is_bash():
    assert SCRIPT.exists()
    assert _read(SCRIPT).splitlines()[0].startswith("#!/usr/bin/env bash")


def test_script_sources_shared_pins_and_version_helper():
    text = _read(SCRIPT)
    assert 'source "$ROOT/scripts/versions.env"' in text
    assert 'source "$ROOT/scripts/lib/version.sh"' in text
    # version-stamped from the git tag via the shared normalizer — not a hardcoded literal.
    assert "pep440_version" in text


def test_versions_env_declares_mediainfo_pins():
    env = _read(VERSIONS_ENV)
    assert "MEDIAINFO_VERSION_MACOS=" in env
    assert "MEDIAINFO_VERSION_LINUX=" in env


def test_script_stages_expected_layout():
    text = _read(SCRIPT)
    assert 'MSA_DIR="$BACKEND_DIR/msa"' in text
    assert 'WHEELS_DIR="$BACKEND_DIR/wheels"' in text
    assert 'BIN_DIR="$DEST/bin"' in text
    # the committed shim (backend/app/) must never be removed by staging — only backend/msa is.
    assert 'rm -rf "$MSA_DIR"' in text
    assert 'rm -rf "$BACKEND_DIR"' not in text
    assert 'rm -rf "$BACKEND_DIR/app"' not in text


def test_script_references_pinned_tool_downloads():
    text = _read(SCRIPT)
    assert "astral-sh/uv/releases/download/${UV_VERSION}" in text
    # macOS/Linux stage the pure-Perl exiftool (a system Perl runs it directly).
    assert "exiftool/exiftool/archive/refs/tags/${EXIFTOOL_VERSION}" in text
    assert "MediaInfo_CLI_${MEDIAINFO_VERSION_MACOS}_Mac.dmg" in text
    # Windows has no system Perl and shutil.which() can't resolve an extensionless Perl script,
    # so the Windows branch stages the NATIVE build from the SourceForge zip (mirrors the shell
    # bundle). exiftool.org only keeps the latest release, so the pinned version comes from SF.
    assert "exiftool-${EXIFTOOL_VERSION}_64.zip" in text
    assert "sourceforge.net/projects/exiftool/files" in text


def test_windows_branch_stages_native_exiftool_exe_and_files_dir():
    # F-8: on Windows the sidecar needs exiftool.exe PLUS its sibling exiftool_files/ runtime dir
    # (the exe alone fails on most operations). macOS/Linux keep the pure-Perl exiftool + lib/.
    text = _read(SCRIPT)
    assert 'plat="$(detect_platform)"' in text  # platform-conditional, like stage_uv/stage_mediainfo
    # Windows layout: native exe + its runtime dir, both staged into BIN_DIR.
    assert '"$BIN_DIR/exiftool.exe"' in text
    assert '"$BIN_DIR/exiftool_files"' in text
    # macOS/Linux layout: the pure-Perl script + its module lib/ stay.
    assert '"$BIN_DIR/exiftool"' in text
    assert '"$BIN_DIR/lib"' in text
    # Hard-fail guards mirror installer/windows-native/shell/build-bundle.sh.
    assert "exiftool(-k).exe not found" in text
    assert "exiftool_files/ not found" in text
    # The zip carries Windows read-only attrs; chmod -R u+w keeps the cleanup rm from failing.
    assert "chmod -R u+w" in text


def test_macos_mediainfo_extracts_pkg_payload_not_bare_find():
    # F-9: the macOS mediainfo DMG contains a .pkg installer, NOT a bare `mediainfo` binary, so a
    # plain `find -name mediainfo` over the mounted DMG finds nothing and stages an empty result.
    # The fix ports the proven installer/macos/shell/build-bundle.sh path: mount -> pkgutil
    # --expand the .pkg -> cpio-extract the gzipped Payload -> lipo -thin the universal binary.
    text = _read(SCRIPT)
    assert "MediaInfo_CLI_${MEDIAINFO_VERSION_MACOS}_Mac.dmg" in text
    assert "hdiutil attach" in text
    assert "find \"$mount\" -name '*.pkg'" in text  # locate the installer inside the DMG
    assert "pkgutil --expand" in text
    assert "-name Payload" in text                  # the gzipped cpio archive inside the pkg
    assert "cpio -id" in text
    assert "lipo -thin" in text                     # thin the universal binary to the host arch
    # Regression guard: the old code searched the raw mount point for the binary directly.
    assert 'payload="$mount"' not in text


def test_windows_and_linux_mediainfo_extract_via_guarded_find():
    # Both the Windows (.exe at zip root) and Linux (bin/ subdir) mediainfo zips are extracted
    # with a guarded `find` — never a hardcoded path that could silently stage nothing.
    text = _read(SCRIPT)
    assert "MediaInfo_CLI_${MEDIAINFO_VERSION_LINUX}_Windows_x64.zip" in text
    assert "find \"$tmp/mi\" -iname 'mediainfo.exe' -type f" in text
    assert "MediaInfo.exe not found in Windows zip package" in text
    assert "find \"$tmp/mi\" -name mediainfo -type f" in text
    assert "mediainfo binary not found in Linux zip package" in text
    # The pre-fix hardcoded top-level copy (which misses the Linux bin/ subdir) must be gone.
    assert 'cp "$tmp/mi/MediaInfo.exe"' not in text


def test_staging_hardens_against_stale_stub_false_pass():
    # F-8/F-9 both "passed" locally only because a stale gitignored stub sat in src-tauri/bin/.
    # The single-file tools are cleared before staging and validated as REAL binaries afterward,
    # so a leftover stub can neither survive nor satisfy a bare existence check on a clean runner.
    text = _read(SCRIPT)
    assert "assert_compiled_binary()" in text                 # the shared real-binary guard
    assert 'rm -f "$BIN_DIR/mediainfo" "$BIN_DIR/mediainfo.exe"' in text
    assert 'rm -f "$BIN_DIR/uv" "$BIN_DIR/uv.exe"' in text
    assert 'rm -f "$BIN_DIR/exiftool" "$BIN_DIR/exiftool.exe"' in text
    # The guard rejects both a too-small placeholder and a shell-script shebang stub.
    assert "-ge \"$min_bytes\"" in text
    assert "begins with a script shebang" in text
    # …and it is actually invoked on the staged uv + mediainfo outputs.
    assert text.count('assert_compiled_binary "$out"') >= 2


def test_script_excludes_ui_build_products():
    text = _read(SCRIPT)
    assert "--exclude='src/msa_apps/ui/node_modules'" in text
    assert "--exclude='src/msa_apps/ui/dist'" in text


# ── functional: --source-only (no network) ───────────────────────────────────


@pytest.fixture
def staged(tmp_path):
    dest = tmp_path / "src-tauri"
    dest.mkdir()
    env = dict(os.environ, MSA_STAGE_VERSION="9.9.9")
    # Pass POSIX-style paths (forward slashes) to Git Bash. On Windows, str(Path)
    # yields a backslash path (C:\...\src-tauri) that MSYS mangles when it rebuilds
    # argv from the Windows command line, so the script saw the dest as an unknown
    # argument and exited 2. `as_posix()` (C:/.../src-tauri) is handled natively.
    proc = subprocess.run(
        [_bash_executable(), SCRIPT.as_posix(), "--source-only", "--dest", dest.as_posix()],
        cwd=str(REPO_ROOT), env=env, capture_output=True, text=True,
    )
    if proc.returncode != 0:
        raise AssertionError(
            "stage-desktop-backend.sh --source-only exited "
            f"{proc.returncode}\n--- stdout ---\n{proc.stdout}\n--- stderr ---\n{proc.stderr}"
        )
    return dest


def test_source_only_stages_project_tree(staged):
    msa = staged / "backend" / "msa"
    assert (msa / "pyproject.toml").exists()
    assert (msa / "src" / "msa_apps" / "search_api" / "sidecar.py").exists()
    assert (msa / "requirements-api.txt").exists()


def test_source_only_stamps_git_tag_version_not_placeholder(staged):
    pyproject = (staged / "backend" / "msa" / "pyproject.toml").read_text()
    assert 'version = "9.9.9"' in pyproject
    assert "0.0.0.dev0" not in pyproject  # the placeholder must be replaced


def test_source_only_stages_a_config_template(staged):
    # The host platform's template is staged under the name the shim reads.
    assert (staged / "backend" / "msa" / "config.yaml.template").exists()


def test_source_only_excludes_node_modules(staged):
    assert not (staged / "backend" / "msa" / "src" / "msa_apps" / "ui" / "node_modules").exists()


def test_source_only_lays_out_wheels_dir(staged):
    # Directory always exists; it holds msa_ranker-*.whl only on private builds (ADR-011).
    assert (staged / "backend" / "wheels").is_dir()
