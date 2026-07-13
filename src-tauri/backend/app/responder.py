"""Provisioning responder — bind ``SIDECAR_PORT`` immediately, before the heavy install.

MSA's first run installs ~2 GB of torch + deps, which dwarfs the supervisor's 120 s
``wait_ready`` budget (a template gap). The fix, per M-7/S-1 spec §1.2: a stdlib
``http.server`` bound to ``127.0.0.1:$SIDECAR_PORT`` the instant the shim starts, serving
``GET /health`` with a *provisioning* status (stage + pct + detail + log path) so the
supervisor's readiness handshake — and the SPA's ``/health`` poll — succeed within seconds
regardless of install duration. When provisioning finishes, the shim stops the responder,
frees the socket, and hands the same port to uvicorn (a short bind-retry closes the race).

Pure stdlib: this runs before the venv has fastapi/uvicorn. Thread-safe: the shim mutates a
shared :class:`ProvisionStatus` from the provisioning thread while the HTTP server serves it.
"""

from __future__ import annotations

import json
import socket
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# The webview preflights JSON requests: answer CORS + OPTIONS.
_CORS = {
    "Access-Control-Allow-Origin": "*",
    "Access-Control-Allow-Methods": "GET, OPTIONS",
    "Access-Control-Allow-Headers": "Content-Type",
}


class ProvisionStatus:
    """Thread-safe provisioning status the responder serves on ``/health``.

    Stages mirror the spec: ``python`` → ``deps-torch`` → ``deps-app`` → ``models-pending``.
    ``set_stage`` updates progress; ``fail`` switches to the error payload (with the log path).
    """

    # How many recently-fetched wheel filenames to keep for the setup screen's rolling file list
    # (spec §S-2.2). Small: it's a "these files are landing" activity signal, not a full manifest —
    # the newest is shown active, a few finished ones trail above it, older ones drop off.
    _MAX_FILES = 6

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._files: list[str] = []
        self._state: dict[str, object] = {
            "status": "provisioning",
            "stage": "python",
            "pct": 0,
            "detail": "Starting up",
            "log": "",
            "files": [],
        }

    def set_stage(self, stage: str, pct: int, detail: str = "", log: str = "") -> None:
        with self._lock:
            self._state.update(status="provisioning", stage=stage, pct=int(pct), detail=detail)
            if log:
                self._state["log"] = log

    def push_file(self, filename: str) -> None:
        """Record a wheel uv is fetching for the setup screen's rolling file list. Deduped against
        the most-recent entry (uv/disk-monitor emit the same in-flight file repeatedly) and capped
        at :data:`_MAX_FILES` (newest last). No-op on a blank name."""
        name = (filename or "").strip()
        if not name:
            return
        with self._lock:
            if self._files and self._files[-1] == name:
                return  # same file still in flight — don't duplicate
            self._files.append(name)
            del self._files[:-self._MAX_FILES]  # keep only the newest _MAX_FILES
            self._state["files"] = list(self._files)

    def fail(self, detail: str, log: str = "") -> None:
        with self._lock:
            self._state.update(status="error", detail=detail)
            if log:
                self._state["log"] = log

    def snapshot(self) -> dict[str, object]:
        with self._lock:
            return dict(self._state)


def _make_handler(status: ProvisionStatus):
    class _Handler(BaseHTTPRequestHandler):
        def log_message(self, *_a):  # silence stdlib request logging
            pass

        def _write(self, obj: dict, code: int = 200) -> None:
            body = json.dumps(obj).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            for k, v in _CORS.items():
                self.send_header(k, v)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_OPTIONS(self):  # noqa: N802 — stdlib naming
            self.send_response(204)
            for k, v in _CORS.items():
                self.send_header(k, v)
            self.end_headers()

        def do_GET(self):  # noqa: N802 — stdlib naming
            if self.path.split("?", 1)[0] == "/health":
                self._write(status.snapshot())
            else:
                self._write({"status": "provisioning", "detail": "not found"}, 404)

    return _Handler


class Responder:
    """Owns the provisioning HTTP server on the sidecar port and its background thread."""

    def __init__(self, port: int, status: ProvisionStatus) -> None:
        self.port = port
        self.status = status
        # allow_reuse_address (default True on HTTPServer) lets uvicorn rebind the port after
        # we stop, without waiting out TIME_WAIT.
        self._server = ThreadingHTTPServer(("127.0.0.1", port), _make_handler(status))
        self._thread = threading.Thread(
            target=self._server.serve_forever, name="provision-responder", daemon=True
        )

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        """Stop serving and release the socket so uvicorn can take the port."""
        try:
            self._server.shutdown()
        finally:
            self._server.server_close()
        self._thread.join(timeout=5.0)


def wait_for_free_port(
    port: int,
    *,
    host: str = "127.0.0.1",
    attempts: int = 10,
    delay: float = 0.2,
    sleep=time.sleep,
) -> bool:
    """Poll until ``host:port`` is bindable again (≤ ``attempts`` × ``delay`` s), closing the
    handoff race between the responder releasing the socket and uvicorn binding it. Returns
    True once a throwaway bind succeeds, False if it never frees in the budget."""
    for _ in range(attempts):
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            s.bind((host, port))
            s.close()
            return True
        except OSError:
            s.close()
            sleep(delay)
    return False
