"""Unified rotating desktop log (M-7/S-2 spec item 5).

The Tauri supervisor drains the sidecar's stdout/stderr into an internal log the user never
sees, so a bug report or a stuck first run has nothing to look at. This installs a single
``RotatingFileHandler`` on the root logger writing ``<LOG_DIR>/msa-desktop.log`` — the PRIMARY
troubleshooting artifact, spanning the provisioning shim and (same process) uvicorn. The
per-run ``provision-<ts>.log`` the responder points at in its ``log`` field stays UNCHANGED and
coexists (it holds the full, verbose ``uv`` output for that one run).

Single owner per process (LOG-001 discipline — never two rotating handles on one file, which
throws ``WinError 32`` on Windows rotation): ``configure`` is idempotent (a sentinel-marked
handler is added at most once), and the sidecar's mirror configure detects the same handler and
no-ops. Within the one shim→uvicorn process, exactly one handler rotates the file. The shim
runs before the venv has any third-party deps, so this module is stdlib-only.
"""

from __future__ import annotations

import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path

LOG_NAME = "msa-desktop.log"
_MAX_BYTES = 5 * 1024 * 1024  # 5 MB per file
_BACKUPS = 3
_HANDLER_MARK = "_msa_desktop_unified"  # sentinel so shim + sidecar agree it's the same handler
_FORMAT = "%(asctime)s %(levelname)s %(name)s: %(message)s"


def unified_log_path(log_dir: Path | str) -> Path:
    return Path(log_dir) / LOG_NAME


def _existing_unified_handler(root: logging.Logger) -> logging.Handler | None:
    for handler in root.handlers:
        if getattr(handler, _HANDLER_MARK, False):
            return handler
    return None


def configure(
    log_dir: Path | str,
    *,
    level: int = logging.INFO,
    max_bytes: int = _MAX_BYTES,
    backups: int = _BACKUPS,
) -> Path:
    """Install the rotating ``msa-desktop.log`` handler on the root logger (idempotent) and
    return its path. Never raises — a logging-setup failure must not block launch."""
    path = unified_log_path(log_dir)
    root = logging.getLogger()
    if _existing_unified_handler(root) is not None:
        return path  # already configured — one rotating handle per file (LOG-001)
    try:
        Path(log_dir).mkdir(parents=True, exist_ok=True)
        handler = RotatingFileHandler(
            path, maxBytes=max_bytes, backupCount=backups, encoding="utf-8"
        )
    except OSError:
        return path  # can't open the log dir — degrade silently, keep launching
    handler.setFormatter(logging.Formatter(_FORMAT))
    setattr(handler, _HANDLER_MARK, True)
    root.addHandler(handler)
    if root.level == logging.NOTSET or root.level > level:
        root.setLevel(level)
    return path


def logger(name: str = "msa.desktop") -> logging.Logger:
    """The shim's application logger — records land in the unified file via the root handler."""
    return logging.getLogger(name)
