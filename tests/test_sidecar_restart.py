"""Process-level restart lifecycle tests for the desktop sidecar (M-7 spec §1.3 contract #3).

The unit suite (``test_sidecar.py``) drives the watchdog/signal/serve pieces with injected
callables; this suite exercises the REAL machinery with real processes — real SIGTERM/SIGKILL
delivery, real ``getppid`` reparenting, real broken stderr pipes, real lock files, real
sockets — because the field failures lived in the gaps between the units: an orphaned backend
surviving its dead supervisor and blocking every later launch with "already running".

Scenarios (each restart must come up on the SAME port with the SAME lock file):
  - restart after a **crash** (SIGKILL — no teardown ran; stale lock + port must recover)
  - restart after a **graceful quit** (SIGTERM — the contract-#3 hard-exit handler, exit 0)
  - **rapid start→kill→restart cycles** with a short human-scale pause (a user reopening the
    app right after closing it), alternating crash and graceful teardown
  - a **second instance is refused** while the first is alive (the lock's positive duty)
  - **supervisor hard-quit**: the sidecar must reap ITSELF via the parent-watchdog even though
    its stderr pipe died with the supervisor (the field-orphan regression), and a fresh launch
    right after must succeed.

No uvicorn / FastAPI: the stub child wires the same sidecar primitives around a plain
listening socket, so the suite stays fast and dependency-light.
"""

import os
import signal
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(
    os.name == "nt",
    reason="POSIX signal/reparent semantics; the Windows lifecycle is covered by the "
    "shell-bundle E2E harness",
)

_REPO = Path(__file__).resolve().parents[1]
_SRC = _REPO / "src"

# The backend stand-in: the real lock, the real bind, the real SIGTERM handler, the real
# parent-watchdog — around a plain socket instead of uvicorn. READY is printed only once the
# lock is held and the port is bound, so a reader can treat it as "startup complete".
_STUB_SRC = """\
import os, sys, time
from pathlib import Path

from msa_apps.search_api.sidecar import _bind_reuse_socket, install_sigterm, start_parent_watchdog
from msa_settings.instance_lock import acquire_instance_lock

acquire_instance_lock(Path(os.environ["MSA_TEST_LOCK"]), "restart-stub")
sock = _bind_reuse_socket("127.0.0.1", int(os.environ["SIDECAR_PORT"]))
install_sigterm()
_sup = int(os.environ.get("SUPERVISOR_PID") or 0)
if _sup:
    start_parent_watchdog(_sup, interval=0.2)
print("READY", flush=True)
while True:
    time.sleep(0.25)
"""

# A minimal supervisor with the real topology: the stub is its DIRECT child and its stderr is
# a pipe the supervisor holds — so a supervisor hard-death both drifts the child's getppid()
# AND breaks the child's stderr pipe (the combination that orphaned the field backend).
_SUPERVISOR_SRC = """\
import os, subprocess, sys, time

env = dict(os.environ, SUPERVISOR_PID=str(os.getpid()))
child = subprocess.Popen([sys.executable, sys.argv[1]], env=env, text=True,
                         stdout=subprocess.PIPE, stderr=subprocess.PIPE)
assert child.stdout.readline().strip() == "READY"
print(f"CHILD {child.pid}", flush=True)
time.sleep(600)  # park until the test SIGKILLs us (a hard quit: no SIGTERM is forwarded)
"""


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _stub_env(port: int, lock: Path) -> dict:
    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join([str(_SRC), env.get("PYTHONPATH", "")])
    env["SIDECAR_PORT"] = str(port)
    env["MSA_TEST_LOCK"] = str(lock)
    env.pop("SUPERVISOR_PID", None)  # only the supervisor scenario sets it, in-process
    return env


def _stub_path(tmp_path: Path) -> Path:
    stub = tmp_path / "stub.py"
    if not stub.exists():
        stub.write_text(_STUB_SRC, encoding="utf-8")
    return stub


def _spawn_stub(tmp_path: Path, port: int, lock: Path) -> subprocess.Popen:
    return subprocess.Popen(
        [sys.executable, str(_stub_path(tmp_path))],
        env=_stub_env(port, lock),
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )


def _read_line(proc: subprocess.Popen, timeout: float = 20.0) -> str:
    """One stdout line with a timeout ('' if the process died silently); never hangs pytest."""
    box: dict = {}

    def _read():
        box["line"] = proc.stdout.readline()

    t = threading.Thread(target=_read, daemon=True)
    t.start()
    t.join(timeout)
    if "line" not in box:
        proc.kill()
        raise AssertionError(f"timed out after {timeout}s waiting for stub output")
    return box["line"].strip()


