"""``python -m app`` — the module the Tauri supervisor spawns (M-7/S-1 spec §1.2).

Order of operations (load-bearing):

  1. Derive the app-private root from ``sys.executable`` (``os.path.abspath``, not
     ``resolve`` — the venv python is a symlink to the uv base CPython).
  2. Export the ADR-009 ``MSA_*`` env so ``msa_settings`` resolves DataDir/config/logs at the
     **existing** user locations (user data is untouched — exit criterion #4), and publish
     ``MSA_TOOLS_DIR`` (the bundled exiftool/mediainfo dir) for the sidecar's PATH. Only the
     runtime (venv/python/uv-cache) is shell-owned, inside the app-private dir.
  3. Start the provisioning **responder** on ``SIDECAR_PORT`` (binds within ms, so the
     supervisor's ``wait_ready`` and the SPA's ``/health`` poll succeed immediately).
  4. Provision: downgrade guard → ``ensure_dependencies`` (fingerprint-gated uv installs) →
     config bootstrap → record the version. A failure switches the responder to a pollable
     error state (with the log path) and the shim stays alive serving it — no orphan.
  5. Stop the responder, wait for the port to free, and hand it to the real backend sidecar
     (``msa_apps.search_api.sidecar.run`` — importable now that the venv has MSA's deps).

Stdlib-only through step 4 (the venv has no MSA deps until ``ensure_dependencies`` runs).
"""

from __future__ import annotations

import importlib
import os
import signal
import sys
import threading
import time
from pathlib import Path

from app import applog, migration, provision
from app.responder import ProvisionStatus, Responder, wait_for_free_port

_WATCHDOG_INTERVAL_S = 2.0


# ── reap-safety: SIGTERM/SIGINT + parent-watchdog, installed BEFORE provisioning ─
#
# The success handoff (``sidecar.run``) and the failure fallback both install the reaper only
# *after* ``ensure_dependencies`` — but that call blocks for minutes on first run (≈2 GB torch
# via a blocking uv subprocess), and the failure fallback can't import ``msa_apps`` before a
# successful install, so it would sleep with no handler. A hard-killed / force-quit supervisor
# during that window orphans this shim and its ``uv`` child (PR #162, findings #1 + #3). These
# helpers are a STDLIB-ONLY mirror of ``msa_apps.search_api.sidecar``'s reaper so we can arm it
# at the very top of ``main()`` — independent of any ``msa_apps`` import — covering the whole
# provisioning window and the failure fallback. On the success path ``sidecar.run`` re-installs
# its own (idempotent: last signal registration wins; a second daemon watchdog is harmless).


def _supervisor_pid(env: dict[str, str]) -> int:
    """The supervisor's pid from ``SUPERVISOR_PID`` (0 when unset/blank ⇒ no watchdog)."""
    raw = (env.get("SUPERVISOR_PID") or "").strip()
    try:
        return int(raw) if raw else 0
    except ValueError:
        return 0


def _parent_alive(pid: int) -> bool:
    """Cross-platform liveness of ``pid`` (stdlib mirror of ``sidecar.parent_alive``): Windows
    uses ``OpenProcess``/``GetExitCodeProcess`` (never ``os.kill`` — that would terminate the
    supervisor and the venv sidecar is a two-process tree), POSIX uses ``os.kill(pid, 0)``.
    ``pid <= 0`` ⇒ no parent declared ⇒ treated as alive."""
    if pid <= 0:
        return True
    if os.name == "nt":
        import ctypes
        from ctypes import wintypes

        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        STILL_ACTIVE = 259
        kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
        handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not handle:
            return False  # cannot open ⇒ gone (or never existed)
        try:
            code = wintypes.DWORD()
            if not kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
                return False
            return code.value == STILL_ACTIVE
        finally:
            kernel32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False  # reaped — the supervisor is gone
    except PermissionError:
        return True  # exists but not ours to signal — still alive
    return True


