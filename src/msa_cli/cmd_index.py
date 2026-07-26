import argparse
import os
import re
import shutil
import signal
import sys
import threading
import time
import warnings

warnings.filterwarnings("ignore", message="QuickGELU mismatch between final model config")

from pathlib import Path


_index_parser: argparse.ArgumentParser | None = None


def register(parent_sp: argparse._SubParsersAction) -> None:
    global _index_parser
    ap = parent_sp.add_parser(
        "index",
        help="Index media files, export to Qdrant",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  msa index run                                          # Run indexer with default config.yaml
  msa index run --config custom.yaml                    # Use custom config file
  msa index run --media-source-override /media/path     # Index specific directory
  msa index run --log-level DEBUG                       # Run with debug logging
  msa index run --no-console-log                        # Disable console output (log to file only)
  msa index run --dry-run                               # Scan files and show stats without processing
  msa index run --image-only                            # Process only images (skip videos)
  msa index run --video-only                            # Process only videos (skip images)
  msa index run --verify-content                        # Re-hash every file, repair fingerprints
  (msa index run --no-console-log </dev/null &) ; tail -f logs/msa.log  # Background + follow log
  msa index stop                                        # Ask the indexer to exit cleanly; wait up to 60s
  msa index stop --wait 120                             # Same; longer wait budget for slow files
  msa index stop --quiet                                # Suppress per-line progress
  msa index export                                      # Export indexed data to Qdrant
  msa index export --recreate                           # Export and recreate Qdrant collections
  msa index export --dry-run                            # Analyse export readiness without exporting
  msa index export --log-level WARNING                  # Export with warning-level logging
        """,
    )
    _index_parser = ap
    sp = ap.add_subparsers(dest="index_cmd", help="Available commands", metavar="COMMAND")

    # ── run ────────────────────────────────────────────────────────────────────
    run = sp.add_parser("run", help="Run the indexer once")
    run.add_argument("--media-source-override", required=False,
                     help="Path to media to index (CLI override; skips media_sources in config)")
    run.add_argument("--config", required=False, default=None,
                     help="YAML config file (default: ./config.yaml)")
    run.add_argument("--export-to-qdrant", action="store_true",
                     help="Force export to Qdrant after indexing, even if nothing changed")
    run.add_argument("--log-level",
                     choices=["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"],
                     default=None, help="Logging level (default: from config)")
    run.add_argument("--no-console-log", action="store_true",
                     help="Log to file only, not console (subprocess / background mode)")
    run.add_argument("--dry-run", action="store_true",
                     help="Scan files and show stats without processing (no ML, no DB writes)")
    run.add_argument("--verify-content", action="store_true",
                     help=(
                         "Bypass the fingerprint fast-path for this run: hash every "
                         "file's content and repair fingerprint records. Use as a "
                         "periodic reconcile if files may have changed without their "
                         "size or modification time changing (some in-place editors "
                         "and sync clients)."
                     ))

    media_type_group = run.add_mutually_exclusive_group()
    media_type_group.add_argument("--image-only", action="store_true",
                                  help="Process only images (skip videos)")
    media_type_group.add_argument("--video-only", action="store_true",
                                  help="Process only videos (skip images)")

    # --reprocess-* flags are intentionally hidden from --help: the underlying
    # stage-rerun paths exist but haven't been validated end-to-end, so we don't
    # want users discovering them and ending up with an inconsistent index.
    # Keep the entry points wired so internal/dev runs can still drive them.
    run.add_argument("--reprocess-gps", action="store_true", help=argparse.SUPPRESS)
    run.add_argument("--reprocess-objects", action="store_true", help=argparse.SUPPRESS)
    run.add_argument("--reprocess-faces", action="store_true", help=argparse.SUPPRESS)
    run.add_argument("--reprocess-embeddings", action="store_true", help=argparse.SUPPRESS)
    run.add_argument("--reprocess-all", action="store_true", help=argparse.SUPPRESS)

    # ── stop ───────────────────────────────────────────────────────────────────
    stop = sp.add_parser(
        "stop",
        help=(
            "Ask the running indexer to exit cleanly at its next per-file "
            "checkpoint, waiting with progress until it does"
        ),
    )
    stop.add_argument("--config", required=False, default=None,
                      help="YAML config file (default: ./config.yaml or $MSA_CONFIG_PATH)")
    stop.add_argument("--wait", type=float, default=60.0,
                      help="Max seconds to wait for clean exit (default: 60)")
    stop.add_argument("--quiet", action="store_true",
                      help="Suppress per-line progress output")
    stop.add_argument(
        "--require-running", action="store_true",
        help=(
            "Exit 1 if no indexer is currently running (default: 0, "
            "idempotent). Use this in CI/BVT where the absence of a "
            "running indexer means the test setup is wrong."
        ),
    )

    # ── export ─────────────────────────────────────────────────────────────────
    export = sp.add_parser("export", help="Export indexed data to Qdrant")
    export.add_argument("--config", required=False, default=None,
                        help="YAML config file (default: ./config.yaml)")
    export.add_argument("--recreate", action="store_true",
                        help=(
                            "Recreate Qdrant collections before exporting. "
                            "Also the blunt repair for dangling search entries "
                            "left by deletions that predate incremental "
                            "tracking — the routine delta export does not "
                            "retro-clean those."
                        ))
    export.add_argument("--dry-run", action="store_true",
                        help="Analyse export readiness without writing")
    export.add_argument("--log-level",
                        choices=["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"],
                        default=None, help="Logging level (default: from config)")

    # ── backup ─────────────────────────────────────────────────────────────────
    # Hidden from --help: backup path hasn't been validated end-to-end. Entry
    # point kept for internal/dev use.
    backup = sp.add_parser("backup", help=argparse.SUPPRESS)
    backup.add_argument("--config", required=False, default=None,
                        help="YAML config file (default: ./config.yaml)")
    backup.add_argument("--output-dir", required=False, default=None,
                        help="Backup destination directory (default: ./index_backups)")
    backup.add_argument("--log-level",
                        choices=["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"],
                        default=None, help="Logging level (default: from config)")



def handle(args: argparse.Namespace) -> None:
    if not args.index_cmd:
        if _index_parser:
            _index_parser.print_help()
        sys.exit(0)

    # Stop is a lightweight CLI helper — it asks the running indexer to exit
    # cleanly at its next per-file checkpoint, then waits with progress. It
    # writes the stop sentinel (and on POSIX also sends SIGTERM) but does
    # NOT initialise the indexer's heavy logging / model imports.
    if args.index_cmd == "stop":
        _handle_stop(args)
        return

    from loguru import logger
    from msa_settings import load_config
    from msa_indexer.pipeline import run_index, run_export, run_dry_run, run_export_dry_run
    from msa_indexer.utils.logging import setup_logging

    config_path = args.config or os.environ.get("MSA_CONFIG_PATH") or "config.yaml"
    cfg = load_config(config_path)

    # Override log level from CLI
    if getattr(args, "log_level", None):
        cfg.log_level = args.log_level

    # Initialise logging
    try:
        no_console_log = getattr(args, "no_console_log", False)
        if no_console_log:
            setup_logging(
                getattr(cfg, "log_dir", "logs"),
                getattr(cfg, "log_level", "INFO"),
                to_console=True,
                to_file=False,
            )
            console_status = "stderr-only (subprocess mode)"
        else:
            setup_logging(
                getattr(cfg, "log_dir", "logs"),
                getattr(cfg, "log_level", "INFO"),
                to_console=True,
                to_file=True,
            )
            console_status = "console+file"
        logger.info(
            f"Logging initialized level={getattr(cfg, 'log_level', 'INFO')} "
            f"dir={getattr(cfg, 'log_dir', 'logs')} mode={console_status}"
        )
    except Exception as e:
        import traceback
        print(f"FATAL: Failed to setup logging: {e}", file=sys.stderr)
        traceback.print_exc(file=sys.stderr)
        sys.exit(1)

    # Log CLI parameters
    cli_params = [f"command=index.{args.index_cmd}", f"config={config_path}"]
    if args.index_cmd == "run":
        if getattr(args, "media_source_override", None):
            cli_params.append(f"media_source_override={args.media_source_override}")
        if getattr(args, "export_to_qdrant", False):
            cli_params.append("export_to_qdrant=true")
        if getattr(args, "image_only", False):
            cli_params.append("image_only=true")
        if getattr(args, "video_only", False):
            cli_params.append("video_only=true")
        reprocess_flags = [
            flag for flag, attr in [
                ("gps", "reprocess_gps"), ("objects", "reprocess_objects"),
                ("faces", "reprocess_faces"), ("embeddings", "reprocess_embeddings"),
                ("all", "reprocess_all"),
            ] if getattr(args, attr, False)
        ]
        if reprocess_flags:
            cli_params.append(f"reprocess=[{', '.join(reprocess_flags)}]")
    elif args.index_cmd == "export":
        if getattr(args, "recreate", False):
            cli_params.append("recreate=true")
    elif args.index_cmd == "backup":
        if getattr(args, "output_dir", None):
            cli_params.append(f"output_dir={args.output_dir}")
    if getattr(args, "log_level", None):
        cli_params.append(f"log_level={args.log_level}")
    logger.info(f"CLI parameters: {' '.join(cli_params)}")

    # ── Dispatch ───────────────────────────────────────────────────────────────

    if args.index_cmd == "run":
        if getattr(args, "media_source_override", None):
            cfg.media_source_override = Path(args.media_source_override)
            setattr(cfg, "_cli_media_override", True)

        cfg.reprocess_gps = args.reprocess_gps or args.reprocess_all
        cfg.reprocess_objects = args.reprocess_objects or args.reprocess_all
        cfg.reprocess_faces = args.reprocess_faces or args.reprocess_all
        cfg.reprocess_embeddings = args.reprocess_embeddings or args.reprocess_all
        cfg.export_to_qdrant = getattr(args, "export_to_qdrant", False)
        cfg.image_only = getattr(args, "image_only", False)
        cfg.video_only = getattr(args, "video_only", False)
        # CLI-only reconcile mode (like the reprocess flags): hash every file
        # this run, repairing fingerprint state — the safety net for the
        # size+mtime blind spot (M-8 plan §3.4).
        cfg.verify_content = getattr(args, "verify_content", False)

        if getattr(args, "dry_run", False):
            run_dry_run(cfg)
        else:
            from msa_settings import acquire_instance_lock, release_instance_lock
            lock_path = Path(cfg.index_dir).parent / "msa-indexer.lock"
            acquire_instance_lock(lock_path, "Media Search Agent Indexer")

            # Resolve where the PID file *will* live (matches indexer_manager
            # convention). Don't write it yet — see below.
            msa_log_dir_env = os.environ.get("MSA_LOG_DIR")
            if msa_log_dir_env:
                _run_dir_for_stop = Path(msa_log_dir_env) / "run"
            else:
                _run_dir_for_stop = Path.cwd() / "run"
            _pid_path_for_stop: "Path | None" = None

            # Default MSA_INDEXER_STOP_FILE so the watcher thread engages.
            # The API sets this when it spawns us; standalone runs need a
            # default so `msa index stop`'s sentinel write is visible.
            # Must be set BEFORE the watcher thread reads it.
            _stop_file_defaulted_here = False
            if not os.environ.get("MSA_INDEXER_STOP_FILE"):
                os.environ["MSA_INDEXER_STOP_FILE"] = str(
                    _run_dir_for_stop / "indexer.stop"
                )
                _stop_file_defaulted_here = True

            # Clear any stale stop sentinel — ONLY in the standalone path
            # where we just defaulted the env var. When the API parent
            # provided MSA_INDEXER_STOP_FILE explicitly, trust them: they
            # already cleared stale state before Popen
            # (IndexerManager.start, test_start_clears_stale_stop_sentinel),
            # so anything in the sentinel file *now* is a legitimate stop
            # request that landed in the parent→Popen→child-init race
            # window. Unlinking it here would erase a real stop request —
            # particularly bad on Windows where there's no SIGTERM
            # fallback and the stop would hang until --wait timeout.
            if _stop_file_defaulted_here:
                try:
                    Path(os.environ["MSA_INDEXER_STOP_FILE"]).unlink(missing_ok=True)
                except OSError:
                    pass

            try:
                stop_event = threading.Event()

                def _handle_stop_signal(signum, frame):
                    logger.warning(
                        "Stop signal received (signum={}) — finishing current file then stopping cleanly",
                        signum,
                    )
                    stop_event.set()

                signal.signal(signal.SIGTERM, _handle_stop_signal)
                # SIGBREAK is the Python delivery name for Windows CTRL_BREAK_EVENT.
                # In practice the API now uses a sentinel file (see below) because
                # Intel Fortran runtime preempts SIGBREAK with its own handler, but
                # we register this anyway for any external caller (taskkill, etc.)
                # that might still send it.
                if hasattr(signal, "SIGBREAK"):
                    signal.signal(signal.SIGBREAK, _handle_stop_signal)

                # Cooperative-stop sentinel file. The API writes this to ask the
                # indexer to shut down cleanly without signals (the Windows-safe
                # path — see indexer_manager.stop() for the rationale).
                stop_file_env = os.getenv("MSA_INDEXER_STOP_FILE")
                if stop_file_env:
                    _stop_file = Path(stop_file_env)

                    def _watch_stop_file():
                        import time
                        # On Windows the sentinel is the primary stop mechanism,
                        # so a silently-swallowed exception here means stops just
                        # don't work and the operator has no diagnostic. Log the
                        # first occurrence at WARNING and suppress repeats so we
                        # don't flood msa.log if the failure is persistent.
                        logged_error = False
                        while not stop_event.is_set():
                            try:
                                if _stop_file.exists():
                                    logger.warning(
                                        "Stop sentinel detected ({}) — finishing current file then stopping cleanly",
                                        _stop_file,
                                    )
                                    stop_event.set()
                                    return
                            except Exception as exc:
                                if not logged_error:
                                    logger.warning(
                                        "Stop-sentinel watcher could not stat {} ({}: {}); "
                                        "stop requests may be ignored. Further errors suppressed.",
                                        _stop_file, type(exc).__name__, exc,
                                    )
                                    logged_error = True
                            time.sleep(0.25)

                    threading.Thread(target=_watch_stop_file, daemon=True).start()

                # Publish our PID *now* — after the SIGTERM handler is
                # installed and the sentinel watcher is running. Any caller
                # seeing this PID is guaranteed to find both delivery paths
                # ready: SIGTERM lands on _handle_stop_signal (not Python's
                # default abort), sentinel writes land on the running
                # watcher. Mirrors IndexerManager.start()'s ordering.
                _pid_path = _run_dir_for_stop / "indexer.pid"
                try:
                    _run_dir_for_stop.mkdir(parents=True, exist_ok=True)
                    _pid_path.write_text(str(os.getpid()))
                    _pid_path_for_stop = _pid_path
                except OSError as exc:
                    logger.warning(
                        "Could not write indexer PID file {}: {} — `msa index stop` "
                        "will not be able to find this run.",
                        _pid_path, exc,
                    )

                run_index(cfg, stop_event=stop_event)
            except Exception:
                logger.opt(exception=True).critical("Unhandled exception — indexer aborting")
                sys.exit(1)
            finally:
                # Clean up our own PID + sentinel files BEFORE releasing the
                # instance lock. Otherwise a quick-restart `msa index run`
                # could acquire the lock between our release_instance_lock
                # and our unlink calls, write its own PID/sentinel, and have
                # us delete the NEW files. Under rapid restart, `msa index
                # stop` would then fail to find the new run.
                if _pid_path_for_stop is not None:
                    try:
                        _pid_path_for_stop.unlink(missing_ok=True)
                    except OSError:
                        pass
                # Sentinel cleanup: if `msa index stop` wrote it but we're
                # exiting on a path that didn't unlink it (timeout, crash
                # post-write), don't leave it behind for the next run.
                _stop_path_for_cleanup = os.environ.get("MSA_INDEXER_STOP_FILE")
                if _stop_path_for_cleanup:
                    try:
                        Path(_stop_path_for_cleanup).unlink(missing_ok=True)
                    except OSError:
                        pass
                release_instance_lock(lock_path)

    elif args.index_cmd == "export":
        try:
            if getattr(args, "dry_run", False):
                run_export_dry_run(cfg)
            else:
                cfg.export_recreate = getattr(args, "recreate", False)
                run_export(cfg)
        except Exception:
            logger.opt(exception=True).critical("Unhandled exception — export aborting")
            sys.exit(1)

    elif args.index_cmd == "backup":
        output_dir = Path(args.output_dir) if getattr(args, "output_dir", None) else Path("index_backups")
        _run_backup(cfg, output_dir)


def _run_backup(config, output_dir: Path) -> None:
    from datetime import datetime
    from loguru import logger

    def _fmt(size_bytes: int) -> str:
        if size_bytes >= 1024 ** 3:
            return f"{size_bytes / 1024 ** 3:.2f}GB"
        if size_bytes >= 1024 ** 2:
            return f"{size_bytes / 1024 ** 2:.2f}MB"
        if size_bytes >= 1024:
            return f"{size_bytes / 1024:.2f}KB"
        return f"{size_bytes}B"

    logger.info("Starting backup of SQLite and FAISS files...")
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    backup_dir = output_dir / f"index_backup_{timestamp}"
    backup_dir.mkdir(parents=True, exist_ok=True)
    logger.info(f"Backup directory: {backup_dir}")

    total = 0

    def _copy(src: Path, label: str) -> None:
        nonlocal total
        if not src.exists():
            logger.warning(f"{label} not found at {src}, skipping...")
            return
        dst = backup_dir / src.name
        shutil.copy2(src, dst)
        size = dst.stat().st_size
        total += size
        logger.info(f"✓ Backed up {label}: {src} → {dst} ({_fmt(size)})")

    _copy(Path(config.sqlite_path), "SQLite")
    faiss = Path(config.faiss_path)
    _copy(faiss, "FAISS")
    _copy(Path(str(faiss) + ".ids"), "FAISS IDs")
    _copy(Path(str(faiss) + ".vecs.npy"), "FAISS vectors")
    face_faiss = Path(getattr(config, "face_faiss_path", "index/face_vec.faiss"))
    _copy(face_faiss, "face FAISS")
    _copy(Path(str(face_faiss) + ".ids"), "face FAISS IDs")
    _copy(Path(str(face_faiss) + ".vecs.npy"), "face FAISS vectors")

    logger.info(f"✓ Backup complete: {backup_dir} (total: {_fmt(total)})")
    logger.info(f"  To restore, copy files from {backup_dir} back to their original locations")


# ── stop ─────────────────────────────────────────────────────────────────────
#
# `msa index stop` asks the indexer subprocess to exit *cleanly* at its next
# per-file checkpoint (it finishes the current file, commits its batch, and
# returns rc=0 — as opposed to a SIGKILL / forced termination that would
# leave SQLite WAL mid-write and skip the Qdrant export). This clean-exit
# behaviour is what the rest of the code calls a "cooperative stop".
#
# The indexer subprocess (cmd_index.py:_handle_stop_signal + _watch_stop_file)
# installs two ways to receive the request:
#
#   1. POSIX signal — SIGTERM handler sets stop_event. Fast and immediate.
#      Doesn't work on Windows: Intel Fortran's SetConsoleCtrlHandler aborts
#      the process on CTRL_BREAK/CTRL_C before Python's handler can run
#      (see WIN-006 in BUGS_AND_GOTCHAS.md).
#   2. Stop sentinel file (<run_dir>/indexer.stop) — daemon watcher thread
#      polls for it every 250 ms and sets stop_event when it appears. Works
#      everywhere. It's the *only* path that works on Windows.
#
# Both mechanisms drive the same stop_event. `msa index stop` uses both:
# it always writes the sentinel, and on POSIX it *also* sends SIGTERM for an
# instant wakeup. On Windows it deliberately does NOT send a signal.
#
# Kept out of the main handle() flow because it must NOT initialise the heavy
# ML / loguru stack — it's a lightweight signal+wait helper.

_STOP_ANSI_ESCAPE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")


# Windows: cache the kernel32 handle + function signatures at module scope so
# the 0.5s _handle_stop polling loop doesn't redeclare ctypes argtypes/restype
# on every call. Mirrors the IndexerManager._pid_alive pattern (PR #124).
# Use a private WinDLL (not the shared windll.kernel32 proxy) so signature
# overrides don't leak to other code in the process.
if sys.platform == "win32":
    import ctypes as _ctypes_win
    from ctypes import wintypes as _wintypes_win

    _STOP_PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    _STOP_STILL_ACTIVE = 259
    _STOP_KERNEL32 = _ctypes_win.WinDLL("kernel32", use_last_error=True)
    _STOP_KERNEL32.OpenProcess.argtypes = [_wintypes_win.DWORD, _wintypes_win.BOOL, _wintypes_win.DWORD]
    _STOP_KERNEL32.OpenProcess.restype = _wintypes_win.HANDLE
    _STOP_KERNEL32.GetExitCodeProcess.argtypes = [_wintypes_win.HANDLE, _ctypes_win.POINTER(_wintypes_win.DWORD)]
    _STOP_KERNEL32.GetExitCodeProcess.restype = _wintypes_win.BOOL
    _STOP_KERNEL32.CloseHandle.argtypes = [_wintypes_win.HANDLE]
    _STOP_KERNEL32.CloseHandle.restype = _wintypes_win.BOOL


def _resolve_stop_paths(config_path_arg) -> "tuple[Path, Path, Path, Path]":
    """Resolve (run_dir, pid_file, stop_file, msa_log) the same way
    IndexerManager does. MSA_LOG_DIR env is the source of truth for run_dir;
    msa.log lives under cfg.log_dir from the config file.
    """
    config_path = config_path_arg or os.environ.get("MSA_CONFIG_PATH") or "config.yaml"
    try:
        from msa_settings import load_config
        cfg = load_config(config_path)
        log_dir = Path(getattr(cfg, "log_dir", "logs"))
    except Exception:
        log_dir = Path("logs")
    msa_log_dir = os.environ.get("MSA_LOG_DIR")
    if msa_log_dir:
        run_dir = Path(msa_log_dir) / "run"
    else:
        run_dir = Path.cwd() / "run"
    return run_dir, run_dir / "indexer.pid", run_dir / "indexer.stop", log_dir / "msa.log"


def _stop_pid_is_indexer(pid: int) -> bool:
    """Return True only if the cmdline of `pid` looks like the MSA indexer.

    Guard against signaling the wrong process after a crash: if `msa index run`
    crashed without unlinking `indexer.pid` and the kernel later reused that
    PID for an unrelated process, `_stop_pid_alive(pid)` would happily report
    "alive" and we'd send SIGTERM to whatever is now running there. Mirrors
    IndexerManager._pid_is_indexer; duplicated here to keep the
    msa_cli → msa_apps.search_api dependency direction one-way.

    Fail-open: if we can't determine the cmdline (e.g., no /proc, ps missing,
    permission denied), return True so we don't refuse to stop a legitimate
    indexer just because identity verification is inconclusive. The PID was
    already shown to be alive by _stop_pid_alive — this check only filters
    out the "someone else inherited our PID" failure mode.
    """
    import platform as _platform
    import subprocess as _subprocess
    try:
        if _platform.system() == "Linux":
            with open(f"/proc/{pid}/cmdline", "rb") as f:
                cmdline = f.read().replace(b"\x00", b" ").decode(errors="replace")
        elif _platform.system() == "Windows":
            result = _subprocess.run(
                ["powershell", "-NoProfile", "-NonInteractive", "-Command",
                 f"(Get-CimInstance Win32_Process -Filter 'ProcessId={pid}').CommandLine"],
                capture_output=True, text=True, timeout=5,
            )
            cmdline = result.stdout
        else:
            # macOS / other POSIX without /proc.
            result = _subprocess.run(
                ["ps", "-p", str(pid), "-o", "args="],
                capture_output=True, text=True, timeout=2,
            )
            cmdline = result.stdout
        return (
            ("msa" in cmdline and "index" in cmdline and "run" in cmdline)
            or "msa_indexer" in cmdline
            or "msa-index" in cmdline
            or "msa_cli" in cmdline
        )
    except FileNotFoundError:
        return True
    except Exception:
        return True


def _stop_pid_alive(pid: int) -> bool:
    """Cross-platform PID liveness. Mirrors IndexerManager._pid_alive
    (PR #124, WIN-002): POSIX uses os.kill(pid, 0); Windows uses
    OpenProcess + GetExitCodeProcess because os.kill(pid, 0) raises
    SystemError for dead PIDs on Windows.

    Windows: uses the module-level cached _STOP_KERNEL32 handle so the
    0.5s _handle_stop polling loop doesn't repeatedly declare ctypes
    signatures.

    Duplicated here (rather than imported) because msa_cli must not depend
    on msa_apps.search_api.
    """
    if sys.platform != "win32":
        try:
            os.kill(pid, 0)
            return True
        except (ProcessLookupError, PermissionError):
            return False
        except OSError:
            return False
    handle = _STOP_KERNEL32.OpenProcess(
        _STOP_PROCESS_QUERY_LIMITED_INFORMATION, False, pid
    )
    if not handle:
        return False
    try:
        exit_code = _wintypes_win.DWORD()
        if not _STOP_KERNEL32.GetExitCodeProcess(handle, _ctypes_win.byref(exit_code)):
            return False
        return exit_code.value == _STOP_STILL_ACTIVE
    finally:
        _STOP_KERNEL32.CloseHandle(handle)


def _stop_tail_log(log_path: Path, offset: int) -> "tuple[list[str], int]":
    """Read new content from log_path since offset, strip ANSI + loguru prefix,
    return (lines, new_offset). Lines are the user-readable message portion
    only — the timestamp/level prefix is stripped to keep stop output tight.
    """
    try:
        with open(log_path, "r", errors="replace") as f:
            f.seek(offset)
            content = f.read()
            new_offset = f.tell()
    except OSError:
        return [], offset
    content = _STOP_ANSI_ESCAPE.sub("", content)
    cleaned: list[str] = []
    for raw in content.splitlines():
        line = raw.rstrip()
        if not line.strip():
            continue
        if " - " in line and "|" in line:
            cleaned.append(line.split(" - ", 1)[1])
        else:
            cleaned.append(line)
    return cleaned, new_offset


def _safe_unlink(path: Path) -> None:
    try:
        path.unlink(missing_ok=True)
    except OSError:
        pass


def _handle_stop(args: argparse.Namespace) -> None:
    run_dir, pid_file, stop_file, msa_log = _resolve_stop_paths(
        getattr(args, "config", None)
    )
    quiet = bool(getattr(args, "quiet", False))
    require_running = bool(getattr(args, "require_running", False))

    if not pid_file.exists():
        print(f"No indexer is currently running. (No PID file at {pid_file})")
        # In --require-running mode (used by BVT), absence of a running
        # indexer is a hard failure — not an idempotent no-op.
        if require_running:
            sys.exit(1)
        return

    try:
        pid = int(pid_file.read_text().strip())
    except (ValueError, OSError) as exc:
        print(f"Could not read PID file {pid_file}: {exc}", file=sys.stderr)
        sys.exit(1)

    if not _stop_pid_alive(pid):
        print(f"No indexer is currently running. (Stale PID {pid} from {pid_file})")
        _safe_unlink(pid_file)
        if require_running:
            sys.exit(1)
        return

    # Identity check: the PID is alive, but is it actually an indexer? If
    # `msa index run` crashed without unlinking its PID file and the kernel
    # later reused that PID for an unrelated process, signaling it would
    # terminate the wrong thing. Treat as stale and clean up.
    if not _stop_pid_is_indexer(pid):
        print(
            f"No indexer is currently running. (PID {pid} from {pid_file} "
            f"is alive but its cmdline does not match `msa index run` — "
            f"the indexer crashed and that PID has been reused.)"
        )
        _safe_unlink(pid_file)
        if require_running:
            sys.exit(1)
        return

    # Write the stop sentinel. The indexer's daemon watcher polls this path
    # every ~250ms and sets stop_event when it appears, which triggers the
    # cooperative exit at the next per-file checkpoint. This is the only
    # delivery mechanism that works on Windows.
    try:
        run_dir.mkdir(parents=True, exist_ok=True)
        stop_file.write_text(str(pid))
    except OSError as exc:
        print(f"Could not write stop sentinel {stop_file}: {exc}", file=sys.stderr)
        sys.exit(1)

    if not quiet:
        print(f"Stop requested for indexer (PID {pid}).")
        print("Waiting for clean exit at next checkpoint… (current file must finish)")

    # POSIX fast path: SIGTERM to the indexer PID fires the indexer's SIGTERM
    # handler immediately, which sets the same stop_event the sentinel watcher
    # would have set ~250ms later. Belt-and-suspenders with the sentinel write
    # above.
    #
    # Note: we deliberately use os.kill(pid, ...) and NOT os.killpg(getpgid(pid),
    # ...). A standalone `msa index run &` launched from a shell normally shares
    # that shell's process group, and killpg would fan SIGTERM out to the
    # calling shell and any sibling processes — including the caller of
    # `msa index stop` itself. The indexer doesn't have long-running children
    # that need fan-out signaling, so PID-targeted SIGTERM is sufficient and
    # safe in every caller context.
    #
    # Windows: do NOT send CTRL_BREAK_EVENT — Intel Fortran runtime's console-
    # control handler aborts the process with forrtl: error (200) before
    # Python can shut down cleanly. The sentinel is the only delivery path
    # on Windows. See WIN-006 in BUGS_AND_GOTCHAS.md.
    if sys.platform != "win32":
        try:
            os.kill(pid, signal.SIGTERM)
        except (ProcessLookupError, OSError):
            pass

    wait_seconds = float(getattr(args, "wait", 60.0))
    start = time.monotonic()
    try:
        log_offset = msa_log.stat().st_size if msa_log.exists() else 0
    except OSError:
        log_offset = 0

    while True:
        elapsed = time.monotonic() - start
        if not _stop_pid_alive(pid):
            if not quiet:
                print(f"Indexer stopped cleanly in {elapsed:.1f} seconds.")
            _safe_unlink(stop_file)
            return

        if elapsed >= wait_seconds:
            print(
                f"[{elapsed:5.1f}s] Indexer did not exit within {wait_seconds:.0f}s — "
                f"it may be processing a slow file."
            )
            print(
                f"The stop request is still pending; the indexer will exit at its next "
                f"per-file checkpoint."
            )
            print(
                f"Re-run `msa index stop --wait {int(wait_seconds * 2)}` to keep waiting, "
                f"or inspect {msa_log}."
            )
            sys.exit(1)

        if not quiet:
            new_lines, log_offset = _stop_tail_log(msa_log, log_offset)
            if new_lines:
                print(f"[{elapsed:5.1f}s] {new_lines[-1]}")

        time.sleep(0.5)
