from __future__ import annotations

import os
from pathlib import Path
from typing import Iterator, Literal, Optional

IMAGE_EXT = {".jpg", ".jpeg", ".png", ".heic", ".tif", ".tiff", ".webp"}
VIDEO_EXT = {".mp4", ".mov", ".m4v", ".avi", ".mkv", ".wmv", ".flv", ".webm"}

def _should_skip_name(name: str) -> bool:
    return name.startswith(".")


def _matches_media_type(ext: str, media_type: Optional[Literal["image", "video"]]) -> bool:
    if media_type == "image":
        return ext in IMAGE_EXT
    if media_type == "video":
        return ext in VIDEO_EXT
    return ext in IMAGE_EXT or ext in VIDEO_EXT


def iter_media(
    root: Path,
    media_type: Optional[Literal["image", "video"]] = None,
    stop_event=None,
) -> Iterator[Path]:
    """
    Iterate over media files in the given directory tree.

    Uses os.scandir() rather than Path.rglob("*") because directory-entry iteration is
    dramatically faster on broad cloud-backed roots (notably macOS OneDrive / File
    Provider folders) and is less likely to trigger expensive provider work.

    Args:
        root: Root directory to scan recursively
        media_type: Optional filter - "image" to return only images, "video" to return
            only videos. If None, returns both images and videos.
        stop_event: Optional threading/event-like object. If set during iteration,
            scanning stops early.

    Yields:
        Path objects for matching media files.
    """
    root = Path(root)
    if not root.exists() or not root.is_dir():
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
                    except OSError:
                        continue

                    ext = os.path.splitext(name)[1].lower()
                    if _matches_media_type(ext, media_type):
                        yield Path(entry.path)
        except OSError:
            continue
