from __future__ import annotations

import threading
from pathlib import Path

from msa_indexer.io.scanner import iter_media


def test_iter_media_finds_supported_images_and_videos(tmp_path: Path):
    (tmp_path / "photo.jpg").touch()
    (tmp_path / "clip.mp4").touch()
    (tmp_path / "note.txt").touch()

    nested = tmp_path / "nested"
    nested.mkdir()
    (nested / "still.heic").touch()

    paths = sorted(path.relative_to(tmp_path).as_posix() for path in iter_media(tmp_path))

    assert paths == ["clip.mp4", "nested/still.heic", "photo.jpg"]


def test_iter_media_skips_hidden_files_and_directories(tmp_path: Path):
    (tmp_path / "visible.jpg").touch()
    (tmp_path / ".hidden.jpg").touch()

    hidden_dir = tmp_path / ".hidden_dir"
    hidden_dir.mkdir()
    (hidden_dir / "inside.mp4").touch()

    paths = [path.name for path in iter_media(tmp_path)]

    assert paths == ["visible.jpg"]


def test_iter_media_respects_media_type_filter(tmp_path: Path):
    (tmp_path / "photo.jpg").touch()
    (tmp_path / "clip.mp4").touch()

    images = [path.name for path in iter_media(tmp_path, media_type="image")]
    videos = [path.name for path in iter_media(tmp_path, media_type="video")]

    assert images == ["photo.jpg"]
    assert videos == ["clip.mp4"]


def test_iter_media_stops_when_stop_event_is_set(tmp_path: Path):
    for idx in range(5):
        (tmp_path / f"photo_{idx}.jpg").touch()

    stop_event = threading.Event()
    iterator = iter_media(tmp_path, stop_event=stop_event)

    first = next(iterator)
    assert first.name.startswith("photo_")

    stop_event.set()
    remaining = list(iterator)

    assert remaining == []
