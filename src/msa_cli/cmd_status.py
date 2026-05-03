import json
import os
import sqlite3
import subprocess
import sys
import urllib.request
from pathlib import Path

from . import cmd_api as _cmd_api

_pid_alive = _cmd_api._pid_alive
_pids_on_port = _cmd_api._pids_on_port
_pid_is_api_server = _cmd_api._pid_is_api_server


def register(parent_sp) -> None:
    ap = parent_sp.add_parser(
        "status",
        help="Show install, service, and index status",
        description="Show a full status snapshot: install paths, running processes, and index counts.",
    )
    ap.add_argument("--json", action="store_true", dest="json_output",
                    help="Output machine-readable JSON")
    ap.add_argument("--config", default=None,
                    help="Config file (default: MSA_CONFIG_PATH env or config.yaml)")


def handle(args) -> None:
    info = _collect(args)
    if getattr(args, "json_output", False):
        print(json.dumps(info, indent=2, default=str))
    else:
        _print_human(info)


# ── Data collection ────────────────────────────────────────────────────────────

def _collect(args) -> dict:
    config_path = (
        getattr(args, "config", None)
        or os.environ.get("MSA_CONFIG_PATH")
        or "config.yaml"
    )

    cfg = None
    try:
        from msa_settings import load_config
        cfg = load_config(config_path)
    except Exception:
        pass

    return {
        "install": _install_info(cfg, config_path),
        "service": _service_info(cfg),
        "index":   _index_info(cfg),
    }


def _install_info(cfg, config_path: str) -> dict:
    info: dict = {}

    msa_root = os.environ.get("MSA_ROOT") or (
        str(cfg.msa_root) if cfg and hasattr(cfg, "msa_root") else None
    )
    if msa_root:
        info["root"] = msa_root

    info["config"] = config_path

    log_dir = (
        os.environ.get("MSA_LOG_DIR")
        or (str(cfg.log_dir) if cfg and hasattr(cfg, "log_dir") else None)
    )
    if log_dir:
        info["log_dir"] = log_dir

    venv_dir = os.environ.get("MSA_VENV_DIR") or str(Path(sys.executable).parent.parent)
    info["venv"] = venv_dir

    if log_dir:
        latest = _latest_installer_log(Path(log_dir))
        if latest:
            info["installer_log"] = str(latest)

    try:
        from importlib.metadata import version
        info["version"] = version("media-search-agent")
    except Exception:
        try:
            from importlib.metadata import version
            info["version"] = version("msa")
        except Exception:
            info["version"] = "unknown"

    # macOS: detect which installer laid down this install
    if sys.platform == "darwin":
        if msa_root and msa_root.startswith("/Applications/"):
            info["installer"] = "pkg"
        elif msa_root and "/Applications/" in msa_root:
            info["installer"] = "shell"
        else:
            info["installer"] = "dev"

    return info


def _service_info(cfg) -> dict:
    port = 8000
    if cfg:
        try:
            port = int(cfg.api.port)
        except Exception:
            pass

    info: dict = {"port": port}

    # PID file
    log_dir = os.environ.get("MSA_LOG_DIR") or (
        str(cfg.log_dir) if cfg and hasattr(cfg, "log_dir") else None
    )
    pid_file = Path(log_dir) / "run" / "uvicorn.pid" if log_dir else None
    uvicorn_pid = None
    if pid_file and pid_file.exists():
        try:
            uvicorn_pid = int(pid_file.read_text().strip())
            if not _pid_alive(uvicorn_pid):
                uvicorn_pid = None
                info["stale_pid_file"] = str(pid_file)
        except Exception:
            pass

    if uvicorn_pid:
        info["uvicorn_pid"] = uvicorn_pid
        info["running"] = True
    else:
        # Fallback: scan port, but validate it's our API server to avoid
        # misreporting a port conflict as "API running".
        port_pids = _pids_on_port(port)
        api_pids = [p for p in port_pids if _pid_is_api_server(p)]
        if api_pids:
            info["uvicorn_pid"] = api_pids[0]
            info["running"] = True
        else:
            info["running"] = False
            if port_pids:
                info["port_conflict_pid"] = port_pids[0]

    if info["running"]:
        info["url"] = f"http://localhost:{port}"
        info["health"] = _health_check(port)

    # Indexer: check both API-managed PID file and CLI lock file
    indexer_pid = None
    indexer_source = None
    if pid_file:
        indexer_pid_file = pid_file.parent / "indexer.pid"
        indexer_pid, indexer_source = _read_live_pid(indexer_pid_file, "api")
    if indexer_pid is None and cfg:
        try:
            cli_lock = Path(cfg.index_dir).parent / "msa-indexer.lock"
            indexer_pid, indexer_source = _read_live_pid(cli_lock, "cli")
        except Exception:
            pass
    if indexer_pid is not None:
        info["indexer_pid"] = indexer_pid
        info["indexer_running"] = True
        info["indexer_source"] = indexer_source
    else:
        info["indexer_running"] = False

    # macOS: is the menu bar app running?
    if sys.platform == "darwin":
        app_pid = _macos_app_pid()
        if app_pid:
            info["menu_bar_app_pid"] = app_pid

    # Log files
    if log_dir:
        log_path = Path(log_dir)
        for name, filename in [("uvicorn_log", "uvicorn.log"), ("app_log", "msa.log")]:
            p = log_path / filename
            if p.exists():
                info[name] = str(p)

    return info


