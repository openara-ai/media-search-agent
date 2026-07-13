// Prevents an extra console window on Windows in release. DO NOT REMOVE.
#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

//! TEMPLATE-OWNED (vendored): the Tauri shell + sidecar supervisor. Implements the
//! language-agnostic sidecar contract from DESKTOP_APP_TEMPLATE_DESIGN.md §3:
//!
//!   every sidecar must:  (1) take a port  (2) signal ready  (3) exit cleanly
//!                        (4) answer CORS preflight  (5) bind 127.0.0.1
//!
//! Python backend = bundled-uv provisioning (design §3a), NOT PyInstaller. The shell:
//!   - reads the project's `app.config.json` (the ONE integration file),
//!   - on first run, PROVISIONS a Python runtime with the bundled `uv`: a standalone
//!     CPython + a venv, with uv's install/cache dirs pointed INTO the app-private data
//!     dir so the interpreter is app-owned (Tier-1 uninstall — UNINSTALL_AND_LIFECYCLE §3),
//!   - assigns each bundled sidecar an ephemeral port (design open-Q #2),
//!   - spawns `…/.venv/bin/python -m app` handing it the port via SIDECAR_PORT (contract #1),
//!   - injects the resolved API base URL into the WebView (design §5: location-agnostic),
//!   - runs a readiness handshake against the health endpoint (contract #2),
//!   - tears every sidecar down on exit (contract #3).
//!
//! It knows NOTHING about the backend language — a sidecar is just "a process that serves
//! HTTP on a port." A venv Python (-m app), a Rust binary, anything, satisfies the contract.

use std::fs;
use std::io::{Read, Write};
use std::net::{TcpListener, TcpStream};
use std::path::{Path, PathBuf};
use std::sync::Mutex;
use std::time::{Duration, Instant};

use serde::Deserialize;
use tauri::{Emitter, Manager, RunEvent, WebviewUrl, WebviewWindowBuilder};
use tauri_plugin_shell::process::{CommandChild, CommandEvent};
use tauri_plugin_shell::ShellExt;

// ---------------------------------------------------------------------------
// app.config.json — the ONLY project→shell integration surface (read at compile time).
// ---------------------------------------------------------------------------
const APP_CONFIG: &str = include_str!("../../app.config.json");

// Python version the bundled-uv adapter provisions (design §3a). One pin, fleet-wide.
const PYTHON_VERSION: &str = "3.12";

// Cross-platform fork points (SHARED supervisor — SPIKE_WINDOWS §0a). The macOS spike
// validated the `not(windows)` branches; the `windows` branches are pre-seamed for the
// Windows spike to verify (marked WINDOWS-SPIKE).
#[cfg(windows)]
const UV_BIN_NAME: &str = "uv.exe";
#[cfg(not(windows))]
const UV_BIN_NAME: &str = "uv";

/// The venv interpreter path differs by OS: `Scripts\python.exe` (Windows) vs
/// `bin/python3` (POSIX). uv lays the venv out the same way the stdlib `venv` does.
fn venv_python(venv_dir: &Path) -> PathBuf {
    #[cfg(windows)]
    {
        venv_dir.join("Scripts").join("python.exe")
    }
    #[cfg(not(windows))]
    {
        venv_dir.join("bin").join("python3")
    }
}

#[derive(Deserialize)]
struct AppConfig {
    app: AppMeta,
    sidecars: Vec<Sidecar>,
}

#[derive(Deserialize)]
struct AppMeta {
    name: String,
    window: WindowCfg,
}

fn default_true() -> bool {
    true
}

#[derive(Deserialize)]
struct WindowCfg {
    width: f64,
    height: f64,
    // Tauri's webview enables an OS-level drag-and-drop handler by default (delivers files dropped
    // onto the window, with their absolute path) — but it is MUTUALLY EXCLUSIVE with in-page HTML5
    // drag-and-drop (draggable reorder, sortable lists), which it silently swallows on BOTH WKWebView
    // and WebView2. Default ON preserves Tauri's native behavior; an app that uses in-page DnD sets
    // `"os_file_drop": false`. Omitted in app.config.json ⇒ true (default_true).
    #[serde(default = "default_true")]
    os_file_drop: bool,
}

