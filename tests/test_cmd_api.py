"""
Unit tests for src/msa_cli/cmd_api.py

Covers:
  - _pid_alive: returns correct values for live/dead/implausible PIDs
  - _pid_alive: Windows path uses ctypes instead of os.kill(pid, 0) (static)
  - _cmd_start: stale PID file is removed rather than blocking start (static)
  - _pids_on_port / _pid_cmdline: Windows paths use platform tools (static)
"""

import os
import subprocess
import sys
from pathlib import Path

import pytest

from msa_cli.cmd_api import _pid_alive

REPO_ROOT = Path(__file__).resolve().parents[1]
CMD_API_SRC = REPO_ROOT / "src" / "msa_cli" / "cmd_api.py"


# ── _pid_alive behavioural tests (platform-independent) ──────────────────────

def test_pid_alive_true_for_current_process():
    assert _pid_alive(os.getpid()) is True


def test_pid_alive_false_for_completed_process():
    p = subprocess.Popen(
        [sys.executable, "-c", "import sys; sys.exit(0)"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    p.wait()
    assert _pid_alive(p.pid) is False


def test_pid_alive_false_for_implausible_pid():
    # PID 2**30 is beyond the OS limit on any supported platform.
    assert _pid_alive(2 ** 30) is False


# ── _pid_alive static: Windows must not use os.kill(pid, 0) ──────────────────

def test_pid_alive_windows_path_uses_ctypes_not_os_kill():
    """WIN-002: os.kill(pid, 0) raises SystemError on Windows for dead PIDs.
    The Windows branch must use ctypes OpenProcess + GetExitCodeProcess instead.
    """
    src = CMD_API_SRC.read_text(encoding="utf-8")
    assert 'sys.platform == "win32"' in src, \
        "_pid_alive must have a sys.platform == 'win32' guard"
    assert "OpenProcess" in src, \
        "_pid_alive Windows path must use OpenProcess"
    assert "GetExitCodeProcess" in src, \
        "_pid_alive Windows path must use GetExitCodeProcess"
    # os.kill(pid, 0) must only appear in the POSIX branch, not before the guard.
    win32_guard_pos = src.index('sys.platform == "win32"')
    # Ensure os.kill is only present after the Windows block (i.e. in the else branch).
    kill_pos = src.index("os.kill(pid, 0)")
    assert kill_pos > win32_guard_pos, \
        "os.kill(pid, 0) must be in the POSIX branch, not before the win32 guard"


# ── _cmd_start: stale PID cleanup (static) ───────────────────────────────────

def test_cmd_start_removes_stale_pid_file_before_port_check():
    """WIN-002: A dead PID in the PID file must be cleaned up, not block start."""
    src = CMD_API_SRC.read_text(encoding="utf-8")
    assert "unlink(missing_ok=True)" in src, \
        "_cmd_start must call pid_path.unlink(missing_ok=True) for stale PIDs"
    # The stale-file cleanup must appear before the port-in-use check so that a
    # stale file does not leave the port check as the only gate.
    stale_pos = src.index("unlink(missing_ok=True)")
    port_check_pos = src.index("_port_in_use(port)")
    assert stale_pos < port_check_pos, \
        "Stale PID cleanup must come before _port_in_use() in _cmd_start"


# ── _pids_on_port / _pid_cmdline: Windows platform tools (static) ────────────

def test_pids_on_port_has_windows_netstat_path():
    """WIN-004: lsof/ss are POSIX-only; Windows must use netstat -ano."""
    src = CMD_API_SRC.read_text(encoding="utf-8")
    assert "netstat" in src, \
        "_pids_on_port must use 'netstat' on Windows (lsof/ss are not available)"
    assert "LISTENING" in src, \
        "_pids_on_port Windows path must filter 'netstat -ano' output by LISTENING state"


def test_pid_cmdline_has_windows_wmic_path():
    """WIN-004: ps is POSIX-only; Windows must use wmic to get process cmdline."""
    src = CMD_API_SRC.read_text(encoding="utf-8")
    assert "wmic" in src, \
        "_pid_cmdline must use 'wmic' on Windows (ps is not available)"
    assert "CommandLine" in src, \
        "_pid_cmdline Windows path must query the CommandLine attribute via wmic"