def _index_info(cfg) -> dict:
    if not cfg:
        return {"error": "config not loaded"}

    sqlite_path = getattr(cfg, "sqlite_path", None)
    if not sqlite_path or not Path(str(sqlite_path)).exists():
        return {"error": f"index not found at {sqlite_path}"}

    try:
        conn = sqlite3.connect(str(sqlite_path), timeout=3)
        cur = conn.cursor()
        images = cur.execute(
            "SELECT COUNT(*) FROM media WHERE mime LIKE 'image/%'"
            " AND (deleted IS NULL OR deleted = 0)"
        ).fetchone()[0]
        videos = cur.execute(
            "SELECT COUNT(*) FROM media WHERE mime LIKE 'video/%'"
            " AND (deleted IS NULL OR deleted = 0)"
        ).fetchone()[0]
        video_secs = cur.execute(
            "SELECT COALESCE(SUM(duration), 0) FROM media WHERE mime LIKE 'video/%'"
            " AND (deleted IS NULL OR deleted = 0)"
        ).fetchone()[0]
        faces = cur.execute("SELECT COUNT(*) FROM face").fetchone()[0]
        people = cur.execute(
            "SELECT COUNT(*) FROM person WHERE is_labeled = 1"
        ).fetchone()[0]
        last_at = cur.execute("SELECT MAX(added_at) FROM media").fetchone()[0]
        conn.close()
        return {
            "images": images,
            "videos": videos,
            "total_media": images + videos,
            "video_duration_hours": round(video_secs / 3600, 1) if video_secs else 0,
            "faces": faces,
            "labeled_people": people,
            "last_indexed_at": last_at,
            "sqlite": str(sqlite_path),
        }
    except Exception as exc:
        return {"error": str(exc)}


# ── Helpers ────────────────────────────────────────────────────────────────────

def _latest_installer_log(log_dir: Path) -> Path | None:
    """Return the most recent install/repair/upgrade log, or None."""
    candidates = []
    for prefix in ("install", "repair", "upgrade"):
        candidates.extend(log_dir.glob(f"{prefix}-*.log"))
    return max(candidates, key=lambda p: p.stat().st_mtime) if candidates else None


def _read_live_pid(pid_file: Path, source: str) -> tuple:
    """Read a PID file and return (pid, source) if the process is alive, else (None, None)."""
    try:
        pid = int(pid_file.read_text().strip())
        if _pid_alive(pid):
            return pid, source
    except Exception:
        pass
    return None, None


def _health_check(port: int) -> str:
    try:
        resp = urllib.request.urlopen(f"http://localhost:{port}/health", timeout=2)
        if resp.status == 200:
            import json as _json
            body = _json.loads(resp.read())
            return body.get("status", "ok")
    except Exception:
        pass
    return "unreachable"


def _macos_app_pid() -> int | None:
    try:
        r = subprocess.run(
            ["pgrep", "-x", "MediaSearchAgent"],
            capture_output=True, text=True, timeout=2,
        )
        pids = [int(p) for p in r.stdout.split() if p.strip().isdigit()]
        return pids[0] if pids else None
    except Exception:
        return None


# ── Human-readable output ──────────────────────────────────────────────────────

def _print_human(info: dict) -> None:
    W = 36

    def row(label, value):
        print(f"  {label:<{W}} {value}")

    def section(title):
        print(f"\n{title}")
        print("  " + "─" * (W + 20))

    # Install
    section("Install")
    inst = info.get("install", {})
    if "version" in inst:
        row("Version", inst["version"])
    if "installer" in inst:
        row("Installer", inst["installer"])
    if "root" in inst:
        row("Root", inst["root"])
    row("Config", inst.get("config", "—"))
    if "log_dir" in inst:
        row("Logs", inst["log_dir"])
    if "venv" in inst:
        row("Venv", inst["venv"])
    if "installer_log" in inst:
        row("Installer log", inst["installer_log"])

    # Service
    section("Service")
    svc = info.get("service", {})
    running = svc.get("running", False)
    status_str = "running" if running else "stopped"
    if running:
        pid = svc.get("uvicorn_pid", "")
        health = svc.get("health", "")
        status_str += f"  (PID {pid}, health: {health})"
    row("API", status_str)
    if "port_conflict_pid" in svc:
        row("Port conflict", f"PID {svc['port_conflict_pid']} is holding port {svc['port']}")
    if running:
        row("URL", svc.get("url", ""))
    if svc.get("indexer_running"):
        source = svc.get("indexer_source", "")
        source_tag = f", via {source}" if source else ""
        row("Indexer", f"running  (PID {svc['indexer_pid']}{source_tag})")
    else:
        row("Indexer", "not running")
    if "menu_bar_app_pid" in svc:
        row("Menu bar app", f"running  (PID {svc['menu_bar_app_pid']})")
    elif sys.platform == "darwin":
        row("Menu bar app", "not running")
    if "stale_pid_file" in svc:
        row("Stale PID file", svc["stale_pid_file"])
    if "uvicorn_log" in svc:
        row("Uvicorn log", svc["uvicorn_log"])
    if "app_log" in svc:
        row("App log", svc["app_log"])

    # Index
    section("Index")
    idx = info.get("index", {})
    if "error" in idx:
        row("Error", idx["error"])
    else:
        row("Total media", f"{idx.get('total_media', 0):,}  "
            f"({idx.get('images', 0):,} images, {idx.get('videos', 0):,} videos)")
        if idx.get("video_duration_hours"):
            row("Video duration", f"{idx['video_duration_hours']} hours")
        row("Faces detected", f"{idx.get('faces', 0):,}")
        row("Labeled people", f"{idx.get('labeled_people', 0):,}")
        row("Last indexed", idx.get("last_indexed_at") or "never")
        row("SQLite", idx.get("sqlite", ""))

    print()