#[derive(Deserialize)]
struct Sidecar {
    name: String,
    kind: String, // "bundled" | "external"
    #[serde(default)]
    adapter: Option<String>, // "python" | "rust" | "ollama" | …
    ready_path: String,
}

// ---------------------------------------------------------------------------
// Supervisor primitives (the part the template should ship generically).
// ---------------------------------------------------------------------------

/// Supervisor-assigned ephemeral port — binds :0, lets the OS pick a free port, releases.
fn free_port() -> u16 {
    TcpListener::bind("127.0.0.1:0")
        .expect("could not bind an ephemeral port")
        .local_addr()
        .unwrap()
        .port()
}

/// Contract obligation #2: poll the sidecar's readiness endpoint until it returns 200.
/// A minimal raw-HTTP GET so the shell needs no HTTP-client dependency at all.
fn wait_ready(port: u16, path: &str, timeout: Duration) -> bool {
    let deadline = Instant::now() + timeout;
    let req = format!("GET {path} HTTP/1.0\r\nHost: 127.0.0.1\r\nConnection: close\r\n\r\n");
    while Instant::now() < deadline {
        if let Ok(mut stream) = TcpStream::connect(("127.0.0.1", port)) {
            let _ = stream.set_read_timeout(Some(Duration::from_millis(800)));
            if stream.write_all(req.as_bytes()).is_ok() {
                let mut buf = String::new();
                let _ = stream.read_to_string(&mut buf);
                if buf.starts_with("HTTP/1.0 200") || buf.starts_with("HTTP/1.1 200") {
                    return true;
                }
            }
        }
        std::thread::sleep(Duration::from_millis(150));
    }
    false
}

/// Handles to spawned sidecars so we can tear them down on exit (contract #3).
struct Sidecars(Mutex<Vec<CommandChild>>);

// ---------------------------------------------------------------------------
// Bundled-uv provisioning (design §3a). TEMPLATE-OWNED: the Python build adapter's
// runtime half. Every Python project in the fleet provisions identically; only the
// entry module differs. This is the work that replaces PyInstaller entirely.
// ---------------------------------------------------------------------------

/// Where the app's interpreter, venv, uv-cache, and the extracted `uv` live. App-owned,
/// inside the per-user data dir, so a single dir removal is a complete Tier-1 uninstall of
/// the runtime (UNINSTALL_AND_LIFECYCLE §3). NOT inside the .app bundle: the bundle is
/// code-signed (writing into it breaks the signature) and the updater replaces it wholesale.
fn app_private_dir(app: &tauri::AppHandle) -> PathBuf {
    // app_local_data_dir() is the cross-platform-correct choice: macOS →
    // ~/Library/Application Support/<id> (same as app_data_dir there), Windows →
    // %LOCALAPPDATA%\<id> (per INSTALLER §2, NOT roaming %APPDATA%). One call, both right.
    app.path()
        .app_local_data_dir()
        .expect("no app data dir")
}

/// Make a copy of the bundled `uv` inside the app-private dir and mark it executable.
/// "Shipped inside the bundle, extracted into the app-private dir" (UNINSTALL §2): the
/// extracted copy is app-owned and removed with the data dir, and we never chmod inside
/// the signed bundle. Returns the path to the runnable uv.
fn extract_uv(app: &tauri::AppHandle, priv_dir: &Path) -> Result<PathBuf, String> {
    let bundled = app
        .path()
        .resolve(format!("bin/{UV_BIN_NAME}"), tauri::path::BaseDirectory::Resource)
        .map_err(|e| format!("cannot resolve bundled bin/{UV_BIN_NAME}: {e}"))?;
    let dest_dir = priv_dir.join("bin");
    fs::create_dir_all(&dest_dir).map_err(|e| format!("mkdir {dest_dir:?}: {e}"))?;
    let dest = dest_dir.join(UV_BIN_NAME);
    // Copy fresh if missing or stale-by-size (cheap; uv is one self-contained binary).
    let need_copy = match (fs::metadata(&dest), fs::metadata(&bundled)) {
        (Ok(d), Ok(b)) => d.len() != b.len(),
        _ => true,
    };
    if need_copy {
        fs::copy(&bundled, &dest).map_err(|e| format!("copy uv -> {dest:?}: {e}"))?;
    }
    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;
        let _ = fs::set_permissions(&dest, fs::Permissions::from_mode(0o755));
    }
    Ok(dest)
}

