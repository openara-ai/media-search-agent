"""
Unit tests for msa_query.storage.db connection helpers.

Stage 2 of internal/docs/storage/SQLITE_INCREMENTAL_VISIBILITY_PLAN.md:

- ``connect_readonly`` opens a SQLite connection in URI ``mode=ro`` with
  ``query_only=1`` and ``busy_timeout=5000``. A missing file raises
  rather than silently creating an empty DB; any attempted write also
  raises; brief writer-lock windows in the indexer don't error readers.
"""
from __future__ import annotations

import sqlite3
from pathlib import PurePosixPath, PureWindowsPath

import pytest

from msa_query.storage.db import _build_readonly_uri, connect_readonly


@pytest.fixture
def seeded_db(tmp_path):
    """A SQLite DB with one table and one row."""
    db_path = tmp_path / "test.sqlite"
    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute("CREATE TABLE thing (id INTEGER PRIMARY KEY, name TEXT)")
        conn.execute("INSERT INTO thing(id, name) VALUES (1, 'alpha')")
        conn.commit()
    finally:
        conn.close()
    return db_path


def _busy_timeout(conn: sqlite3.Connection) -> int:
    return conn.execute("PRAGMA busy_timeout").fetchone()[0]


def _query_only(conn: sqlite3.Connection) -> int:
    return conn.execute("PRAGMA query_only").fetchone()[0]


# ---------------------------------------------------------------------------
# connect_readonly
# ---------------------------------------------------------------------------


def test_connect_readonly_can_read(seeded_db):
    conn = connect_readonly(seeded_db)
    try:
        rows = conn.execute("SELECT id, name FROM thing").fetchall()
        assert rows == [(1, "alpha")]
    finally:
        conn.close()


def test_connect_readonly_blocks_inserts(seeded_db):
    conn = connect_readonly(seeded_db)
    try:
        with pytest.raises(sqlite3.OperationalError):
            conn.execute("INSERT INTO thing(id, name) VALUES (2, 'beta')")
    finally:
        conn.close()


def test_connect_readonly_blocks_updates(seeded_db):
    conn = connect_readonly(seeded_db)
    try:
        with pytest.raises(sqlite3.OperationalError):
            conn.execute("UPDATE thing SET name='zeta' WHERE id=1")
    finally:
        conn.close()


def test_connect_readonly_sets_busy_timeout(seeded_db):
    conn = connect_readonly(seeded_db)
    try:
        assert _busy_timeout(conn) == 5000
    finally:
        conn.close()


def test_connect_readonly_sets_query_only(seeded_db):
    conn = connect_readonly(seeded_db)
    try:
        assert _query_only(conn) == 1
    finally:
        conn.close()


def test_connect_readonly_accepts_str_or_path(seeded_db):
    conn1 = connect_readonly(seeded_db)
    conn2 = connect_readonly(str(seeded_db))
    try:
        for conn in (conn1, conn2):
            rows = conn.execute("SELECT id FROM thing").fetchall()
            assert rows == [(1,)]
    finally:
        conn1.close()
        conn2.close()


# ---------------------------------------------------------------------------
# Missing-file behavior — connect_readonly must NOT silently create an
# empty DB. The URI ``mode=ro`` ensures SQLite raises instead.
# ---------------------------------------------------------------------------


def test_connect_readonly_raises_on_missing_file(tmp_path):
    missing = tmp_path / "does_not_exist.sqlite"
    assert not missing.exists()
    with pytest.raises(sqlite3.OperationalError):
        connect_readonly(missing)
    # Critical: the file must NOT have been created as a side effect.
    assert not missing.exists(), (
        "connect_readonly must not create the database file when missing"
    )


def test_connect_readonly_raises_on_missing_path_str(tmp_path):
    missing = tmp_path / "still_missing.sqlite"
    with pytest.raises(sqlite3.OperationalError):
        connect_readonly(str(missing))
    assert not missing.exists()


# ---------------------------------------------------------------------------
# URI construction — cross-platform via Pure*Path. Verifies that the
# SQLite URI we hand to sqlite3.connect handles Windows drive letters,
# backslash-to-slash conversion, and percent-escaping of reserved
# characters. These tests run on any platform; the native Windows build
# is exercised through PureWindowsPath even when CI runs on macOS/Linux.
# ---------------------------------------------------------------------------


def test_build_uri_posix_simple():
    uri = _build_readonly_uri(PurePosixPath("/var/data/media.sqlite"))
    assert uri == "file:///var/data/media.sqlite?mode=ro"


def test_build_uri_posix_with_spaces():
    uri = _build_readonly_uri(PurePosixPath("/Users/Alice/My Photos/db.sqlite"))
    assert uri == "file:///Users/Alice/My%20Photos/db.sqlite?mode=ro"


def test_build_uri_windows_drive_letter():
    """Windows absolute paths must get the ``/C:/...`` shape."""
    uri = _build_readonly_uri(PureWindowsPath(r"C:\Users\Alice\media.sqlite"))
    assert uri == "file:///C:/Users/Alice/media.sqlite?mode=ro"


def test_build_uri_windows_backslashes_become_slashes():
    uri = _build_readonly_uri(PureWindowsPath(r"D:\Photos\Library\db.sqlite"))
    assert "\\" not in uri
    assert uri == "file:///D:/Photos/Library/db.sqlite?mode=ro"


def test_build_uri_windows_path_with_spaces_is_percent_escaped():
    uri = _build_readonly_uri(PureWindowsPath(r"C:\Users\Alice\My Stuff\db.sqlite"))
    assert "%20" in uri
    assert uri == "file:///C:/Users/Alice/My%20Stuff/db.sqlite?mode=ro"


def test_build_uri_windows_percent_escapes_question_mark_in_path():
    """A literal ? in the path must be percent-escaped so SQLite doesn't
    interpret it as the start of the URI query string.
    """
    uri = _build_readonly_uri(PureWindowsPath(r"C:\weird\name?with?qm.sqlite"))
    # The ? in the filename is escaped; only the trailing ?mode=ro is the
    # actual URI query separator.
    assert uri.endswith("?mode=ro")
    # Before the trailing ?mode=ro, no unescaped ? should appear.
    body = uri[: -len("?mode=ro")]
    assert "?" not in body, f"unexpected ? in URI body: {uri}"
    assert "%3F" in body  # the question marks are percent-escaped


def test_build_uri_windows_percent_escapes_hash():
    uri = _build_readonly_uri(PureWindowsPath(r"C:\weird\name#hash.sqlite"))
    assert "#" not in uri.split("?", 1)[0]
    assert "%23" in uri


def test_connect_readonly_handles_path_with_spaces(tmp_path):
    """End-to-end: a path with spaces on the current platform opens
    successfully via the URI helper.
    """
    db_path = tmp_path / "name with spaces.sqlite"
    seed = sqlite3.connect(str(db_path))
    try:
        seed.execute("CREATE TABLE x (id INTEGER PRIMARY KEY)")
        seed.commit()
    finally:
        seed.close()

    conn = connect_readonly(db_path)
    try:
        rows = conn.execute("SELECT id FROM x").fetchall()
        assert rows == []
    finally:
        conn.close()
