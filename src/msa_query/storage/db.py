"""
Read-only SQLite connection helper shared by the API and the query
engine.

This module lives in ``msa_query.storage`` so it can be consumed both
by the higher API layer (``msa_apps``) and by the query engine itself,
without inverting the architectural dependency direction
``msa_apps -> msa_query -> msa_indexer``.

The indexer process owns its own writer connection through
``msa_indexer.db.sqlite_store.SQLiteStore`` and is unaffected. The
small set of API endpoints that mutate SQLite (face labeling, person
management) also go through ``SQLiteStore``. A separate
``connect_writer`` helper is intentionally not provided here — it
would have no consumer today, and per project guidance we don't add
features beyond what the task requires. Add one when a real caller
emerges.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path, PurePath
from typing import Union

PathLike = Union[str, Path]

# busy_timeout is long enough to hide the indexer's brief BEGIN IMMEDIATE
# windows but short enough that a genuinely stuck reader fails fast.
_BUSY_TIMEOUT_MS = 5000


def _build_readonly_uri(p: PurePath) -> str:
    """Build a SQLite read-only URI from an absolute path.

    Delegates to ``pathlib.PurePath.as_uri()`` for proper cross-platform
    escaping:

    - Windows absolute paths get the ``file:///C:/...`` shape.
    - Backslashes convert to forward slashes.
    - Reserved characters (spaces, ``?``, ``#``) are percent-escaped.

    Then appends ``?mode=ro`` so SQLite opens read-only and raises if
    the file does not exist.

    Exposed as a separate helper so the URI-construction logic can be
    unit-tested with ``PureWindowsPath`` / ``PurePosixPath`` instances
    on any platform — the native Windows build path is exercised in
    CI even when tests run on macOS or Linux.
    """
    return f"{p.as_uri()}?mode=ro"


def connect_readonly(path: PathLike) -> sqlite3.Connection:
    """Open a read-only SQLite connection.

    Uses SQLite URI ``mode=ro`` so a missing file raises
    ``sqlite3.OperationalError: unable to open database file`` instead
    of silently creating an empty DB. Also sets ``query_only=1`` (a
    second layer of read-only enforcement at the SQL surface) and
    ``busy_timeout=5000`` so brief writer-lock windows from the indexer
    are waited on rather than errored on.

    Use this from API endpoints and query-engine helpers that should
    never mutate SQLite.

    Raises:
        sqlite3.OperationalError: if the database file does not exist.
    """
    p = Path(path).absolute()
    uri = _build_readonly_uri(p)
    conn = sqlite3.connect(uri, uri=True)
    conn.execute(f"PRAGMA busy_timeout = {_BUSY_TIMEOUT_MS}")
    conn.execute("PRAGMA query_only = 1")
    return conn