def _supervisor_present(pid: int, expected_ppid: int) -> bool:
    """The watchdog's liveness check. Recycle-proof on POSIX: we start as the supervisor's
    **direct child**, so ``os.getppid()`` drifting off ``pid`` is an unfakeable "supervisor
    gone" (unlike ``os.kill(pid, 0)`` which a pid-reuse can fool). Falls back to a raw pid
    liveness probe (the sole check on Windows). Either signal failing ⇒ reap ourselves."""
    if pid <= 0:
        return True
    if os.name != "nt" and expected_ppid == pid and os.getppid() != pid:
        return False
    return _parent_alive(pid)


def _install_reaper(
    env: dict[str, str],
    *,
    exit_fn=os._exit,
    sleep=time.sleep,
    present=None,
    reap=None,
) -> threading.Thread | None:
    """Arm reap-safety BEFORE the minutes-long first-run provisioning: a non-deadlocking
    SIGTERM/SIGINT handler (→ reap the in-flight uv subtree, then ``exit_fn(0)``) and, when
    ``SUPERVISOR_PID`` is set, a daemon parent-watchdog that does the same the moment the
    supervisor is gone. ``os._exit`` never cascades to children, so both paths must first kill the
    in-flight ``uv`` install (its own session/process-group) or it orphans (PR #162 follow-on).
    Stdlib-only and independent of ``msa_apps``, so the whole provisioning window AND the
    error-hold fallback are orphan-safe. ``present``/``sleep``/``exit_fn``/``reap`` are injectable
    for the offline suite. Must run on the main thread (``signal.signal`` requirement)."""
    _reap = reap if reap is not None else provision.reap_active_child

    def _handler(_signum: int, _frame: object) -> None:
        _reap()  # kill the uv subtree first — os._exit(0) below won't cascade to it
        exit_fn(0)

    signal.signal(signal.SIGTERM, _handler)
    try:  # SIGINT may be unavailable in odd embeddings; never let that block startup
        signal.signal(signal.SIGINT, _handler)
    except (ValueError, OSError):  # pragma: no cover
        pass

    pid = _supervisor_pid(env)
    if pid <= 0:
        return None  # no supervisor declared (dev/test) — handler only, no watchdog

    expected_ppid = os.getppid()
    _present = present or (lambda: _supervisor_present(pid, expected_ppid))

    def _loop() -> None:
        while True:
            if not _present():
                sys.stderr.write(f"[shim] supervisor pid {pid} is gone - exiting\n")
                sys.stderr.flush()
                _reap()  # kill the uv subtree first — os._exit(0) below won't cascade to it
                exit_fn(0)
                return  # only reached if exit_fn is a test stub
            sleep(_WATCHDOG_INTERVAL_S)

    thread = threading.Thread(target=_loop, name="shim-parent-watchdog", daemon=True)
    thread.start()
    return thread


# ── ADR-009 platform directory map (stdlib mirror of msa_settings/config.py) ─


def _platform_dirs() -> dict[str, Path]:
    return provision.platform_dirs()  # single source of truth (shared with the headless entry)


def _resolved_dirs() -> dict[str, Path]:
    """Platform defaults with any pre-set ``MSA_*`` override respected. Delegates to
    ``provision.resolved_dirs`` so the shim, the sidecar, and ``python -m app.provision`` agree."""
    return provision.resolved_dirs()


def _export_env(dirs: dict[str, Path], tools_dir: Path) -> None:
    """Publish the ADR-009 env across the shim→sidecar boundary (defensive override; the
    defaults already agree). ``MSA_CONFIG_PATH`` is set later, once bootstrap has created the
    file (config.py rejects an MSA_CONFIG_PATH that points at a missing file)."""
    os.environ.setdefault("MSA_DATA_DIR", str(dirs["data"]))
    os.environ.setdefault("MSA_CACHE_DIR", str(dirs["cache"]))
    os.environ.setdefault("MSA_LOG_DIR", str(dirs["log"]))
    if tools_dir.is_dir():
        os.environ["MSA_TOOLS_DIR"] = str(tools_dir)


