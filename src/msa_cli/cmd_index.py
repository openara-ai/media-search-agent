import argparse
import os
import shutil
import signal
import sys
import threading
import warnings

warnings.filterwarnings("ignore", message="QuickGELU mismatch between final model config")

from pathlib import Path


_index_parser: argparse.ArgumentParser | None = None


def register(parent_sp: argparse._SubParsersAction) -> None:
    global _index_parser
    ap = parent_sp.add_parser(
        "index",
        help="Index media files, export to Qdrant, create backups",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  msa index run                                          # Run indexer with default config.yaml
  msa index run --config custom.yaml                    # Use custom config file
  msa index run --media-source-override /media/path     # Index specific directory
  msa index run --reprocess-faces                       # Re-run face detection only
  msa index run --reprocess-all                         # Re-run all processing stages
  msa index run --log-level DEBUG                       # Run with debug logging
  msa index run --no-console-log                        # Disable console output (log to file only)
  msa index run --dry-run                               # Scan files and show stats without processing
  msa index run --image-only                            # Process only images (skip videos)
  msa index run --video-only                            # Process only videos (skip images)
  (msa index run --no-console-log </dev/null &) ; tail -f logs/msa.log  # Background + follow log
  msa index export                                      # Export indexed data to Qdrant
  msa index export --recreate                           # Export and recreate Qdrant collections
  msa index export --dry-run                            # Analyse export readiness without exporting
  msa index export --log-level WARNING                  # Export with warning-level logging
  msa index backup                                      # Backup SQLite and FAISS to ./index_backups
  msa index backup --output-dir /path/to/backups        # Backup to custom directory
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

    media_type_group = run.add_mutually_exclusive_group()
    media_type_group.add_argument("--image-only", action="store_true",
                                  help="Process only images (skip videos)")
    media_type_group.add_argument("--video-only", action="store_true",
                                  help="Process only videos (skip images)")

    run.add_argument("--reprocess-gps", action="store_true",
                     help="Force re-extract GPS metadata for all media")
    run.add_argument("--reprocess-objects", action="store_true",
                     help="Force re-run object detection for all media")
    run.add_argument("--reprocess-faces", action="store_true",
                     help="Force re-run face detection for all media")
    run.add_argument("--reprocess-embeddings", action="store_true",
                     help="Force re-compute CLIP embeddings for all media")
    run.add_argument("--reprocess-all", action="store_true",
                     help="Force reprocess all stages (GPS, objects, faces, embeddings)")

    # ── export ─────────────────────────────────────────────────────────────────
    export = sp.add_parser("export", help="Export indexed data to Qdrant")
    export.add_argument("--config", required=False, default=None,
                        help="YAML config file (default: ./config.yaml)")
    export.add_argument("--recreate", action="store_true",
                        help="Recreate Qdrant collections before exporting")
    export.add_argument("--dry-run", action="store_true",
                        help="Analyse export readiness without writing")
    export.add_argument("--log-level",
                        choices=["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"],
                        default=None, help="Logging level (default: from config)")

    # ── backup ─────────────────────────────────────────────────────────────────
    backup = sp.add_parser("backup", help="Backup SQLite database and FAISS indexes")
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

        if getattr(args, "dry_run", False):
            run_dry_run(cfg)
        else:
            from msa_settings import acquire_instance_lock, release_instance_lock
            lock_path = Path(cfg.index_dir).parent / "msa-indexer.lock"
            acquire_instance_lock(lock_path, "Media Search Agent Indexer")
            try:
                stop_event = threading.Event()

                def _handle_sigterm(signum, frame):
                    logger.warning("SIGTERM received — finishing current file then stopping cleanly")
                    stop_event.set()

                signal.signal(signal.SIGTERM, _handle_sigterm)
                run_index(cfg, stop_event=stop_event)
            except Exception:
                logger.opt(exception=True).critical("Unhandled exception — indexer aborting")
                sys.exit(1)
            finally:
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
