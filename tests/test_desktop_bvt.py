"""Desktop BVT — headless-safe backend-contract test (M-7/S-4 spec item 5).

The release-blocking real-host validation (silent install → provision → search → no-orphan) is a
human/VM step (S-3 `validate-installed-desktop-windows.ps1`). This is its CI-runnable companion:
it exercises the responder→sidecar HANDOFF and the ``/health`` state machine the Tauri supervisor's
``wait_ready`` depends on, using the *staged shim's* real ``Responder`` + ``wait_for_free_port`` and
the *real* backend sidecar entry (``sidecar.run`` — port bind, SIGTERM handler, parent-watchdog,
uvicorn) with a real ``SIDECAR_PORT`` and a fake ``SUPERVISOR_PID``. The heavy ML backend is stood in
for by a torch-free app so this stays fast and side-effect-free on CI (its full boot + search is the
human/VM real-host step). It also asserts the updater endpoint targets the PUBLIC repo — the in-app
updater resolves the manifest anonymously only there (spec §S-4.2), the constant the pipeline signs.

Runs in the non-slow suite so it gates on every PR (mirrors how 4D.2 gates today). Follows the
test_desktop_shim / test_sidecar patterns: src-tauri/backend on sys.path, offline, no packaged app.
"""

from __future__ import annotations

import json
import logging
import os
import re
import signal
import socket
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[1]
_SHIM_ROOT = _REPO_ROOT / "src-tauri" / "backend"
if str(_SHIM_ROOT) not in sys.path:
    sys.path.insert(0, str(_SHIM_ROOT))

from app import responder as shim_responder  # noqa: E402 — the staged shim's provisioning responder
from msa_apps.search_api import sidecar  # noqa: E402 — the real backend sidecar entry

_TAURI_CONF = _REPO_ROOT / "src-tauri" / "tauri.conf.json"
_PUBLIC_REPO = "openara-ai/media-search-agent"
# Anchor on the full host+path. A substring test (`_PUBLIC_REPO in e`) also accepts the
# staging repo `openara-ai/media-search-agent-public-staging`, whose releases are NOT
# anonymously resolvable — the in-app updater would then silently fail for end users.
_PUBLIC_REPO_RELEASES_RE = re.compile(rf"^https://github\.com/{re.escape(_PUBLIC_REPO)}/releases/")
_HEALTH_STAGES = {"python", "deps-torch", "deps-app", "models-pending"}

# A torch-free stand-in for the ML backend so the REAL sidecar entry (uvicorn, signals, watchdog,
# port bind, /health) is exercised on CI without importing the heavy stack — booting the real app
# in-process segfaults the interpreter at teardown (torch native finalization) and would kick off a
# model download. /health returns starting on the first poll, ready after: the backend-side
# transition that follows the responder's provisioning.
_STUB_APP = """\
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

app = FastAPI()
app.add_middleware(
    CORSMiddleware, allow_origins=["*"], allow_credentials=True,
    allow_methods=["*"], allow_headers=["*"],
)
_polls = {"n": 0}


@app.get("/health")
def health():
    _polls["n"] += 1
    return {"status": "ready" if _polls["n"] > 1 else "starting"}
"""


# ── fixtures (mirror test_desktop_shim: don't leak root-log handlers or signal handlers) ─────


@pytest.fixture
def clean_root_logger():
    root = logging.getLogger()
    before = root.handlers[:]
    level = root.level
    try:
        yield root
    finally:
        for handler in root.handlers[:]:
            if handler not in before:
                root.removeHandler(handler)
                try:
                    handler.close()
                except Exception:
                    pass
        root.setLevel(level)


@pytest.fixture
def restore_signals():
    old_term = signal.getsignal(signal.SIGTERM)
    old_int = signal.getsignal(signal.SIGINT)
    try:
        yield
    finally:
        signal.signal(signal.SIGTERM, old_term)
        signal.signal(signal.SIGINT, old_int)


