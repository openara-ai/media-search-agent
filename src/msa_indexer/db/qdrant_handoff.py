"""Sentinel-file handshake for the embedded-Qdrant lock window (M-8/S-2).

The indexer runs for hours touching only SQLite; it needs the embedded
Qdrant lock only for the export window at the end of a run. This module
owns the file protocol through which the indexer asks the API process to
release its shared client for exactly that window:

- ``qdrant.request`` — written by the INDEXER immediately before its first
  Qdrant open. JSON ``{"run_id", "pid", "ts"}``.
- ``qdrant.granted`` — written by the API's handoff watcher after it has
  drained in-flight operations and closed the shared client, echoing the
  ``run_id`` of the request it grants. The indexer accepts only a grant
  bearing its own run_id and deletes any other, so a stale grant surviving
  a crashed prior run can never authorize an export before the drain has
  actually happened.

Both files live in ONE well-known run directory both processes derive
from the ACTIVE CONFIG (round-5 review finding, P2): env overrides first
(``MSA_QDRANT_HANDOFF_DIR``, then ``$MSA_LOG_DIR/run``), else the loaded
config's ``log_dir``/run — a cwd-independent app-private location, so an
API and a standalone ``msa index export --config ...`` launched from
different working directories still agree on the same slot. ``<cwd>/run``
remains only as the last-resort fallback when no config can be loaded at
all. The API passes its own resolved dir to a spawned indexer via
``MSA_QDRANT_HANDOFF_DIR``. The protocol is pure file I/O — no signals,
no HTTP — because the stop sentinel proved that is the only mechanism
that carries on Windows (WIN-006), and a file is spawner-independent:
the API can restart mid-run and the handshake still works on re-attach.

File-lifecycle ownership (round-3 review finding, P2): the handshake files
are a single shared slot, so REMOVING them is a privileged action. Every
cleanup routes through :func:`cleanup_stale`, the one owner/liveness-aware
authority: it clears the files only when the request's recorded pid is
dead, belongs to the calling process itself, or the request is absent
(an orphaned grant never authorizes anything — grants are run_id-bound).
A LIVE foreign window is never clobbered — a new requester waits bounded
for it to clear (:func:`_claim_request_slot`), then fails loudly with
:class:`HandoffSlotBusyError` (§3.1 step 5); the API treats a live foreign
request found at startup exactly like a fresh ``qdrant.request`` arrival.
The lifecycle grep-gate in ``tests/test_qdrant_handoff.py`` enforces that
no code outside this module deletes the sentinels directly.

Slot acquisition is claim-by-create (round-4 review finding, P2): the
request file is created with ``O_CREAT|O_EXCL`` — atomic on POSIX and on
Windows/NTFS — so the create itself IS the claim. Two fresh concurrent
requesters can never both own the slot: exactly one create succeeds; the
loser re-enters the bounded wait. There is no check-then-write anywhere.

Kill switch: ``MSA_QDRANT_HANDOFF=off`` disables the handshake on both
sides (the API subprocess env is inherited), restoring the pre-S-2
close-at-start behavior.
"""
import json
import os
import sys
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Callable, Iterator, Optional

from loguru import logger

REQUEST_FILE_NAME = "qdrant.request"
GRANTED_FILE_NAME = "qdrant.granted"

