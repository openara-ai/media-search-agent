from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator, Literal, Optional

IMAGE_EXT = {".jpg", ".jpeg", ".png", ".heic", ".tif", ".tiff", ".webp"}
VIDEO_EXT = {".mp4", ".mov", ".m4v", ".avi", ".mkv", ".wmv", ".flv", ".webm"}


@dataclass
class ScanStats:
    """Mutable per-walk statistics.

    ``walk_errors`` counts every swallowed OSError (unreadable entry or
    directory) — including a root that is missing or not a directory at
    walk start. The deletion sweep treats any non-zero count as "this
    source was NOT walked to completion" — a transient I/O error or
    unmount must never look like mass deletion (M-8 plan §3.4, R3).
    """

    walk_errors: int = 0


def _should_skip_name(name: str) -> bool:
    return name.startswith(".")


def _matches_media_type(ext: str, media_type: Optional[Literal["image", "video"]]) -> bool:
    if media_type == "image":
        return ext in IMAGE_EXT
    if media_type == "video":
        return ext in VIDEO_EXT
    return ext in IMAGE_EXT or ext in VIDEO_EXT


def iter_media_entries(
    root: Path,
    media_type: Optional[Literal["image", "video"]] = None,
    stop_event=None,
    stats: Optional[ScanStats] = None,
) -> Iterator[tuple[Path, os.stat_result]]:
    """
    Iterate over media files in the given directory tree, yielding each
    file together with its ``os.stat_result``.

    Uses os.scandir() rather than Path.rglob("*") because directory-entry
    iteration is dramatically faster on broad cloud-backed roots (notably
    macOS OneDrive / File Provider folders) and is less likely to trigger
    expensive provider work. The stat comes from
    ``DirEntry.stat(follow_symlinks=False)`` — cached on Windows, one
    syscall elsewhere — so callers get size/mtime nearly for free
    (fingerprint fast-path, M-8 plan §3.2).

    Args:
        root: Root directory to scan recursively
        media_type: Optional filter - "image" to return only images, "video"
            to return only videos. If None, returns both images and videos.
        stop_event: Optional threading/event-like object. If set during
            iteration, scanning stops early.
        stats: Optional ScanStats; ``walk_errors`` is incremented for every
            swallowed OSError so callers can disqualify the walk from
            deletion sweeping (R3).

    Yields:
        (Path, os.stat_result) tuples for matching media files.
    """
    root = Path(root)
    if not root.exists() or not root.is_dir():
        # A missing/non-directory root is a walk failure, not an empty
        # walk: a source root that disappears between run_index's upfront
        # validation and walk start (transient unmount, e.g. network or
        # external drive) would otherwise return with walk_errors == 0 and
        # the deletion sweep would treat the source as cleanly walked —
        # two such runs would grace-then-tombstone every fingerprint under
        # it (the R3 mass-tombstone trap).
        if stats is not None:
            stats.walk_errors += 1
        return

    stack = [root]

    while stack:
        if stop_event is not None and stop_event.is_set():
            return

        current = stack.pop()
        try:
            with os.scandir(current) as entries:
                for entry in entries:
                    if stop_event is not None and stop_event.is_set():
                        return

                    name = entry.name
                    if _should_skip_name(name):
                        continue

                    try:
                        if entry.is_dir(follow_symlinks=False):
                            stack.append(Path(entry.path))
                            continue

                        if not entry.is_file(follow_symlinks=False):
                            continue

                        ext = os.path.splitext(name)[1].lower()
                        if not _matches_media_type(ext, media_type):
                            continue

                        st = entry.stat(follow_symlinks=False)
                    except OSError:
                        if stats is not None:
                            stats.walk_errors += 1
                        continue

                    yield Path(entry.path), st
        except OSError:
            if stats is not None:
                stats.walk_errors += 1
            continue


def iter_media(
    root: Path,
    media_type: Optional[Literal["image", "video"]] = None,
    stop_event=None,
) -> Iterator[Path]:
    """
    Iterate over media files in the given directory tree.

    Thin wrapper over :func:`iter_media_entries` (drops the stat result)
    so existing callers are unchanged.

    Args:
        root: Root directory to scan recursively
        media_type: Optional filter - "image" to return only images, "video"
            to return only videos. If None, returns both images and videos.
        stop_event: Optional threading/event-like object. If set during
            iteration, scanning stops early.

    Yields:
        Path objects for matching media files.
    """
    for path, _st in iter_media_entries(root, media_type=media_type, stop_event=stop_event):
        yield path