@pytest.fixture
def free_tcp_port():
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def _poll_health(port: int, want: set[str], timeout: float) -> dict:
    """Poll ``GET /health`` until ``status`` is in ``want`` (tolerating the connection-refused
    window while the responder frees the port and the backend rebinds it). Returns the JSON
    payload; raises AssertionError with the last error on timeout."""
    deadline = time.monotonic() + timeout
    last = ""
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=3) as resp:
                body = json.loads(resp.read())
            if body.get("status") in want:
                return body
            last = f"status={body.get('status')!r} (want {want})"
        except (urllib.error.URLError, ConnectionError, OSError, ValueError) as exc:
            last = repr(exc)
        time.sleep(0.1)
    raise AssertionError(f"/health never reached {want} within {timeout}s; last: {last}")


# ── updater endpoint targets the PUBLIC repo (spec §S-4.2 / ADR-012 §5) ──────────────────────


def test_bvt_updater_endpoint_targets_public_repo():
    conf = json.loads(_TAURI_CONF.read_text(encoding="utf-8"))
    endpoints = conf.get("plugins", {}).get("updater", {}).get("endpoints", [])
    assert endpoints, "updater must declare at least one endpoint (never an empty plugins block)"
    # The in-app updater resolves latest.json anonymously ONLY on the public repo — every endpoint
    # must target it, or self-update silently fails for end users on a private/staging URL.
    assert all(_PUBLIC_REPO_RELEASES_RE.match(e) for e in endpoints), endpoints
    assert any("latest.json" in e for e in endpoints)


def test_bvt_updater_endpoint_rejects_staging_lookalike():
    """The tightened host+path anchor must REJECT the staging mirror
    (`…-public-staging`), whose releases are not anonymously resolvable — a plain substring
    check (`_PUBLIC_REPO in e`) would have wrongly accepted it."""
    public = (
        "https://github.com/openara-ai/media-search-agent/releases/latest/download/latest.json"
    )
    staging = (
        "https://github.com/openara-ai/media-search-agent-public-staging"
        "/releases/latest/download/latest.json"
    )
    assert _PUBLIC_REPO_RELEASES_RE.match(public)
    assert _PUBLIC_REPO_RELEASES_RE.match(staging) is None
    # Guard against a regression to the old, too-permissive substring check.
    assert _PUBLIC_REPO in staging


# ── responder contract: /health provisioning + CORS preflight ───────────────────────────────


def test_bvt_responder_health_and_cors(free_tcp_port):
    status = shim_responder.ProvisionStatus()
    status.set_stage("deps-torch", 40, "Installing PyTorch", log="/tmp/p.log")
    r = shim_responder.Responder(free_tcp_port, status)
    r.start()
    try:
        body = _poll_health(free_tcp_port, want={"provisioning"}, timeout=5)
        assert body["stage"] == "deps-torch" and body["pct"] == 40
        # The webview preflights JSON — OPTIONS must answer with CORS.
        req = urllib.request.Request(
            f"http://127.0.0.1:{free_tcp_port}/health", method="OPTIONS",
            headers={"Origin": "tauri://localhost"},
        )
        with urllib.request.urlopen(req, timeout=5) as resp:
            assert resp.status == 204
            assert resp.headers.get("Access-Control-Allow-Origin") == "*"
    finally:
        r.stop()


# ── the handoff: responder(provisioning) → free the port → real sidecar entry(/health) ───────