# Indexer-side budget waiting for a grant before proceeding anyway
# (no API alive, or headless CLI run — L6 in the plan's risk table).
# Deliberately SHORT: this is the only cost a headless run pays, and the
# alignment with the API watcher's slower worst case lives in the lock
# ladder below, not here (round-4 review finding, P2).
GRANT_WAIT_SECONDS = 15.0
GRANT_POLL_SECONDS = 0.2
# Backoff ladder for the first Qdrant open when the lock is still held
# (drain-timeout residual race, or a foreign process): retry up to this
# total, then fail loudly (§3.1 step 5).
#
# Round-4 alignment (P2): the ladder must outlast the API watcher's
# worst-case LEGITIMATE grant latency — an in-flight §4 payload write is
# waited on up to the write-drain ceiling (120s,
# _HANDOFF_WRITE_DRAIN_CEILING_SECONDS in indexer_manager), then the
# reader drain (10s) and poll latency (~0.5s) follow: ≈131s after the
# request lands. The previous 15s grant wait + 60s ladder gave up at
# ~75s, declaring a spurious export_blocked while the watcher was still
# correctly draining a 75–120s bulk label/merge. Open attempts are
# harmless probes: they fail fast while the API holds the embedded lock
# and succeed the moment the grant-side close lands, so a grant arriving
# mid-ladder is honored implicitly — no second wait needed, and headless
# runs still proceed after the 15s grant wait at the cost of one failed
# open only when a foreign process genuinely holds the lock. The loud
# export_blocked therefore fires only after GRANT_WAIT_SECONDS + this
# ladder ≈ 165s — beyond the watcher's worst case, never during it.
LOCK_RETRY_TOTAL_SECONDS = 150.0
# Budget a NEW requester waits for a LIVE foreign handoff window to clear
# before failing loudly (HandoffSlotBusyError). Same budget class as the
# lock-retry ladder — the two are the same "someone else genuinely has
# Qdrant right now" condition observed at different layers, and a foreign
# window's release includes the same worst-case drain latency, so the two
# budgets move together (round-4).
SLOT_WAIT_TOTAL_SECONDS = 150.0
# Grace period before an EXISTING-but-unparseable request file is treated
# as a crash leftover rather than a claim whose content write has not
# landed yet. The claim-by-create in _try_create_request makes the O_EXCL
# create and the content write two steps, so a poller can briefly observe
# an empty file; within the grace it is honored as a live in-progress
# claim (never clobber what we cannot prove stale), past it a process
# that died between create and write must not wedge the slot forever.
CLAIM_WRITE_GRACE_SECONDS = 5.0


class HandoffSlotBusyError(RuntimeError):
    """A LIVE foreign process holds the handoff window (request slot).

    Raised by handoff_window() when the slot did not clear within the
    bounded wait — the caller must fail loudly (§3.1 step 5: ERROR log,
    export_blocked, no version record) rather than clobber the active
    window or interleave two exporters.
    """


if sys.platform == "win32":
    # Windows liveness check for the request's recorded pid. os.kill(pid, 0)
    # is NOT a POSIX-style existence check on Windows — it calls
    # TerminateProcess via OpenProcess (it would KILL the process), and as
    # long as any handle to an already-exited process is still open,
    # OpenProcess succeeds and reports the PID as alive. Use
    # GetExitCodeProcess on a query-only handle and treat anything other
    # than STILL_ACTIVE as dead. Same idiom as the indexer pid handling in
    # msa_apps.search_api.indexer_manager, which imports pid_alive from
    # here so the process has exactly ONE liveness implementation.
    #
    # ctypes defaults function restype to c_int, but a Windows HANDLE is
    # pointer-sized — declare argtypes/restype explicitly, on a private
    # WinDLL instance so we don't change signatures other code relies on.
    import ctypes
    from ctypes import wintypes

    _PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    _STILL_ACTIVE = 259
    _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    _kernel32.OpenProcess.restype = wintypes.HANDLE
    _kernel32.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
    _kernel32.GetExitCodeProcess.restype = wintypes.BOOL
    _kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    _kernel32.CloseHandle.restype = wintypes.BOOL


def pid_alive(pid: int) -> bool:
    """True when ``pid`` is a live process (platform-correct; see win32 note)."""
    if sys.platform == "win32":
        handle = _kernel32.OpenProcess(_PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not handle:
            return False
        try:
            exit_code = wintypes.DWORD()
            if not _kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code)):
                return False
            return exit_code.value == _STILL_ACTIVE
        finally:
            _kernel32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
        return True
    except (ProcessLookupError, PermissionError):
        return False


def handoff_enabled() -> bool:
    """Kill switch predicate — MSA_QDRANT_HANDOFF=off disables the handshake."""
    return os.getenv("MSA_QDRANT_HANDOFF", "").strip().lower() != "off"


def _config_log_dir() -> Optional[Path]:
    """log_dir of the ACTIVE config, or None when no config can be loaded.

    ``load_config()`` is lru-cached and already encodes the documented
    precedence (MSA_LOG_DIR env, else the platform log dir; MSA_DEV=1 uses
    the repo-relative dev location) — crucially, none of those depend on
    the process working directory. Failure-tolerant local import: the
    handshake degrades to the legacy derivation in stripped environments
    without msa_settings/config.yaml rather than breaking an export.
    """
    try:
        from msa_settings import load_config
        return Path(load_config().log_dir)
    except Exception:
        return None


