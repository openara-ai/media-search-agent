from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "tests" / "real_media" / "run-local.sh"

_skip_on_windows = pytest.mark.skipif(
    sys.platform == "win32",
    reason="run-local.sh is a Linux/macOS bash script; no WSL distro on Windows CI",
)


@_skip_on_windows
def test_run_local_help():
    result = subprocess.run(
        ["bash", str(SCRIPT), "--help"],
        capture_output=True,
        text=True,
        check=False,
        cwd=str(REPO_ROOT),
    )
    assert result.returncode == 0
    assert "--source-mode committed|staged|dirty" in result.stdout
    assert "--report-dir <path>" in result.stdout


@_skip_on_windows
def test_run_local_rejects_invalid_source_mode():
    result = subprocess.run(
        ["bash", str(SCRIPT), "--source-mode", "bogus"],
        capture_output=True,
        text=True,
        check=False,
        cwd=str(REPO_ROOT),
    )
    assert result.returncode != 0
    assert "--source-mode must be one of" in result.stderr or "--source-mode must be one of" in result.stdout


@_skip_on_windows
def test_run_local_report_dir_path_resolution_is_portable(tmp_path: Path):
    report_dir = tmp_path / "reports"
    result = subprocess.run(
        [
            "bash",
            str(SCRIPT),
            "--report-dir",
            str(report_dir),
            "--skip-index",
            "--skip-api",
            "--skip-slow-model-checks",
            "--keep-workspace",
            "never",
        ],
        capture_output=True,
        text=True,
        check=False,
        cwd=str(REPO_ROOT),
    )
    combined_output = f"{result.stdout}\n{result.stderr}"
    assert "Resolved configuration:" in result.stdout
    assert "realpath: illegal option -- m" not in combined_output
