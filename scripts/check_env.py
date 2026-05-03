#!/usr/bin/env python3
"""
Environment verification for media-search-agent.

Fails if required external tools are missing or too old.
Minimum versions are read from scripts/versions.env — the single source of
truth for all pinned tool versions.

Called by:
  - scripts/dev-setup.sh   — end of developer bootstrap
  - Can also be run standalone: python scripts/check_env.py
"""
import shutil
import subprocess
import sys
from pathlib import Path

# Read versions.env — the single source of truth for pinned tool versions.
_versions: dict[str, str] = {}
for _line in (Path(__file__).parent / "versions.env").read_text().splitlines():
    _line = _line.strip()
    if _line and not _line.startswith("#") and "=" in _line:
        _k, _v = _line.split("=", 1)
        _versions[_k.strip()] = _v.strip()

EXIFTOOL_MIN = _versions["EXIFTOOL_VERSION"]

_FIX = {
    "linux":   "bash scripts/dev-setup.sh",
    "macos":   "brew install/upgrade the tool",
    "windows": "winget install the tool  (see scripts/tools notes)",
}
_platform = "windows" if sys.platform == "win32" else "macos" if sys.platform == "darwin" else "linux"

problems: list[str] = []

# ── exiftool: presence + version gate ────────────────────────────────────────

exiftool_exe = shutil.which("exiftool")
if not exiftool_exe:
    problems.append(f"exiftool not found\n  Fix: {_FIX[_platform]}")
else:
    try:
        ver_str = subprocess.check_output(
            [exiftool_exe, "-ver"], text=True, timeout=5
        ).strip()
        parts = tuple(int(x) for x in ver_str.split(".")[:2])
        min_parts = tuple(int(x) for x in EXIFTOOL_MIN.split(".")[:2])
        if parts < min_parts:
            problems.append(
                f"exiftool {ver_str} is too old (need >= {EXIFTOOL_MIN}).\n"
                "  The apt package libimage-exiftool-perl cannot extract GoPro GPS.\n"
                f"  Fix: {_FIX[_platform]}"
            )
    except Exception:
        pass  # cannot determine version; assume OK

# ── mediainfo: presence only ─────────────────────────────────────────────────
# mediainfo is not required on Windows — pymediainfo bundles libmediainfo.dll.

_required = ("mediainfo",) if sys.platform != "win32" else ()
for tool in _required:
    if not shutil.which(tool):
        problems.append(f"{tool} not found\n  Fix: {_FIX[_platform]}")

if problems:
    sys.stderr.write("\nERROR: Environment check failed:\n\n")
    for p in problems:
        sys.stderr.write(f"  • {p}\n\n")
    sys.exit(1)

tools = "exiftool, mediainfo" if sys.platform != "win32" else "exiftool"
print(f"Environment OK: {tools} found.")