def main() -> None:
    port_raw = os.environ.get("SIDECAR_PORT")
    if not port_raw:
        sys.stderr.write(
            "SIDECAR_PORT is not set — `python -m app` is the desktop sidecar entry spawned by "
            "the Tauri supervisor; for local development run `msa api start` instead.\n"
        )
        raise SystemExit(2)
    port = int(port_raw)

    # Reap-safety FIRST — before the responder binds and before the minutes-long provisioning —
    # so a hard-killed / force-quit supervisor can't orphan this shim or its uv child at any
    # point (PR #162, findings #1 + #3). Stdlib-only; does not import msa_apps.
    _install_reaper(os.environ)

    dirs = _resolved_dirs()
    tools_dir = provision.resource_root() / "bin"
    _export_env(dirs, tools_dir)

    log_dir = dirs["log"]
    try:
        log_dir.mkdir(parents=True, exist_ok=True)
    except OSError:
        pass
    # Unified, rotating msa-desktop.log spanning the shim + (same process) uvicorn — the primary
    # troubleshooting artifact (spec §S-2.5). The per-run provision-<ts>.log below is unchanged
    # and coexists (full uv output for this run; it's the path the responder reports).
    desktop_log = applog.configure(log_dir)
    log = applog.logger()
    _install_thread_excepthook(log)  # no thread may die silently again (arch doc §10 risk #6)
    provision_log = log_dir / f"provision-{time.strftime('%Y%m%d-%H%M%S')}.log"
    log.info("shim starting (pid %d); data_dir=%s; desktop_log=%s", os.getpid(), dirs["data"], desktop_log)

    status = ProvisionStatus()
    status.set_stage("python", 0, "Preparing runtime", log=str(provision_log))
    responder = Responder(port, status)
    responder.start()
    log.info("responder up on 127.0.0.1:%d", port)
    print(f"[shim] responder up on 127.0.0.1:{port}; data_dir={dirs['data']}", flush=True)

    # Log each coarse stage transition into the unified log (finer pct emits are not logged, to
    # keep the file readable); the responder still gets every update for the SPA.
    last_stage = {"name": None}

    def on_stage(stage: str, pct: int, detail: str = "", log_field: str = "") -> None:
        status.set_stage(stage, pct, detail, log_field)
        if stage != last_stage["name"]:
            last_stage["name"] = stage
            log.info("provisioning stage=%s pct=%d %s", stage, pct, detail)

    def on_file(filename: str) -> None:
        # Feed the setup screen's rolling file list (the wheels uv is fetching). Not logged per-file
        # — the full uv output is already in the provision log; this is just the UI activity signal.
        status.push_file(filename)

    project = provision.staged_project_dir()
    version = provision.app_version(project)
    config_dir, data_dir = dirs["config"], dirs["data"]
    try:
        # Preflight BEFORE any download (spec §S-2.4). ARCH is always fatal (an unsupported CPU
        # can never run MSA), but the ≥5 GB DISK gate guards only the fresh/partial multi-GB
        # install — so skip it on the warm hot path where deps already match the fingerprint and
        # ensure_dependencies will no-op: a since-shrunk disk must not block a launch that
        # downloads nothing. When provisioning/resuming IS needed, the disk gate still runs and
        # surfaces the actionable error (the S-2 kill-resume DoD). On a RESUME the gate is SIZED to
        # the remaining stages: the heavy stages already recorded complete wrote their bytes on the
        # earlier run, so re-charging the full fresh threshold would reject a machine that barely fit
        # torch and now has adequate headroom for the small remainder (finding B). A FRESH install
        # (empty ledger) still demands the full budget.
        needs_provision = not provision.dependencies_complete(os.environ, project=project)
        # Belt-and-braces legacy migration on first run (the NSIS PREINSTALL hook is the primary
        # path on Windows; this also covers a direct-download install + the macOS artifacts NSIS
        # can't reach). Idempotent, best-effort, never blocks launch. Gated to first run
        # (needs_provision) so warm launches don't re-scan. Passes self_install_dir so it can never
        # delete the running Tauri app's own bin\/backend\ (migration.py collision guard).
        if needs_provision:
            _sweep_legacy_install(log)
        min_free_gb = provision.remaining_install_min_gb(os.environ, project=project)
        provision.preflight_system(check_disk=needs_provision, min_free_gb=min_free_gb)  # arch always; disk sized to remaining work
        provision.check_downgrade(version, data_dir / provision._VERSION_FILE)
        provision.ensure_dependencies(on_stage=on_stage, on_file=on_file, log_path=provision_log)
        provision.bootstrap_config(config_dir, project / provision._CONFIG_TEMPLATE)
        # config.yaml now exists → safe to pin MSA_CONFIG_PATH for the sidecar + diagnostics.
        os.environ["MSA_CONFIG_PATH"] = str(config_dir / "config.yaml")
        provision.write_version(data_dir / provision._VERSION_FILE, version)
    except Exception as e:  # provisioning is fail-loud; surface it in the responder, don't crash
        log.exception("provisioning failed")  # full traceback into the unified log
        sys.stderr.write(f"[shim] provisioning FAILED: {e}\n")
        status.fail(str(e), log=str(provision_log))
        _hold_error_state_inline()
        return

    # ── Pre-import the app WHILE the responder still owns /health (the first-launch fix) ──
    #
    # The riskiest step of the handoff is not the port swap — it is importing
    # msa_apps.search_api.app in the SAME interpreter that just installed everything. Two
    # confirmed failure classes here (2026-07-10/11 field incidents, arch doc §10 risk #6):
    # (1) .pth-dependent packages installed mid-process are invisible until site re-scans —
    #     pywin32/pywintypes; fixed deterministically by _refresh_site_packages below;
    # (2) any residual transient (e.g. a file briefly locked) — covered by the retries.
    # Importing here means a persistent failure lands in the responder's pollable error state
    # (splash shows the message + log path + Retry) instead of a silent dead /health, and
    # uvicorn's later config.load() finds the module in sys.modules — shrinking the /health
    # dead window at handoff from ~15-20 s (cold import) to ~1-2 s (bind + lifespan only).
    if needs_provision:
        _refresh_site_packages(log)
    on_stage("models-pending", 100, "Loading backend")
    if not _preload_app(log, status, provision_log):
        _hold_error_state_inline()
        return

    on_stage("models-pending", 100, "Starting backend")
    responder.stop()
    if not wait_for_free_port(port):
        log.warning("port %d did not free after responder stop", port)
        sys.stderr.write(f"[shim] port {port} did not free after responder stop\n")
    log.info("provisioning complete — handing port to uvicorn")
    print("[shim] provisioning complete — handing port to uvicorn", flush=True)

    from msa_apps.search_api.sidecar import run  # importable now (venv has MSA's deps)

    # Last mile. sidecar.run() normally never returns (it parks on uvicorn until SIGTERM →
    # os._exit). The app module is already pre-loaded above, so an exception here is a genuine
    # startup crash (bind exhaustion, lifespan failure) escaping _serve's own handling — log the
    # full traceback to the unified msa-desktop.log so it's diagnosable, then re-raise so the
    # process still exits non-zero (the supervisor reads that as a failure, not a graceful quit).
    try:
        run()
    except Exception:
        log.exception("backend handoff (sidecar.run) failed")
        raise