/// Run a `uv` subcommand with the install/cache dirs pinned into the app-private dir, so
/// the standalone CPython lands app-owned (the §3a discipline + UNINSTALL §3). Blocks until
/// it finishes; logs stdout/stderr. Returns Err on a non-zero exit.
fn run_uv(uv: &Path, priv_dir: &Path, args: &[&str]) -> Result<(), String> {
    let out = std::process::Command::new(uv)
        .args(args)
        // Point uv's standalone-CPython install dir + cache INTO the app-private dir.
        // This is THE line that makes the interpreter Tier-1 (app-owned), not an orphan
        // in ~/.local/share/uv. (The MSA reference omits this — see FRICTION_LOG.)
        .env("UV_PYTHON_INSTALL_DIR", priv_dir.join("python"))
        .env("UV_CACHE_DIR", priv_dir.join("uv-cache"))
        // Never read/write the user's system Python config (design §3a discipline).
        .env("UV_NO_CONFIG", "1")
        // Modern uv (>=0.5) ALSO drops a `python3.x` launcher shim into the user's bin dir
        // (~/.local/bin on Windows/POSIX) on `uv python install` — OUTSIDE the app-private dir,
        // so it orphans on uninstall and writes user space the redirect was meant to avoid.
        // The macOS spike ran uv 0.5.21 and only checked ~/.local/share/uv (§F2); on Windows
        // with uv 0.11 the shim is real and visible. Suppress it so the interpreter stays
        // wholly app-owned (Tier-1). Env var (not the `--no-bin` flag) so older uv that lacks
        // the option silently ignores it instead of erroring — keeps this fork-free + macOS-safe.
        .env("UV_PYTHON_INSTALL_BIN", "0")
        .output()
        .map_err(|e| format!("spawn uv {args:?}: {e}"))?;
    let so = String::from_utf8_lossy(&out.stdout);
    let se = String::from_utf8_lossy(&out.stderr);
    for line in so.lines().chain(se.lines()) {
        eprintln!("[provision:uv] {line}");
    }
    if out.status.success() {
        Ok(())
    } else {
        Err(format!("uv {args:?} exited {:?}", out.status.code()))
    }
}

/// First-run provisioning (design §3a). Idempotent: if the venv interpreter already exists,
/// returns immediately (subsequent launches are fast — no 2–4 s onefile self-extract).
/// Otherwise: extract uv → `uv python install` → `uv venv`. The upstream
/// template's engine is pure-stdlib, so there is no `uv pip install` step (design §3a: "needs none of it").
/// Returns the path to the venv Python to spawn.
fn provision_python(app: &tauri::AppHandle) -> Result<PathBuf, String> {
    let priv_dir = app_private_dir(app);
    fs::create_dir_all(&priv_dir).map_err(|e| format!("mkdir {priv_dir:?}: {e}"))?;
    let venv_py = venv_python(&priv_dir.join(".venv"));

    if venv_py.exists() {
        eprintln!("[provision] venv present — skipping (fast path): {venv_py:?}");
        return Ok(venv_py);
    }

    eprintln!("[provision] FIRST RUN — provisioning Python {PYTHON_VERSION} via bundled uv");
    let _ = app.emit("sidecar-provisioning", "backend");
    let uv = extract_uv(app, &priv_dir)?;
    // Standalone CPython INTO the app-private install dir (env set in run_uv).
    run_uv(&uv, &priv_dir, &["python", "install", PYTHON_VERSION])?;
    // Create the venv from that interpreter. --python <ver> resolves the app-private one.
    let venv_path = priv_dir.join(".venv");
    run_uv(
        &uv,
        &priv_dir,
        &["venv", venv_path.to_str().unwrap(), "--python", PYTHON_VERSION],
    )?;

    if !venv_py.exists() {
        return Err(format!("venv created but interpreter missing at {venv_py:?}"));
    }
    eprintln!("[provision] done — interpreter at {venv_py:?}");
    Ok(venv_py)
}