def handoff_dir(log_dir: Optional[Path] = None) -> Path:
    """The run directory holding the handshake files.

    The two sentinels are a single shared slot, so the API watcher and any
    exporter MUST compute the identical directory even when launched from
    different working directories (round-5 review finding, P2 — the old
    <cwd>/run fallback made an API and a standalone ``msa index export
    --config ...`` poll two different dirs, and the documented manual-repair
    export timed out into lock contention). Resolution order:

    1. ``MSA_QDRANT_HANDOFF_DIR`` — explicit override; IndexerManager passes
       its own resolved dir to the spawned indexer subprocess.
    2. ``$MSA_LOG_DIR/run`` — same env rule as the pid/stop sentinel dir.
    3. ``log_dir``/run — the caller's ACTIVE CONFIG ``cfg.log_dir`` when
       supplied (the pipeline passes its ``--config``'s value), else the
       loaded config's ``log_dir`` via :func:`_config_log_dir`. Both are
       cwd-independent, so two processes sharing a config agree.
    4. ``<cwd>/run`` — last resort, only when no config can be loaded.
    """
    override = os.getenv("MSA_QDRANT_HANDOFF_DIR")
    if override:
        return Path(override)
    env_log = os.getenv("MSA_LOG_DIR")
    if env_log:
        return Path(env_log) / "run"
    if log_dir is None:
        log_dir = _config_log_dir()
    if log_dir is not None:
        return Path(log_dir) / "run"
    return Path(os.getcwd()) / "run"


def request_path(base: Optional[Path] = None) -> Path:
    return (base if base is not None else handoff_dir()) / REQUEST_FILE_NAME


def granted_path(base: Optional[Path] = None) -> Path:
    return (base if base is not None else handoff_dir()) / GRANTED_FILE_NAME