def _sweep_legacy_install(log) -> None:
    """Run the first-run legacy migration (belt-and-braces to the NSIS PREINSTALL hook), via the
    shared guarded sweep that the headless entry (``app.provision.headless_main``) also uses — one
    code path, all guards intact. Never raises — a migration hiccup must never block the app
    launch (``run_first_run_sweep`` swallows any unexpected error and returns ``None``)."""
    plan = migration.run_first_run_sweep(os.environ, log=lambda m: log.info("migration: %s", m))
    if plan is not None and not plan.is_empty():
        log.info(
            "legacy migration: %d fs target(s), %d skipped-for-safety",
            len(plan.fs_targets()), len(plan.skipped),
        )


_APP_MODULE = "msa_apps.search_api.app"


def _refresh_site_packages(log) -> None:
    """Make packages installed DURING this process importable — specifically the .pth-dependent
    ones. Python processes site-packages ``.pth`` files once, at interpreter startup; a cold
    first launch installs everything AFTER that moment, into this same interpreter, so a package
    whose importability rides on a ``.pth`` stays invisible until the next process. pywin32 is
    the textbook case: ``pywin32.pth`` adds ``win32/``, ``win32/lib/``, ``Pythonwin/`` to
    ``sys.path``, and without it ``import pywintypes`` fails — the ACTUAL first-launch killer
    (2026-07-11 field traceback: ``ModuleNotFoundError: No module named 'pywintypes'`` via
    qdrant_client → portalocker), and why a relaunch always "fixed" it (fresh interpreter →
    .pth processed at startup). ``site.addsitedir`` re-scans site-packages and processes its
    .pth files now, giving this interpreter the same ``sys.path`` a fresh one would have.
    Gated to the provisioning path by the caller; never raises."""
    import site
    import sysconfig

    paths = sysconfig.get_paths()
    seen = set()
    for key in ("purelib", "platlib"):
        d = paths.get(key)
        if d and d not in seen and os.path.isdir(d):
            seen.add(d)
            try:
                site.addsitedir(d)
            except Exception:  # noqa: BLE001 — a site rescan must never block the launch
                log.exception("site-packages re-scan failed for %s", d)
    log.info("site-packages re-scanned for .pth files (post-provision import visibility)")


