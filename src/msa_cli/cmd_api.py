import argparse
import json
import os
import signal
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path


_api_parser: argparse.ArgumentParser | None = None


def register(parent_sp: argparse._SubParsersAction) -> None:
    global _api_parser
    ap = parent_sp.add_parser(
        "api",
        help="Start, stop, restart, and check the status of the API server",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  msa api start                      # Start the API server (foreground; Ctrl+C to stop)
  msa api start --bind-host 0.0.0.0  # Accept connections from other machines / VMs
  msa api start --port 8080          # Start on a non-default port
  msa api stop                       # Stop a running API server
  msa api restart                    # Restart the API server
  msa api status                     # Show whether the server is running
  msa api status --json              # Machine-readable JSON output (exit 0=running, 1=stopped)
        """,
    )
    _api_parser = ap
    sp = ap.add_subparsers(dest="api_cmd", metavar="COMMAND")

    # ── start ──────────────────────────────────────────────────────────────────
    start = sp.add_parser("start", help="Start the API server (foreground)")
    start.add_argument("--config", default=None,
                       help="Config file (default: config.yaml)")
    start.add_argument("--bind-host", default=None, dest="bind_host",
                       help="Host to bind to (default: from config, usually 127.0.0.1). "
                            "Use 0.0.0.0 to accept connections from other machines.")
    start.add_argument("--port", type=int, default=None,
                       help="Port to listen on (default: from config, usually 8000)")

    # ── stop ───────────────────────────────────────────────────────────────────
    stop = sp.add_parser("stop", help="Stop the running API server")
    stop.add_argument("--config", default=None,
                      help="Config file (default: config.yaml)")
    stop.add_argument("--port", type=int, default=None,
                      help="Port override (used as fallback if PID file is missing)")

    # ── restart ────────────────────────────────────────────────────────────────
    restart = sp.add_parser("restart", help="Stop then start the API server")
    restart.add_argument("--config", default=None,
                         help="Config file (default: config.yaml)")
    restart.add_argument("--bind-host", default=None, dest="bind_host",
                         help="Host to bind to (passed through to start)")
    restart.add_argument("--port", type=int, default=None,
                         help="Port override (passed through to start)")

    # ── status ─────────────────────────────────────────────────────────────────
    status = sp.add_parser("status", help="Show API server status")
    status.add_argument("--config", default=None,
                        help="Config file (default: config.yaml)")
    status.add_argument("--port", type=int, default=None,
                        help="Port override")
    status.add_argument("--json", action="store_true", dest="json_output",
                        help="Output machine-readable JSON (exit 0=running, 1=stopped)")


def handle(args: argparse.Namespace) -> None:
    if not args.api_cmd:
        if _api_parser:
            _api_parser.print_help()
        sys.exit(0)

    if args.api_cmd == "start":
        _cmd_start(args)
    elif args.api_cmd == "stop":
        _cmd_stop(args)
    elif args.api_cmd == "restart":
        _cmd_restart(args)
    elif args.api_cmd == "status":
        _cmd_status(args)


# ── Helpers ────────────────────────────────────────────────────────────────────

def _load_cfg(args: argparse.Namespace):
    """Load config. Returns (cfg, config_path).

    If --config was given explicitly and is unreadable/invalid, exits immediately
    rather than silently falling back to defaults and potentially targeting the
    wrong instance.  When no --config is supplied and the default config.yaml is
    absent we return cfg=None and let callers fall back to built-in defaults,
    which is expected in bare dev checkouts.
    """
    explicit = getattr(args, "config", None)
    config_path = explicit or os.environ.get("MSA_CONFIG_PATH") or "config.yaml"
    try:
        from msa_settings import load_config
        return load_config(config_path), config_path
    except Exception as exc:
        if explicit:
            print(f"Error: cannot load config '{config_path}': {exc}", file=sys.stderr)
            sys.exit(1)
        return None, config_path


def _resolve_port(args: argparse.Namespace, cfg) -> int:
    if getattr(args, "port", None):
        return args.port
    if cfg:
        return cfg.api.port
    return 8000


def _resolve_host(args: argparse.Namespace, cfg) -> str:
    if getattr(args, "bind_host", None):
        return args.bind_host
    if cfg:
        return cfg.api.host
    return "127.0.0.1"


def _pid_file(cfg) -> Path:
    """Return the uvicorn PID file path — matches the location used by start.sh."""
    log_dir = os.getenv("MSA_LOG_DIR")
    if not log_dir and cfg:
        log_dir = str(cfg.log_dir)
    if not log_dir:
        log_dir = "logs"
    return Path(log_dir) / "run" / "uvicorn.pid"


def _read_pid(pid_file: Path):
    try:
        return int(pid_file.read_text().strip())
    except Exception:
        return None


def _pid_alive(pid: int) -> bool:
    if sys.platform == "win32":
        # os.kill(pid, 0) is unreliable on Windows — signal 0 is a POSIX probe
        # idiom that CPython doesn't implement correctly there, producing a
        # SystemError instead of OSError for dead/invalid PIDs.
        # Use OpenProcess + GetExitCodeProcess via ctypes instead.
        import ctypes
        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        handle = ctypes.windll.kernel32.OpenProcess(
            PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not handle:
            return False
        try:
            exit_code = ctypes.c_ulong(0)
            ok = ctypes.windll.kernel32.GetExitCodeProcess(
                handle, ctypes.byref(exit_code))
            return bool(ok) and exit_code.value == 259  # 259 = STILL_ACTIVE
        finally:
            ctypes.windll.kernel32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
        return True
    except (ProcessLookupError, PermissionError, OSError):
        return False


def _health_ok(port: int) -> bool:
    try:
        resp = urllib.request.urlopen(f"http://localhost:{port}/health", timeout=2)
        return resp.status == 200
    except Exception:
        return False


# ── Commands ───────────────────────────────────────────────────────────────────

def _cmd_start(args: argparse.Namespace) -> None:
    cfg, config_path = _load_cfg(args)
    host = _resolve_host(args, cfg)
    port = _resolve_port(args, cfg)
    pid_path = _pid_file(cfg)

    # Guard: already running via PID file
    existing_pid = _read_pid(pid_path)
    if existing_pid:
        if _pid_alive(existing_pid):
            print(f"API is already running (PID {existing_pid}) at http://localhost:{port}")
            sys.exit(1)
        else:
            # Stale PID file from an unclean shutdown — remove it and proceed.
            try:
                pid_path.unlink(missing_ok=True)
            except Exception:
                pass

    # Guard: port already in use (catches instances started outside msa api start,
    # stale PID files, and anything else holding the port)
    if _port_in_use(port):
        print(f"Port {port} is already in use. Is the API already running?")
        print(f"  Check with: msa api status")
        sys.exit(1)

    # Set env so the FastAPI app finds the right config.
    # Only set when config was loaded successfully; if cfg is None the default
    # config.yaml was absent — leave MSA_CONFIG_PATH unset so the server can
    # apply its own config-search logic rather than being pointed at a missing file.
    if cfg is not None:
        os.environ["MSA_CONFIG_PATH"] = config_path

    print(f"Starting API at http://{host}:{port}")
    print("Press Ctrl+C to stop.")

    # Write our PID so stop.sh / msa api stop can find us
    pid_path.parent.mkdir(parents=True, exist_ok=True)
    pid_path.write_text(str(os.getpid()))

    try:
        import uvicorn
        uvicorn.run(
            "msa_apps.search_api.app:app",
            host=host,
            port=port,
            log_level="info",
        )
    finally:
        try:
            pid_path.unlink(missing_ok=True)
        except Exception:
            pass


def _cmd_stop(args: argparse.Namespace) -> None:
    cfg, _ = _load_cfg(args)
    port = _resolve_port(args, cfg)
    pid_path = _pid_file(cfg)

    pid = _read_pid(pid_path)
    if pid and _pid_alive(pid):
        print(f"Stopping API (PID {pid})...")
        try:
            os.kill(pid, signal.SIGTERM)
            for _ in range(10):
                time.sleep(1)
                if not _pid_alive(pid):
                    break
            if _pid_alive(pid):
                # SIGKILL not available on Windows — TerminateProcess via SIGTERM already did it
                kill_sig = getattr(signal, "SIGKILL", signal.SIGTERM)
                os.kill(pid, kill_sig)
        except ProcessLookupError:
            pass
        try:
            pid_path.unlink(missing_ok=True)
        except Exception:
            pass
        print("API stopped.")
        return

    # No live PID file — try killing by port
    if _kill_by_port(port):
        print("API stopped.")
    else:
        print("API is not running.")


def _pids_on_port(port: int) -> list:
    """Return PIDs of processes listening on port."""
    import re
    import subprocess

    if sys.platform == "win32":
        # netstat -ano: last column is PID; match lines with our port in LISTENING state.
        try:
            result = subprocess.run(
                ["netstat", "-ano"],
                capture_output=True, text=True, timeout=5,
            )
            pids: set[int] = set()
            for line in result.stdout.splitlines():
                if f":{port}" in line and "LISTENING" in line:
                    m = re.search(r"\s+(\d+)\s*$", line)
                    if m:
                        pids.add(int(m.group(1)))
            return list(pids)
        except Exception:
            return []

    # Try lsof first (macOS + most Linux)
    try:
        result = subprocess.run(
            ["lsof", "-ti", f":{port}"],
            capture_output=True, text=True, timeout=3,
        )
        pids = [int(p) for p in result.stdout.split() if p.strip().isdigit()]
        if pids:
            return pids
    except FileNotFoundError:
        pass
    except Exception:
        return []

    # Fallback: ss (Linux without lsof)
    try:
        result = subprocess.run(
            ["ss", "-tlnp", f"sport = :{port}"],
            capture_output=True, text=True, timeout=3,
        )
        pids = [int(m) for m in re.findall(r"pid=(\d+)", result.stdout)]
        return pids
    except Exception:
        return []


def _port_in_use(port: int) -> bool:
    """Return True if anything is currently listening on port."""
    return bool(_pids_on_port(port))


def _pid_cmdline(pid: int) -> str:
    """Return the command line of a process as a string, or '' on failure."""
    import subprocess as _sp
    try:
        if sys.platform == "win32":
            r = _sp.run(
                ["wmic", "process", "where", f"ProcessId={pid}",
                 "get", "CommandLine", "/FORMAT:LIST"],
                capture_output=True, text=True, timeout=5,
            )
            for line in r.stdout.splitlines():
                if line.startswith("CommandLine="):
                    return line[len("CommandLine="):]
            return ""
        if sys.platform == "linux":
            with open(f"/proc/{pid}/cmdline", "rb") as f:
                return f.read().replace(b"\x00", b" ").decode(errors="replace")
        # macOS
        r = _sp.run(["ps", "-p", str(pid), "-o", "args="],
                    capture_output=True, text=True, timeout=2)
        return r.stdout
    except Exception:
        return ""


def _pid_is_api_server(pid: int) -> bool:
    """Return True if the process looks like our API server (uvicorn / msa_apps)."""
    cmdline = _pid_cmdline(pid)
    return "uvicorn" in cmdline or "msa_apps" in cmdline or "msa_cli" in cmdline


def _kill_by_port(port: int) -> bool:
    """Kill MSA API processes listening on port. Returns True if any were found.

    Only kills processes whose cmdline looks like our API server to avoid
    accidentally terminating unrelated services on the same port.
    """
    pids = _pids_on_port(port)
    if not pids:
        return False
    found = False
    for pid in pids:
        if not _pid_is_api_server(pid):
            print(f"  PID {pid} is listening on port {port} but does not look like "
                  f"the MSA API server — skipping.", file=sys.stderr)
            continue
        found = True
        try:
            os.kill(pid, signal.SIGTERM)
        except Exception:
            pass
    return found


def _cmd_restart(args: argparse.Namespace) -> None:
    _cmd_stop(args)
    time.sleep(1)
    _cmd_start(args)


def _cmd_status(args: argparse.Namespace) -> None:
    cfg, _ = _load_cfg(args)
    port = _resolve_port(args, cfg)
    pid_path = _pid_file(cfg)

    pid = _read_pid(pid_path)
    pid_alive = pid is not None and _pid_alive(pid)
    healthy = _health_ok(port)
    running = pid_alive or healthy

    if getattr(args, "json_output", False):
        result = {
            "running": running,
            "pid": pid if pid_alive else None,
            "url": f"http://localhost:{port}" if running else None,
            "health": "ok" if healthy else ("unreachable" if running else "stopped"),
        }
        print(json.dumps(result))
        sys.exit(0 if running else 1)

    if running:
        pid_str = f"PID {pid}, " if pid_alive else ""
        print(f"API status: running  ({pid_str}http://localhost:{port})")
        sys.exit(0)
    else:
        print("API status: stopped")
        sys.exit(1)