fn main() {
    let cfg: AppConfig = serde_json::from_str(APP_CONFIG).expect("invalid app.config.json");

    tauri::Builder::default()
        .plugin(tauri_plugin_shell::init())
        // Updater plugin registered but INERT: it makes no network call unless `.check()` is
        // invoked, and nothing invokes it automatically (no launch-time check — see setup()).
        // Kept registered as the seam for a future user-initiated "Check for updates" action.
        .plugin(tauri_plugin_updater::Builder::new().build())
        .manage(Sidecars(Mutex::new(Vec::new())))
        .setup(move |app| {
            let handle = app.handle().clone();
            let mut api_base = String::new();

            // INVARIANT — no automatic update check / no phone-home (ADR-012): the shell makes
            // NO unsolicited network request at launch. Updates are user-initiated. The updater
            // plugin registered above stays inert (no network call unless `.check()` runs), so a
            // future user-clicked "Check for updates" can drive it from an explicit command.
            // Do NOT reintroduce a launch-time check here. Guard: tests/test_updater_no_auto_check.py.

            // The backend Python source ships as a bundle resource; `-m app` finds it via
            // PYTHONPATH. (Read-only in the signed bundle — PYTHONDONTWRITEBYTECODE avoids
            // any attempt to write .pyc back into the sealed bundle.)
            let backend_pythonpath = app
                .path()
                .resolve("backend", tauri::path::BaseDirectory::Resource)
                .map(|p| p.to_string_lossy().to_string())
                .unwrap_or_default();

            for sc in &cfg.sidecars {
                if sc.kind == "external" {
                    // External sidecar (e.g. the LLM runtime): DETECT, don't spawn
                    // (design §3 / §6.3). The backend mediates it; the shell does nothing.
                    eprintln!("[shell] sidecar '{}' is EXTERNAL — backend detects/provisions it", sc.name);
                    continue;
                }

                let port = free_port(); // ephemeral, per sidecar
                let url_base = format!("http://127.0.0.1:{port}");
                if sc.name == "backend" {
                    api_base = url_base.clone();
                }

                // Provision + spawn happen off the UI thread: provisioning can take seconds
                // (first run) and must not block the window. The window comes up immediately
                // showing a "setting up…/starting…" state; the SPA polls /health.
                let app_h = handle.clone();
                let name = sc.name.clone();
                let ready_path = sc.ready_path.clone();
                let adapter = sc.adapter.clone().unwrap_or_default();
                let pp = backend_pythonpath.clone();
                std::thread::spawn(move || {
                    // 1) Bundled-uv provisioning (Python adapter only).
                    let venv_py = if adapter == "python" {
                        match provision_python(&app_h) {
                            Ok(p) => p,
                            Err(e) => {
                                eprintln!("[shell] provisioning FAILED for '{name}': {e}");
                                let _ = app_h.emit("sidecar-failed", &name);
                                return;
                            }
                        }
                    } else {
                        eprintln!("[shell] adapter '{adapter}' not supported in this spike");
                        let _ = app_h.emit("sidecar-failed", &name);
                        return;
                    };

                    // 2) Sidecar-launch shape (design §3a): the venv Python on the entry
                    //    module. Contract #1: hand it the port via SIDECAR_PORT; also our
                    //    PID so it can self-terminate if we die (no orphans).
                    let command = app_h
                        .shell()
                        .command(venv_py.to_string_lossy().to_string())
                        .args(["-m", "app"])
                        .env("PYTHONPATH", &pp)
                        .env("PYTHONDONTWRITEBYTECODE", "1")
                        .env("PYTHONUNBUFFERED", "1")
                        .env("SIDECAR_PORT", port.to_string())
                        .env("SUPERVISOR_PID", std::process::id().to_string());

                    let (mut rx, child) = match command.spawn() {
                        Ok(v) => v,
                        Err(e) => {
                            eprintln!("[shell] sidecar '{name}' spawn FAILED: {e:?}");
                            let _ = app_h.emit("sidecar-failed", &name);
                            return;
                        }
                    };
                    app_h.state::<Sidecars>().0.lock().unwrap().push(child);
                    eprintln!("[shell] spawned '{name}' = venv python -m app on port {port}");

                    // Drain the sidecar's stdout/stderr into the shell log.
                    let log_name = name.clone();
                    tauri::async_runtime::spawn(async move {
                        while let Some(event) = rx.recv().await {
                            match event {
                                CommandEvent::Stdout(l) | CommandEvent::Stderr(l) => {
                                    eprintln!("[{log_name}] {}", String::from_utf8_lossy(&l).trim_end());
                                }
                                CommandEvent::Terminated(p) => {
                                    eprintln!("[{log_name}] terminated: {:?}", p.code);
                                }
                                _ => {}
                            }
                        }
                    });

                    // 3) Contract #2: readiness. A venv interpreter starts in ~100 ms (no
                    //    onefile self-extract), but first-run provisioning above can add
                    //    seconds, so the budget is generous.
                    let t0 = Instant::now();
                    if wait_ready(port, &ready_path, Duration::from_secs(120)) {
                        eprintln!("[shell] '{name}' READY in {} ms", t0.elapsed().as_millis());
                        let _ = app_h.emit("sidecar-ready", &name);
                    } else {
                        eprintln!("[shell] '{name}' FAILED to become ready");
                        let _ = app_h.emit("sidecar-failed", &name);
                    }
                });
            }

            // Design §5 (the one rule): inject the resolved API base URL. The SPA reads
            // window.__API_BASE__ and NEVER hard-codes localhost. The port is known the
            // moment it's assigned, so we inject immediately — no need to block on readiness.
            let init = format!(
                "window.__API_BASE__ = {api_base:?}; window.__APP_NAME__ = {:?}; window.__APP_VERSION__ = {:?};",
                cfg.app.name,
                env!("CARGO_PKG_VERSION")
            );
            let mut builder = WebviewWindowBuilder::new(app, "main", WebviewUrl::App("index.html".into()))
                .title(&cfg.app.name)
                .inner_size(cfg.app.window.width, cfg.app.window.height)
                .initialization_script(&init);
            // Opt out of Tauri's OS-level drag-drop handler when the app uses in-page HTML5 DnD
            // (see WindowCfg::os_file_drop). Leaving the handler on (the default) silently breaks
            // `draggable` reorder / sortable lists on both WKWebView and WebView2 — found by the
            // first consumer (a drag-to-reorder / sortable list UI).
            if !cfg.app.window.os_file_drop {
                builder = builder.disable_drag_drop_handler();
            }
            builder.build()?;
            eprintln!("[shell] window up — injected API base {api_base}");
            Ok(())
        })
        .build(tauri::generate_context!())
        .expect("error while building the Tauri application")
        .run(|app, event| {
            // Contract #3: terminate every sidecar on exit (clean teardown).
            if let RunEvent::ExitRequested { .. } = event {
                let children = std::mem::take(&mut *app.state::<Sidecars>().0.lock().unwrap());
                for child in children {
                    // The sidecar is now a SINGLE process (the venv interpreter), not a
                    // PyInstaller bootloader+forked-child pair — so SIGTERM reaches it
                    // directly and an orphaned onefile child cannot occur.
                    // Still prefer SIGTERM so the backend's handler runs (os._exit(0));
                    // the sidecar's parent-watchdog is the backstop if this handler is
                    // skipped entirely (a hard quit that bypasses RunEvent::ExitRequested).
                    #[cfg(unix)]
                    let sent = {
                        let pid = child.pid();
                        std::process::Command::new("/bin/kill")
                            .arg("-TERM")
                            .arg(pid.to_string())
                            .status()
                            .map(|s| s.success())
                            .unwrap_or(false)
                    };
                    // WINDOWS-SPIKE (§2.2 teardown): Windows has no SIGTERM, so we fall
                    // through to child.kill() (TerminateProcess) — which does NOT run the
                    // backend's signal handler. For the single-process venv sidecar that's
                    // fine (stateless HTTP; no bootloader child to orphan), and the backend's
                    // parent-watchdog is the backstop. VERIFY on Windows: no orphaned
                    // python.exe after quit; if a future sidecar spawns its own children,
                    // consider `taskkill /T /PID` for a tree-kill instead of child.kill().
                    #[cfg(not(unix))]
                    let sent = false;
                    if !sent {
                        let _ = child.kill(); // fallback (non-unix, or /bin/kill missing)
                    }
                }
                eprintln!("[shell] all sidecars torn down");
            }
        });
}
