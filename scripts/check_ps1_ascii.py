#!/usr/bin/env python3
"""Enforce ASCII + LF for *.ps1 files.

Windows PowerShell 5.1 reads BOM-less files as the system ANSI codepage
(Windows-1252 on en-US runners). UTF-8 multi-byte sequences then decode to
unintended cp1252 characters, which can confuse the PS 5.1 lexer mid-string
and cascade into "missing closing brace" parse errors that only surface
when the script actually runs on a Windows runner (typically during a
release-tag BVT, far from the commit that introduced the byte).

This check rejects:
  - any byte > 0x7F (non-ASCII)
  - any byte 0x0D (CR; LF-only line endings are project policy)

Usage:
    python scripts/check_ps1_ascii.py [file ...]
        no args: scan every *.ps1 in the repo (excluding .git, node_modules,
        .venv, dist).
        with args: scan only the listed files (pre-commit pass-through).
"""
from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
EXCLUDED_DIRS = {".git", "node_modules", ".venv", "dist", "build"}

# Vendored from desktop-app-template-spike (read-only); ASCII-clean must be fixed
# upstream + re-vendored before S-3 Windows packaging - see tracking issue #163.
# These two build scripts are byte-identical copies from the spike and carry em-dash/section
# chars in their comments. The exemption is narrow (exactly these two repo-relative paths);
# every MSA-authored .ps1 (installer/**, tests/**) stays strictly ASCII + LF.
VENDORED_PS1 = frozenset({
    Path("scripts/build-app.ps1"),
    Path("scripts/build-backend.ps1"),
})


def _is_vendored(path: Path) -> bool:
    """True iff ``path`` is one of the read-only vendored build scripts (compared as a
    repo-relative path, so it works for both discovered and explicitly-passed args)."""
    try:
        rel = path.resolve().relative_to(REPO_ROOT)
    except (ValueError, OSError):
        return False
    return rel in VENDORED_PS1


def discover_ps1() -> list[Path]:
    out: list[Path] = []
    for path in REPO_ROOT.rglob("*.ps1"):
        rel = path.relative_to(REPO_ROOT)
        if any(part in EXCLUDED_DIRS for part in rel.parts):
            continue
        if rel in VENDORED_PS1:
            continue  # read-only vendored script - exempt (see VENDORED_PS1)
        out.append(path)
    return out


def scan(path: Path) -> list[tuple[int, int, int]]:
    """Return [(line, col, byte_value), ...] for offending bytes."""
    data = path.read_bytes()
    issues: list[tuple[int, int, int]] = []
    line, col = 1, 1
    for b in data:
        if b == 0x0A:
            line += 1
            col = 1
            continue
        if b > 0x7F or b == 0x0D:
            issues.append((line, col, b))
        col += 1
    return issues


def main(argv: list[str]) -> int:
    if argv:
        files = [Path(a) for a in argv]
    else:
        files = discover_ps1()

    bad: list[tuple[Path, list[tuple[int, int, int]]]] = []
    scanned = 0
    for f in files:
        if f.suffix.lower() != ".ps1":
            continue
        if _is_vendored(f):
            continue  # exempt even when passed explicitly (pre-commit) - see VENDORED_PS1
        scanned += 1
        issues = scan(f)
        if issues:
            bad.append((f, issues))

    if not bad:
        print(f"OK: scanned {scanned} .ps1 file(s); ASCII + LF clean.")
        return 0

    print(f"FAIL: non-ASCII or CR bytes in {len(bad)} .ps1 file(s):")
    for path, issues in bad:
        try:
            rel = path.relative_to(REPO_ROOT)
        except ValueError:
            rel = path
        for line, col, b in issues[:10]:
            label = "CR" if b == 0x0D else f"0x{b:02x}"
            print(f"  {rel}:{line}:{col}  byte {label}")
        if len(issues) > 10:
            print(f"  ...and {len(issues) - 10} more in {rel}")
    print()
    print("PowerShell 5.1 reads BOM-less files using the system ANSI codepage,")
    print("which misinterprets multi-byte UTF-8 sequences and breaks the lexer.")
    print("Use ASCII (e.g. -- instead of em-dash) and LF line endings.")
    return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
