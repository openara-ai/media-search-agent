#!/usr/bin/env python3
"""Check that public docs do not link into internal/.

Reads .public-paths to determine which tracked files ship to the public
openara-ai mirror. For every .md file in the public set, fail if it
contains a reference to an internal/ path.

Usage:
    python scripts/check_doc_separation.py            # scan and report
    python scripts/check_doc_separation.py --list     # print public files
"""
from __future__ import annotations

import argparse
import re
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
MANIFEST = REPO_ROOT / ".public-paths"

INTERNAL_REF_RE = re.compile(
    r"(?:^|[\s(\[\"'`])(?:\.{1,2}/)*internal/", re.MULTILINE
)

# Internal paths that public docs are allowed to name. The agent instruction
# files (CLAUDE.md, AGENTS.md) must spell out internal/docs/CLAUDE-private.md
# verbatim so the next session can load it. Any other internal/ reference in
# a public doc still fails the check.
INTERNAL_PATH_RE = re.compile(r"internal/[^\s)\]\"'`]*")
ALLOWED_INTERNAL_PATHS = {"internal/docs/CLAUDE-private.md"}


def load_patterns() -> tuple[list[str], list[str]]:
    includes: list[str] = []
    excludes: list[str] = []
    for raw in MANIFEST.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("!"):
            excludes.append(line[1:])
        else:
            includes.append(line)
    return includes, excludes


def pattern_to_regex(pattern: str) -> re.Pattern[str]:
    escaped = re.escape(pattern)
    escaped = escaped.replace(r"\*\*", ".*").replace(r"\*", "[^/]*")
    return re.compile(f"^{escaped}$")


def list_tracked_files() -> list[str]:
    out = subprocess.check_output(
        ["git", "-C", str(REPO_ROOT), "ls-files"], text=True
    )
    return [line for line in out.splitlines() if line]


def list_untracked_files() -> list[str]:
    out = subprocess.check_output(
        ["git", "-C", str(REPO_ROOT), "ls-files", "--others", "--exclude-standard"],
        text=True,
    )
    return [line for line in out.splitlines() if line]


def filter_public(paths: list[str]) -> list[str]:
    includes, excludes = load_patterns()
    inc_re = [pattern_to_regex(p) for p in includes]
    exc_re = [pattern_to_regex(p) for p in excludes]
    return [
        p
        for p in paths
        if not any(r.match(p) for r in exc_re)
        and any(r.match(p) for r in inc_re)
    ]


def public_files() -> list[Path]:
    return [REPO_ROOT / p for p in filter_public(list_tracked_files())]


def scan_for_internal_refs(files: list[Path]) -> list[tuple[Path, int, str]]:
    issues: list[tuple[Path, int, str]] = []
    for f in files:
        if f.suffix.lower() != ".md":
            continue
        try:
            text = f.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        for lineno, line in enumerate(text.splitlines(), start=1):
            if INTERNAL_REF_RE.search(line):
                paths = INTERNAL_PATH_RE.findall(line)
                if paths and all(p in ALLOWED_INTERNAL_PATHS for p in paths):
                    continue
                issues.append((f, lineno, line.strip()))
    return issues


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--list",
        action="store_true",
        help="Print the resolved public file set (tracked) and exit.",
    )
    parser.add_argument(
        "--list-untracked-public",
        action="store_true",
        help="Print untracked files that would be public if committed.",
    )
    args = parser.parse_args()

    if args.list_untracked_public:
        for p in sorted(filter_public(list_untracked_files())):
            print(p)
        return 0

    files = public_files()

    if args.list:
        for f in sorted(files):
            print(f.relative_to(REPO_ROOT))
        return 0

    md_count = sum(1 for f in files if f.suffix.lower() == ".md")
    issues = scan_for_internal_refs(files)
    if not issues:
        print(f"OK: scanned {md_count} public .md files; no internal/ links.")
        return 0

    print(f"FAIL: {len(issues)} reference(s) from public docs into internal/:")
    for path, lineno, line in issues:
        rel = path.relative_to(REPO_ROOT)
        print(f"  {rel}:{lineno}: {line}")
    print()
    print("Public docs must not link into internal/. Either:")
    print("  - move the referenced doc into the public docs/ folder, or")
    print("  - rewrite the link to a public-facing equivalent, or")
    print("  - exclude the file from .public-paths if it is meta-internal.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
