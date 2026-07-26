from __future__ import annotations

import os
import threading
from pathlib import Path

from msa_indexer.io.scanner import ScanStats, iter_media, iter_media_entries


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


# ---------------------------------------------------------------------------
# M-8/S-1: stat-yielding variant + walk-error counter (plan §3.2)
# ---------------------------------------------------------------------------


def test_iter_media_entries_stat_matches_os_stat(tmp_path: Path):
    p = tmp_path / "photo.jpg"
    p.write_bytes(b"jpegdata")

    entries = list(iter_media_entries(tmp_path))
    assert len(entries) == 1
    path, st = entries[0]
    assert path == p

    expected = os.stat(p)
    assert st.st_size == expected.st_size
    assert st.st_mtime_ns == expected.st_mtime_ns


def test_iter_media_wrapper_yields_identical_sequence(tmp_path: Path):
    (tmp_path / "a.jpg").touch()
    (tmp_path / "b.mp4").touch()
    nested = tmp_path / "nested"
    nested.mkdir()
    (nested / "c.heic").touch()
    (tmp_path / "skip.txt").touch()

    via_wrapper = sorted(p.as_posix() for p in iter_media(tmp_path))
    via_entries = sorted(p.as_posix() for p, _st in iter_media_entries(tmp_path))
    assert via_wrapper == via_entries
    assert len(via_wrapper) == 3


def test_iter_media_entries_respects_media_type_filter(tmp_path: Path):
    (tmp_path / "photo.jpg").touch()
    (tmp_path / "clip.mp4").touch()

    images = [p.name for p, _st in iter_media_entries(tmp_path, media_type="image")]
    videos = [p.name for p, _st in iter_media_entries(tmp_path, media_type="video")]
    assert images == ["photo.jpg"]
    assert videos == ["clip.mp4"]


def test_walk_errors_increment_on_unreadable_directory(tmp_path: Path, monkeypatch):
    (tmp_path / "ok.jpg").touch()
    bad_dir = tmp_path / "locked"
    bad_dir.mkdir()
    (bad_dir / "hidden_by_error.jpg").touch()

    real_scandir = os.scandir

    def _failing_scandir(path):
        if Path(path) == bad_dir:
            raise OSError("permission denied (simulated)")
        return real_scandir(path)

    monkeypatch.setattr(os, "scandir", _failing_scandir)

    stats = ScanStats()
    found = [p.name for p, _st in iter_media_entries(tmp_path, stats=stats)]

    assert found == ["ok.jpg"]
    assert stats.walk_errors == 1


def test_walk_errors_increment_on_unstatable_entry(tmp_path: Path, monkeypatch):
    (tmp_path / "ok.jpg").touch()
    (tmp_path / "broken.jpg").touch()

    real_scandir = os.scandir

    class _EntryProxy:
        def __init__(self, entry):
            self._entry = entry
            self.name = entry.name
            self.path = entry.path

        def is_dir(self, follow_symlinks=True):
            return self._entry.is_dir(follow_symlinks=follow_symlinks)

        def is_file(self, follow_symlinks=True):
            return self._entry.is_file(follow_symlinks=follow_symlinks)

        def stat(self, follow_symlinks=True):
            if self.name == "broken.jpg":
                raise OSError("stat failed (simulated)")
            return self._entry.stat(follow_symlinks=follow_symlinks)

    class _ScandirProxy:
        def __init__(self, path):
            self._inner = real_scandir(path)

        def __enter__(self):
            return (_EntryProxy(e) for e in self._inner.__enter__())

        def __exit__(self, *args):
            return self._inner.__exit__(*args)

    monkeypatch.setattr(os, "scandir", lambda path: _ScandirProxy(path))

    stats = ScanStats()
    found = [p.name for p, _st in iter_media_entries(tmp_path, stats=stats)]

    assert found == ["ok.jpg"]
    assert stats.walk_errors == 1


def test_walk_errors_zero_on_clean_walk(tmp_path: Path):
    (tmp_path / "a.jpg").touch()
    stats = ScanStats()
    list(iter_media_entries(tmp_path, stats=stats))
    assert stats.walk_errors == 0


def test_walk_errors_increment_on_missing_root(tmp_path: Path):
    # A root that vanished between run_index's upfront validation and the
    # walk start must count as a walk failure, not an empty walk — the
    # deletion sweep would otherwise treat the source as cleanly walked and
    # grace-then-tombstone everything under it (M-8 plan §3.4, R3).
    stats = ScanStats()
    found = list(iter_media_entries(tmp_path / "vanished", stats=stats))
    assert found == []
    assert stats.walk_errors == 1


def test_walk_errors_increment_on_non_directory_root(tmp_path: Path):
    not_a_dir = tmp_path / "actually_a_file"
    not_a_dir.touch()
    stats = ScanStats()
    found = list(iter_media_entries(not_a_dir, stats=stats))
    assert found == []
    assert stats.walk_errors == 1
