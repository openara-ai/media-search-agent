from __future__ import annotations

from pathlib import Path
from typing import Optional
import sys

from loguru import logger as LOG


_DEFAULT_FORMAT = (
	"<green>{time:YYYY-MM-DD HH:mm:ss.SSS}</green> | "
	"<level>{level: <8}</level> | "
	"<cyan>{name}</cyan>:<cyan>{function}</cyan>:<cyan>{line}</cyan> | "
	"<magenta>{extra}</magenta> - <level>{message}</level>"
)


def setup_logging(
	log_dir: Path | str = "logs",
	level: str = "INFO",
	to_file: bool = True,
	to_console: bool = True,
	rotation: str = "10 MB",
	retention: str = "7 days",
	serialize_json: bool = False,
) -> None:
	"""Configure Loguru logging for console and file outputs.

	Args:
		log_dir: Directory to place logs/; created if missing.
		level: Minimum log level (DEBUG/INFO/WARNING/ERROR)
		to_file: If True, also write to a rotating file sink.
		to_console: If True, write to console (stderr). Set to False to disable console output for better performance.
		rotation: File rotation condition (size/time, per Loguru syntax)
		retention: How long to keep rotated files
		serialize_json: If True, file logs are JSON serialized (machine-friendly)

	Usage:
		from msa_indexer.utils.logging import setup_logging
		setup_logging(cfg.log_dir, cfg.log_level, to_console=False)
	"""

	# Remove any existing handlers (important for notebooks/CLI re-runs)
	LOG.remove()

	# Console sink – colorful, human-friendly (only if enabled)
	if to_console:
		LOG.add(
			sys.stderr,
			level=level.upper(),
			colorize=sys.stderr.isatty(),
			backtrace=True,
			diagnose=False,
			format=_DEFAULT_FORMAT,
		)

	if to_file:
		log_dir = Path(log_dir)
		try:
			log_dir.mkdir(parents=True, exist_ok=True)
		except (OSError, PermissionError) as e:
			raise RuntimeError(f"Failed to create log directory '{log_dir}': {e}") from e
		
		logfile = log_dir / "msa.log"
		
		# Verify we can write to the log file location
		try:
			# Try to open the file for writing to verify permissions
			test_file = logfile.parent / f".{logfile.name}.test"
			test_file.touch()
			test_file.unlink()
		except (OSError, PermissionError) as e:
			raise RuntimeError(f"Cannot write to log file location '{logfile}': {e}") from e
		
		if serialize_json:
			try:
				LOG.add(
					logfile,
					level=level.upper(),
					rotation=rotation,
					retention=retention,
					enqueue=True,
					serialize=True,
					backtrace=True,
					diagnose=False,
				)
			except Exception as e:
				raise RuntimeError(f"Failed to add JSON log file handler for '{logfile}': {e}") from e
		else:
			try:
				LOG.add(
					logfile,
					level=level.upper(),
					rotation=rotation,
					retention=retention,
					enqueue=True,
					serialize=False,
					backtrace=True,
					diagnose=False,
					format=_DEFAULT_FORMAT,
				)
			except Exception as e:
				raise RuntimeError(f"Failed to add log file handler for '{logfile}': {e}") from e

	# A small extra: attach default structured fields so {extra} doesn't look empty
	# Bind a default context so {extra} has at least the app field
	LOG.bind(app="media-search-agent")


def with_ctx(**kwargs):
	"""Return a logger bound with extra context fields.

	Example:
		log = with_ctx(media_id=mid, path=str(p))
		log.info("Embedding created")
	"""
	return LOG.bind(**kwargs)