def test_bvt_responder_to_sidecar_handoff(
    tmp_path, monkeypatch, free_tcp_port, restore_signals, clean_root_logger
):
    """The readiness handshake the Tauri supervisor performs: the shim's responder answers
    ``status=provisioning`` on SIDECAR_PORT (so ``wait_ready`` succeeds in seconds regardless of the
    multi-GB install), then the port is freed and the REAL backend sidecar entry (``sidecar.run``)
    binds it and serves ``/health`` → ``starting``|``ready``. Exercises the real Responder,
    ``wait_for_free_port``, and ``sidecar.run`` (bind, signal handler, watchdog, uvicorn); the ML
    backend is stood in torch-free."""
    port = free_tcp_port

    # Phase 1 — responder serves provisioning + a stage transition on the sidecar port.
    status = shim_responder.ProvisionStatus()
    status.set_stage("python", 0, "Preparing runtime")
    r = shim_responder.Responder(port, status)
    r.start()
    prov = _poll_health(port, want={"provisioning"}, timeout=5)
    assert prov["status"] == "provisioning" and prov["stage"] == "python", prov
    status.set_stage("deps-torch", 40, "Installing PyTorch")
    assert _poll_health(port, want={"provisioning"}, timeout=5)["stage"] == "deps-torch"

    # Handoff — stop the responder, wait for the socket to free, then the real sidecar binds it.
    r.stop()
    assert shim_responder.wait_for_free_port(port) is True

    # Torch-free stand-in backend, importable by uvicorn as `bvt_stub_app:app`.
    (tmp_path / "bvt_stub_app.py").write_text(_STUB_APP, encoding="utf-8")
    monkeypatch.syspath_prepend(str(tmp_path))

    # Capture uvicorn's Server so teardown stops it WITHOUT a signal (the sidecar's SIGTERM handler
    # would os._exit the whole test process), and neutralize _serve's os._exit(1) startup-failure
    # path so a hiccup can never kill the test runner.
    captured: dict[str, object] = {}
    orig_serve = sidecar._serve

    def capture_serve(server_factory, **kw):
        # _serve now takes a factory (it may rebuild the Server to retry the bind race), so wrap it
        # to capture the ACTUAL uvicorn.Server it builds — teardown needs the real server to set
        # should_exit (a graceful stop, no signal) so sidecar.run returns.
        def capturing_factory():
            srv = server_factory()
            captured["server"] = srv
            return srv

        kw.setdefault("exit_fn", lambda code: captured.__setitem__("serve_exit", code))
        orig_serve(capturing_factory, **kw)

    monkeypatch.setattr(sidecar, "_serve", capture_serve)
    # Fake SUPERVISOR_PID = our own pid ⇒ the watchdog sees the "supervisor" alive and never reaps.
    monkeypatch.setenv("SIDECAR_PORT", str(port))
    monkeypatch.setenv("SUPERVISOR_PID", str(os.getpid()))
    monkeypatch.setenv("MSA_LOG_DIR", str(tmp_path / "logs"))

    # sidecar.run installs SIGTERM/SIGINT handlers (main-thread only), so run it here and drive the
    # /health poll + teardown from a helper thread.
    results: dict[str, object] = {}

    def driver() -> None:
        try:
            results["ready"] = _poll_health(port, want={"starting", "ready"}, timeout=20)
        except Exception as exc:  # noqa: BLE001 — surfaced as a failure below
            results["error"] = exc
        finally:
            deadline = time.monotonic() + 20
            while "server" not in captured and time.monotonic() < deadline:
                time.sleep(0.05)
            srv = captured.get("server")
            if srv is not None:
                for _ in range(300):  # wait until uvicorn is in its serve loop before should_exit
                    if getattr(srv, "started", False):
                        break
                    time.sleep(0.05)
                srv.should_exit = True  # graceful uvicorn shutdown (no signal) → sidecar.run returns

    driver_thread = threading.Thread(target=driver, name="bvt-driver", daemon=True)
    driver_thread.start()
    try:
        sidecar.run(app_path="bvt_stub_app:app")  # real sidecar entry, torch-free stand-in app
    finally:
        driver_thread.join(timeout=15)

    assert "error" not in results, results.get("error")
    ready = results["ready"]
    assert ready["status"] in {"starting", "ready"}, ready
    assert captured.get("serve_exit") is None, "sidecar reported a startup failure (os._exit(1) path)"