def _write_json_atomic(path: Path, payload: dict) -> None:
    """Write via temp file + os.replace so a poller never reads a partial file.

    os.replace is atomic on POSIX and on Windows within a volume — the same
    guarantee class the stop-sentinel idiom relies on.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, sort_keys=True), encoding="utf-8")
    os.replace(tmp, path)


def _read_json(path: Path) -> Optional[dict]:
    """Parsed JSON dict, or None when absent/corrupt (poller retries)."""
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return payload if isinstance(payload, dict) else None


def write_request(run_id: str, base: Optional[Path] = None) -> None:
    """Overwrite the request file (atomic temp+replace). NOT the claim path:
    the indexer acquires the slot via _claim_request_slot()'s exclusive
    create — an unconditional overwrite would reintroduce the round-4
    last-writer-wins race. Retained for tests simulating a requester."""
    _write_json_atomic(
        request_path(base),
        {"run_id": run_id, "pid": os.getpid(), "ts": time.time()},
    )


def _try_create_request(run_id: str, base: Optional[Path] = None) -> bool:
    """Atomically CLAIM the request slot by creating the file exclusively.

    The ``O_CREAT|O_EXCL`` create itself is the claim — atomic on POSIX and
    on Windows/NTFS (pure file I/O, WIN-006) — so exactly one of any number
    of concurrent requesters succeeds; the rest get ``FileExistsError`` and
    return False (slot busy — re-enter the bounded wait). This replaces the
    old check-then-write, whose window between the last slot poll and
    write_request() let a second fresh requester replace the first request
    (round-4 review finding, P2).

    The content lands in a single write immediately after the create; a
    reader catching the gap sees an unparseable file and retries
    (_read_json → None), and cleanup_stale() honors such a file as a live
    in-progress claim within CLAIM_WRITE_GRACE_SECONDS.

    Returns True when the slot was claimed, False when it is already
    occupied. OSErrors other than "already exists" propagate (the caller
    degrades to no-handshake, same as the old write-failure path).
    """
    path = request_path(base)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(
        {"run_id": run_id, "pid": os.getpid(), "ts": time.time()},
        sort_keys=True,
    )
    try:
        fd = os.open(str(path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        return False
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(payload)
    return True


def read_request(base: Optional[Path] = None) -> Optional[dict]:
    return _read_json(request_path(base))


def write_grant(run_id: str, base: Optional[Path] = None) -> None:
    _write_json_atomic(granted_path(base), {"run_id": run_id, "ts": time.time()})


def read_grant(base: Optional[Path] = None) -> Optional[dict]:
    return _read_json(granted_path(base))


def _clear_request(base: Optional[Path] = None) -> None:
    """Raw removal primitive. NEVER call outside this module — all cleanup
    must route through the owner/liveness-aware cleanup_stale() (grep-gated)."""
    try:
        request_path(base).unlink(missing_ok=True)
    except OSError:
        pass


def _clear_grant(base: Optional[Path] = None) -> None:
    """Raw removal primitive — see _clear_request."""
    try:
        granted_path(base).unlink(missing_ok=True)
    except OSError:
        pass


def _clear_all(base: Optional[Path] = None) -> None:
    """Raw removal primitive — see _clear_request."""
    _clear_request(base)
    _clear_grant(base)


def cleanup_stale(
    base: Optional[Path] = None,
    pid_alive_fn: Optional[Callable[[int], bool]] = None,
) -> bool:
    """THE single authority for removing the handshake files (round-3, P2).

    Owner/liveness-aware: clears ``qdrant.request``/``qdrant.granted`` only
    when the window is provably not in use —

    - the request is absent (a leftover grant is orphaned: grants are
      run_id-bound and never authorize a later window, so it is swept), or
    - the request's recorded pid is the calling process itself (our own
      window — the indexer-side finally-cleanup), or
    - the request's recorded pid is dead (crash leftovers — the L4
      stale-file mitigation and the watcher's dead-pid fail-safe).

    A LIVE foreign request is NEVER cleared: the files are preserved and
    the function returns False. Callers must then treat the window as
    active — the API side serves it like a fresh request arrival; a new
    requester waits via _claim_request_slot() and fails loudly on
    timeout. A request whose pid cannot be read or verified is treated as
    live (fail-safe: never clobber what we cannot prove stale). The same
    fail-safe covers a request file that EXISTS but does not parse: within
    CLAIM_WRITE_GRACE_SECONDS of its mtime it is a claim whose content
    write has not landed yet (see _try_create_request) and is preserved;
    past the grace it is a crashed-mid-claim leftover and is cleared, so a
    dead half-written claim can never wedge the slot.

    Returns True when the slot is free after the call, False when a live
    foreign window (or in-progress claim) was preserved.
    """
    check = pid_alive_fn if pid_alive_fn is not None else pid_alive
    req = read_request(base)
    if req is None:
        try:
            st = request_path(base).stat()
        except OSError:
            # Request absent — a leftover grant is orphaned (grants are
            # run_id-bound and never authorize a later window): sweep it.
            _clear_grant(base)
            return True
        # Request EXISTS but is unparseable: an in-progress claim (fresh)
        # or a crashed-mid-claim leftover (old). See docstring.
        if time.time() - st.st_mtime <= CLAIM_WRITE_GRACE_SECONDS:
            return False
        logger.warning(
            "Qdrant handoff: clearing unparseable request file older than "
            "{:.0f}s (crashed mid-claim leftover)",
            CLAIM_WRITE_GRACE_SECONDS,
        )
        _clear_all(base)
        return True
    try:
        req_pid = int(req.get("pid"))
    except (TypeError, ValueError):
        req_pid = None
    if req_pid is not None and req_pid == os.getpid():
        _clear_all(base)
        return True
    alive = True
    if req_pid is not None and req_pid > 0:
        try:
            alive = bool(check(req_pid))
        except Exception:
            alive = True  # unverifiable → preserve (never clobber)
    if alive:
        return False
    logger.warning(
        "Qdrant handoff: clearing stale handshake files (owner pid {} is dead, run_id={})",
        req_pid, req.get("run_id"),
    )
    _clear_all(base)
    return True


def window_active(
    base: Optional[Path] = None,
    pid_alive_fn: Optional[Callable[[int], bool]] = None,
) -> bool:
    """True while a LIVE handoff window (request slot) is present — read-only.

    The reopen-gating complement of :func:`cleanup_stale` (round-6 review
    finding, P2): any code that would reopen the API's shared client outside
    the handoff watcher — e.g. ``IndexerManager._monitor``'s on-exit crash
    net — must check this first. An unconditional reopen can flip the API
    unblocked in the middle of a SUCCESSOR window granted back-to-back to a
    standalone exporter: payload writes then bypass their 503 and commit
    SQLite over a silently failed sync, and the API races the granted
    exporter for the embedded lock. Mirrors cleanup_stale's liveness rules
    WITHOUT removing anything:

    - request absent → False (no window; an orphaned grant authorizes
      nothing).
    - request exists but unparseable → True within
      CLAIM_WRITE_GRACE_SECONDS of its mtime (an in-progress claim), False
      past it (crashed-mid-claim leftover).
    - request owned by the calling process itself → False (our own
      leftovers are clearable, not a foreign window to defer to).
    - request pid unreadable or unverifiable → True (fail-safe: never race
      a window we cannot disprove).
    - otherwise → the recorded pid's liveness.
    """
    check = pid_alive_fn if pid_alive_fn is not None else pid_alive
    req = read_request(base)
    if req is None:
        try:
            st = request_path(base).stat()
        except OSError:
            return False
        return time.time() - st.st_mtime <= CLAIM_WRITE_GRACE_SECONDS
    try:
        req_pid = int(req.get("pid"))
    except (TypeError, ValueError):
        req_pid = None
    if req_pid is not None and req_pid == os.getpid():
        return False
    if req_pid is None or req_pid <= 0:
        return True
    try:
        return bool(check(req_pid))
    except Exception:
        return True


def _claim_request_slot(
    run_id: str,
    base: Optional[Path] = None,
    timeout: Optional[float] = None,
    poll: Optional[float] = None,
) -> bool:
    """Atomically claim the request slot (serializes concurrent windows).

    Each attempt runs cleanup_stale() — dead owners are reaped immediately;
    a LIVE foreign window is honored until it releases — then tries the
    exclusive create (_try_create_request). The create IS the claim, so
    two racing fresh requesters can never both proceed: the old wait-then-
    write left a TOCTOU window between the last slot poll and the request
    write in which the last writer replaced the first request (round-4
    review finding, P2). A successful create is verified by re-reading our
    own run_id before the claim is trusted.

    Returns True when the slot is ours, False when a foreign window
    outlived the budget — the caller must then fail loudly, never clobber
    (§3.1 step 5). OSErrors from the create (other than "already exists")
    propagate to the caller's degrade-to-no-handshake path.
    """
    timeout = SLOT_WAIT_TOTAL_SECONDS if timeout is None else timeout
    poll = GRANT_POLL_SECONDS if poll is None else poll
    deadline = time.monotonic() + timeout
    warned = False
    while True:
        if cleanup_stale(base):
            if _try_create_request(run_id, base):
                claimed = read_request(base)
                if claimed is not None and claimed.get("run_id") == run_id:
                    return True
                # Verification failed — the slot's content is not ours
                # (clobbered between create and read). Treat as busy and
                # re-enter the wait rather than proceed on a slot we
                # cannot prove we own.
        if not warned:
            owner = read_request(base) or {}
            logger.info(
                "Qdrant handoff: another process (pid={} run_id={}) holds the "
                "window — waiting up to {:.0f}s for it to release",
                owner.get("pid"), owner.get("run_id"), timeout,
            )
            warned = True
        if time.monotonic() >= deadline:
            return False
        time.sleep(poll)


def wait_for_grant(
    run_id: str,
    base: Optional[Path] = None,
    timeout: Optional[float] = None,
    poll: Optional[float] = None,
) -> bool:
    """Poll for a grant bearing our run_id. A grant with any other run_id is
    stale (crashed prior run) — it is deleted and never authorizes us."""
    # Late-bound so tests (and future tuning) can adjust the module constants.
    timeout = GRANT_WAIT_SECONDS if timeout is None else timeout
    poll = GRANT_POLL_SECONDS if poll is None else poll
    deadline = time.monotonic() + timeout
    while True:
        grant = read_grant(base)
        if grant is not None:
            if grant.get("run_id") == run_id:
                return True
            logger.warning(
                "Qdrant handoff: removing stale grant (run_id={} != ours {})",
                grant.get("run_id"), run_id,
            )
            # Internal raw clear is safe here: OUR request owns the slot, so
            # a grant bearing any other run_id is orphaned by construction.
            _clear_grant(base)
        if time.monotonic() >= deadline:
            return False
        time.sleep(poll)


@contextmanager
def handoff_window(
    timeout: Optional[float] = None,
    poll: Optional[float] = None,
    log_dir: Optional[Path] = None,
) -> Iterator[None]:
    """Indexer-side lock window: atomic slot claim → bounded grant wait
    → yield → owner-aware cleanup.

    ``log_dir`` is the caller's ACTIVE CONFIG ``cfg.log_dir`` — pass it so
    the sentinel dir is anchored to the same config the API watcher loads
    (see handoff_dir; round-5 review finding, P2) rather than to whichever
    config a fresh no-arg load would find.

    Everything that opens embedded Qdrant (version read, exporters, version
    record) must run inside the with-block. Cleanup in the finally routes
    through cleanup_stale(), which removes OUR files (so a crashed export
    can never wedge the API — the watcher sees the request disappear and
    reopens its client) but never a foreign live window's.

    Serialization (round-3, P2; claim-by-create round-4, P2): a stale slot
    — dead owner, or our own process's leftovers — is reaped before the
    claim; a LIVE foreign window is never clobbered. The claim itself is
    an atomic exclusive create (_claim_request_slot), so two fresh
    concurrent requesters can never both own the slot. We wait bounded for
    a foreign window to release, then raise HandoffSlotBusyError so the
    caller fails loudly (§3.1 step 5): two concurrent exporters serialize
    or fail, never interleave.

    With the kill switch off this is a no-op passthrough (the API kept
    close-at-start, so the lock is already free).
    """
    if not handoff_enabled():
        yield
        return
    base = handoff_dir(log_dir)
    run_id = uuid.uuid4().hex
    try:
        claimed = _claim_request_slot(run_id, base, poll=poll)
    except Exception as exc:
        logger.warning(
            "Qdrant handoff: could not write request file ({}); proceeding without handshake",
            exc,
        )
        yield
        return
    if not claimed:
        owner = read_request(base) or {}
        raise HandoffSlotBusyError(
            "another process (pid={} run_id={}) holds the Qdrant handoff window "
            "and did not release it within {:.0f}s".format(
                owner.get("pid"), owner.get("run_id"), SLOT_WAIT_TOTAL_SECONDS
            )
        )
    logger.info("Qdrant handoff: request written (run_id={} dir={})", run_id, base)
    try:
        if wait_for_grant(run_id, base, timeout=timeout, poll=poll):
            logger.info("Qdrant handoff: granted (run_id={})", run_id)
        else:
            logger.info(
                "Qdrant handoff: no grant within {:.0f}s — proceeding "
                "(no API attached, or its watcher is unavailable)",
                GRANT_WAIT_SECONDS if timeout is None else timeout,
            )
        yield
    finally:
        cleanup_stale(base)
        logger.info("Qdrant handoff: released (request/grant files removed)")


def is_lock_error(exc: BaseException) -> bool:
    """True for embedded-Qdrant lock contention ("already accessed by
    another instance")."""
    return "already accessed" in str(exc).lower()


def call_with_lock_retry(
    fn,
    total_seconds: Optional[float] = None,
    initial_delay: float = 1.0,
    max_delay: float = 8.0,
):
    """Call fn(); on embedded-lock contention retry with exponential backoff
    up to ``total_seconds`` (default LOCK_RETRY_TOTAL_SECONDS), then re-raise
    the lock error for the caller's loud-failure path. Non-lock exceptions
    propagate immediately.
    """
    total_seconds = LOCK_RETRY_TOTAL_SECONDS if total_seconds is None else total_seconds
    deadline = time.monotonic() + total_seconds
    delay = initial_delay
    while True:
        try:
            return fn()
        except Exception as exc:
            if not is_lock_error(exc):
                raise
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise
            sleep_for = min(delay, max_delay, remaining)
            logger.warning(
                "Qdrant lock held by another process — retrying in {:.1f}s "
                "({:.0f}s budget left): {}",
                sleep_for, remaining, exc,
            )
            time.sleep(sleep_for)
            delay = min(delay * 2, max_delay)