def _wait_ready(proc: subprocess.Popen) -> None:
    line = _read_line(proc)
    if line != "READY":
        try:
            _, err = proc.communicate(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            _, err = proc.communicate()
        raise AssertionError(f"stub did not become ready: line={line!r} stderr={err!r}")


def _wait_gone(pid: int, timeout: float = 10.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return
        time.sleep(0.1)
    raise AssertionError(
        f"pid {pid} still alive after {timeout}s — orphaned backend (watchdog failed to reap)"
    )


def _reap(proc: subprocess.Popen | None) -> None:
    if proc is not None and proc.poll() is None:
        proc.kill()
        proc.wait(timeout=10)


def test_restart_after_crash(tmp_path):
    """A crash (SIGKILL — no teardown) leaves the lock file behind with a dead PID; the next
    launch must remove the stale lock and rebind the same port."""
    port, lock = _free_port(), tmp_path / "msa-api.lock"
    p2 = None
    p1 = _spawn_stub(tmp_path, port, lock)
    try:
        _wait_ready(p1)
        p1.send_signal(signal.SIGKILL)
        p1.wait(timeout=10)
        assert lock.read_text().strip() == str(p1.pid)  # the crash left its stale lock behind
        p2 = _spawn_stub(tmp_path, port, lock)
        _wait_ready(p2)
        assert lock.read_text().strip() == str(p2.pid)  # stale lock replaced, not appended to
    finally:
        _reap(p1)
        _reap(p2)


def test_restart_after_graceful_teardown(tmp_path):
    """A supervisor quit delivers SIGTERM; contract #3 exits hard and clean (code 0). The exit
    does NOT unlink the lock (os._exit skips teardown by design), so the relaunch exercises
    the same stale-lock removal as the crash path — and must come up."""
    port, lock = _free_port(), tmp_path / "msa-api.lock"
    p2 = None
    p1 = _spawn_stub(tmp_path, port, lock)
    try:
        _wait_ready(p1)
        p1.send_signal(signal.SIGTERM)
        assert p1.wait(timeout=10) == 0  # the non-deadlocking handler: immediate, clean
        p2 = _spawn_stub(tmp_path, port, lock)
        _wait_ready(p2)
    finally:
        _reap(p1)
        _reap(p2)


def test_rapid_start_kill_restart_cycles(tmp_path):
    """Start→kill→restart in quick succession, with a short human-scale pause after each
    launch (a user reopening the app right after closing or force-quitting it). Alternates
    crash and graceful teardown; every relaunch must come up on the SAME port with the SAME
    lock (SO_REUSEADDR must beat TIME_WAIT; stale-lock removal must beat the dead PID)."""
    port, lock = _free_port(), tmp_path / "msa-api.lock"
    proc = None
    try:
        for cycle in range(3):
            proc = _spawn_stub(tmp_path, port, lock)
            _wait_ready(proc)
            time.sleep(0.3)  # the user looks at the window before closing it
            proc.send_signal(signal.SIGKILL if cycle % 2 == 0 else signal.SIGTERM)
            proc.wait(timeout=10)
        proc = _spawn_stub(tmp_path, port, lock)
        _wait_ready(proc)  # the final relaunch stays up
    finally:
        _reap(proc)


def test_second_instance_refused_while_first_is_alive(tmp_path):
    """The lock's positive duty: while a live instance holds it, a second start must refuse
    loudly (this is the guard that correctly refused to start when a field orphan was holding
    the lock — the bug was the orphan, never the refusal)."""
    port, lock = _free_port(), tmp_path / "msa-api.lock"
    p1 = _spawn_stub(tmp_path, port, lock)
    try:
        _wait_ready(p1)
        p2 = _spawn_stub(tmp_path, _free_port(), lock)  # different port: the LOCK refuses, not the bind
        _, err = p2.communicate(timeout=20)
        assert p2.returncode != 0
        assert "already running" in err
        assert lock.read_text().strip() == str(p1.pid)  # the holder was untouched
    finally:
        _reap(p1)


def test_supervisor_hard_quit_reaps_backend_and_restart_succeeds(tmp_path):
    """THE field-orphan regression, end-to-end. The supervisor dies hard (SIGKILL — no SIGTERM
    reaches the backend), which simultaneously drifts the backend's getppid() AND breaks its
    stderr pipe. The parent-watchdog must still reap the backend (the broken pipe used to kill
    the watchdog thread first, leaving a live orphan holding port + lock for days), and the
    very next launch must come up."""
    port, lock = _free_port(), tmp_path / "msa-api.lock"
    stub = _stub_path(tmp_path)
    supervisor = tmp_path / "supervisor.py"
    supervisor.write_text(_SUPERVISOR_SRC, encoding="utf-8")

    p2 = None
    sup = subprocess.Popen(
        [sys.executable, str(supervisor), str(stub)],
        env=_stub_env(port, lock),
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )
    try:
        line = _read_line(sup)
        assert line.startswith("CHILD "), f"supervisor never reported its child: {line!r}"
        backend_pid = int(line.split()[1])
        sup.send_signal(signal.SIGKILL)  # hard quit — exactly what orphaned the field backend
        sup.wait(timeout=10)
        _wait_gone(backend_pid)  # the watchdog reaps the backend on its own
        p2 = _spawn_stub(tmp_path, port, lock)
        _wait_ready(p2)  # and the world is immediately launchable again
    finally:
        _reap(sup)
        _reap(p2)