def _preload_app(
    log,
    status: ProvisionStatus,
    provision_log: Path,
    *,
    attempts: int = 4,
    backoff_s: float = 2.0,
    sleep=time.sleep,
    import_module=None,
) -> bool:
    """Import the FastAPI app while the provisioning responder still owns ``/health``.

    The deterministic cold-launch failure class (.pth-invisible packages, e.g. pywintypes —
    the confirmed 2026-07-10/11 field incidents) is fixed upstream by ``_refresh_site_packages``;
    the retries here cover genuine transients (e.g. a file briefly locked): each failed attempt
    logs the FULL traceback into the unified log, then backs off ``backoff_s * 2**n`` (2/4/8 s).
    A persistent failure flips the responder to its pollable error state (message + the
    provision-log path) and returns False — the splash shows the error with open-logs + Retry
    instead of freezing on a dead ``/health``. On True, the module sits in ``sys.modules`` so
    uvicorn's ``config.load()`` is instant and the handoff dead window is bind + lifespan only."""
    do_import = import_module or importlib.import_module
    last: Exception | None = None
    for attempt in range(1, attempts + 1):
        try:
            do_import(_APP_MODULE)
        except Exception as exc:  # noqa: BLE001 — every failure is logged with its traceback
            last = exc
            log.exception("backend pre-load failed (attempt %d/%d) — %s", attempt, attempts, _APP_MODULE)
            if attempt < attempts:
                status.set_stage(
                    "models-pending", 100, f"Loading backend (retry {attempt}/{attempts - 1})"
                )
                sleep(backoff_s * 2 ** (attempt - 1))
            continue
        if attempt > 1:
            log.info("backend pre-load succeeded on attempt %d/%d", attempt, attempts)
        return True
    status.fail(f"Backend failed to load: {last}", log=str(provision_log))
    sys.stderr.write(f"[shim] backend pre-load FAILED: {last}\n")
    return False


def _install_thread_excepthook(log) -> None:
    """Route ANY thread's uncaught exception through the unified log (``msa-desktop.log``).

    Python's default hook prints the traceback to raw stderr, which nothing captures in the
    packaged GUI app — the exact mechanism that made the 2026-07-10 first-launch failure
    invisible (architecture doc §5 "What reaches msa-desktop.log"). Chains to the previous hook
    so dev runs keep the stderr traceback too. Both paths are guarded: logging must never take
    the process down."""
    prev = threading.excepthook

    def _hook(args, _prev=prev):
        try:
            log.error(
                "uncaught exception in thread %r",
                getattr(args.thread, "name", None) or "?",
                exc_info=(args.exc_type, args.exc_value, args.exc_traceback),
            )
        except Exception:  # noqa: BLE001
            pass
        try:
            _prev(args)
        except Exception:  # noqa: BLE001
            pass

    threading.excepthook = _hook


def _hold_error_state_inline() -> None:
    """Keep the process alive serving the responder's error payload after a provisioning failure.
    Reap-safety (SIGTERM/SIGINT + parent-watchdog) was already armed at the top of ``main()``
    (``_install_reaper``, stdlib-only, before ``ensure_dependencies``), so this path is
    orphan-safe WITHOUT importing ``msa_apps`` — which isn't on ``sys.path`` until a successful
    install anyway, the exact gap that used to leave this fallback watchdog-less (PR #162,
    finding #3). Block until the supervisor's SIGTERM or the watchdog tears us down."""
    while True:
        time.sleep(3600)


if __name__ == "__main__":
    main()
