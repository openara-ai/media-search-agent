# ADR-012: Desktop Shell (Tauri) — Supervisor + Sidecar

Status: Accepted (desktop-shell work in progress; the initial increment landed the shell + backend/SPA sidecar contract).

Supersedes in part: [ADR-004](ADR-004-installer-design.md) (macOS Platypus menu-bar app
and the Windows PowerShell launcher/tray are replaced by the Tauri shell on macOS and
Windows; the Linux shell-bundle path is retained).

Relates to: [ADR-003](ADR-003-python-uv.md) (bundled `uv` provisioning),
[ADR-005](ADR-005-uninstall-policy.md) (tiered uninstall),
[ADR-009](ADR-009-runtime-path-ownership-and-naming.md) (path ownership — amended by this
work).

## Context

MSA renders in a browser tab the app does not control, and the one-line installer does
*everything* (download, venv, torch, config, tray, auto-start) in a terminal before the
user sees anything. That posture carried three native UI codebases in three languages (a
Swift menu-bar app, a .NET tray, a WinForms splash) purely to bridge "installed" to
"browser open", plus fixed-port fragility and browser-handoff choreography.

A Rust supervisor hosting a native window replaces all of that: one process that owns the
window, spawns and supervises the Python sidecar, and resolves a free port. (The design is
validated by a reference spike and by a sibling FastAPI + uvicorn + Vite/React product in
production.) MSA uses it for macOS and Windows.

## Decision

### 1. Supervisor + sidecar

The shell is a **supervisor** (Rust) that owns a native window and the lifecycle
of a **sidecar** (a process that serves HTTP on a loopback port). The shell knows nothing
about Python or MSA. The **sidecar contract** is five obligations: take a port
(`SIDECAR_PORT`), signal ready (HTTP 200 on the config's `ready_path`, MSA's `/health`),
exit cleanly (SIGTERM → prompt exit), answer CORS preflight, bind `127.0.0.1` only.

- **Python packaging** is bundled `uv` → CPython 3.12 → app-local venv at first run — no
  PyInstaller. The interpreter is a child process, not `dlopen`ed, sidestepping macOS
  Hardened-Runtime / Library-Validation entirely (ADR-003 concept unchanged).
- **Ports are supervisor-assigned and ephemeral**, eliminating the port-conflict class.
  The `msa` CLI / browser mode keep the fixed `config.yaml` `api.port`.
- **The React `dist/` is embedded** in the webview, so the SPA renders provisioning
  progress without the backend up.
- **First-run readiness** is solved by a **provisioning responder**: a stdlib HTTP server
  the shim binds on `SIDECAR_PORT` within milliseconds, serving `/health` with
  `status=provisioning` + stage + pct until uvicorn takes the same port. This neutralizes
  the supervisor's 120 s readiness budget vs MSA's multi-gigabyte first run, with zero
  change to the Rust shell.

### 2. The two seams

- **`app.config.json`** (repo root) is the ONE project→shell integration file — name,
  identifier, window size, `spa_dir`, sidecar list. It is compiled into the supervisor via
  `include_str!`.
- **`window.__API_BASE__`** is the ONE frontend seam. The supervisor injects the ephemeral
  origin; the SPA routes every fetch/WebSocket/asset URL through `apiUrl()` / `wsUrl()`
  (`src/msa_apps/ui/src/lib/apiBase.ts`). An empty-string fallback keeps plain browser /
  dev mode byte-identical.

### 3. The shell is a generic unit; MSA authors ~zero Rust

`src-tauri/` (the Rust supervisor, `Cargo.toml`/`.lock`, `build.rs`, `capabilities/`),
`scripts/build-backend.{sh,ps1}`, `scripts/build-app.{sh,ps1}`, the baseline
`packaging/windows/installer-hooks.nsh`, and the root Tauri `package.json` form a
**self-contained shell unit**, deliberately kept generic: it knows nothing about Python or
MSA, and MSA authors ~zero Rust in it. Its provenance and any local divergence are recorded
in `src-tauri/.template-version`.

**Project-owned / instantiated files** (edited for MSA, kept in sync manually):
`app.config.json`, `src-tauri/tauri.conf.json`, `src-tauri/tauri.windows.conf.json`,
`src-tauri/icons/`, the provisioning shim `src-tauri/backend/app/`, the backend sidecar
entry, the thin bootstraps, and the release-workflow changes.

**Divergence policy.** Keeping the shell unit generic is a *guideline*, not a contract: MSA
may hand-edit files in it when the generic baseline does not fit or would block progress. The
discipline that remains is small — record each divergence in `src-tauri/.template-version` so
it is explainable later, and prefer a generic fix for changes any consumer of the shell would
want (better, but not a precondition for fixing MSA).

Project-identity values are set only where they matter and deferred otherwise. Known
deviations: the crate/package name is `media-search-agent` (the crate name is the default
binary name — an earlier build shipped under the wrong executable name), while `Cargo.toml`'s
version stays at its `0.1.0` default (the user-visible version comes from `tauri.conf.json`
and the backend, not the crate — see §4). The bundle name + identifier come from
`tauri.conf.json` (`MediaSearchAgent` / `ai.openara.mediasearchagent`). Backend staging is the
project-owned `scripts/stage-desktop-backend.sh` (backend source tree + optional wheels +
exiftool/mediainfo + config template), not the generic `build-backend.sh` stub.

### 4. Instantiation values (the desync-risk surface)

`tauri.conf.json`, `app.config.json`, and `Cargo.toml` duplicate values that are not yet
single-sourced. The authoritative table:

| Field | Value | Where |
|---|---|---|
| Product / window title | `MediaSearchAgent` | `tauri.conf.json productName`, `app.config.json app.name` |
| Bundle identifier | `ai.openara.mediasearchagent` | `tauri.conf.json identifier`, `app.config.json app.identifier` |
| Frontend dist | `../src/msa_apps/ui/dist` | `tauri.conf.json build.frontendDist`, `app.config.json spa_dir` |
| Window | 1440 × 900 | `app.config.json app.window` |
| Sidecar ready path | `/health` | `app.config.json sidecars[0].ready_path` |
| App version | git-tag-stamped `X.Y.Z` (dev placeholder `0.0.0`) | `tauri.conf.json version`, stamped by `stage-desktop-backend.sh` |
| macOS min OS | `12.0`, `signingIdentity "-"` | `tauri.conf.json bundle.macOS` |
| Updater endpoint | `github.com/openara-ai/media-search-agent/releases/latest/download/latest.json` | `tauri.conf.json plugins.updater` |
| Windows target | `nsis`, `currentUser`, `installer-hooks.nsh` | `tauri.windows.conf.json` |

A version-consistency preflight (tag == `tauri.conf.json` == `pyproject` pep440 ==
CHANGELOG) **landed as a `release.yml` guard**. It is placeholder-aware: both
manifests are git-tag-stamped at build time by `stage-desktop-backend.sh`, so the committed
`0.0.0` / `0.0.0.dev0` placeholders pass, while a *hard-coded* version that disagrees with the
tag fails. The same step asserts the updater endpoint targets the public repo.

**Consequence — do not read `window.__APP_VERSION__` for user-facing version display.** The
preflight and stamping cover `tauri.conf.json`, *not* `src-tauri/Cargo.toml`. But the supervisor
injects `window.__APP_VERSION__` from `env!("CARGO_PKG_VERSION")` (Cargo.toml), which stays at its
`0.1.0` default in every packaged build — it is a deferred project-identity value (§3), not a
stamped one. UI that shows the app version must therefore source it from the backend `app_version`
(`GET /diagnostics` → `_APP_VERSION`, git-tag-derived via `importlib.metadata`), which is correct in
both shell and browser mode. This bit the Settings › About section: preferring the injected
seam would have shown `0.1.0` on every release. If a future change stamps `Cargo.toml` at build (or
folds it into the release preflight), the seam becomes trustworthy and this note can retire.

### 5. No automatic updates

The shell does not check for or install updates on its own — it makes no unsolicited network
request at launch. MSA is a local-first appliance; update timing is the user's decision. To
update, the user installs a newer release the same way as the first install (macOS/Windows:
over the current version; Linux: re-run the installer).

The Tauri updater plugin is configured but inert — registered so a future *user-initiated*
"Check for updates" can use it, and it verifies downloads against a minisign public key in
`tauri.conf.json` (a fail-closed placeholder until a real keypair is generated). Two build
constraints keep key-less contributor builds working: `createUpdaterArtifacts` is committed
`false` (the release pipeline forces it `true` and injects the signing key only on the
publishing repos), and the `plugins` block is never empty (an empty block panics the app at
launch). This signing / `latest.json` machinery is dormant until the manual check is wired.

### 6. Path ownership

User data is untouched (see the ADR-009 amendment): `config.yaml`, `index/`, thumbnails,
and logs stay in the existing per-user ADR-009 locations. Only the **runtime**
(venv/CPython/uv-cache/extracted `uv`) moves into the shell-owned app-private dir, keyed by
bundle identifier, so a single directory removal is a complete Tier-1 uninstall of the
runtime. The provisioning shim exports the ADR-009 `MSA_*` env so `msa_settings` resolves
the user directories exactly as before.

### 7. First-run experience

The first run installs ≈3.5 GB (torch + models), so it needs an honest, resumable,
unattended-safe path from double-click to searchable UI:

- **Preflight, then download.** `provision.preflight_system()` runs a cheap go/no-go gate
  *before* any bytes move: supported arch (Apple Silicon on macOS, x86_64 on Windows) and
  ≥5 GB free on the install volume, surfaced as an actionable responder error — no
  half-downloaded 2 GB torch on a machine that can't run it.
- **Two-tier resumable ledger.** `.venv/.msa-deps.json` keeps the existing top-level `fingerprint`
  marker as the all-done fast path AND adds a `progress` block —
  `{"fingerprint": <fp>, "completed": [step ids]}` — recording each install step **only on
  `uv` `rc==0`**. A `kill -9` mid-torch relaunch skips finished steps instead of restarting
  the multi-gigabyte install (`uv` is idempotent, so a half-run step is always safely re-run).
  The ledger is written atomically (temp + `os.replace`) and is **asserted to live only in the
  app-private runtime venv, never under DataDir** — it is disposable, Tier-1-with-the-venv.
- **Honest progress.** The shim streams `uv` output line-by-line, interpolating byte progress
  into per-step pct bands when `uv` exposes it (else a stage-weighted estimate). The SPA's
  `StartupGate` polls `/health` and renders staged progress → the existing SetupPage model
  download flow once ready; it never one-shots the fetch.
- **A user-visible log.** A single rotating `msa-desktop.log` in the ADR-009 log dir spans the
  shim and (same process) uvicorn — the supervisor otherwise drains sidecar stdout into an
  invisible internal log. The per-run `provision-<timestamp>.log` (the path the responder
  reports) is unchanged and coexists.

## Consequences

- Three native UI codebases (Swift menu-bar, .NET tray, WinForms splash), browser-handoff
  choreography, port-conflict detection, and single-instance machinery are absorbed by the
  shell (deleted in a later increment). ADR-004 is superseded in part for macOS + Windows.
- Provisioning (uv/venv, the NVIDIA→cu128/CPU torch gate, `facenet-pytorch --no-deps`
  ordering, any vendored optional wheels, config bootstrap, downgrade guard) moves
  into the first-run shim (`src-tauri/backend/app/provision.py`), never writing to or
  deleting from the DataDir outside config bootstrap (iff-absent) and `version.txt`
  (on-success).
- Browser/dev mode, the `msa` CLI, the WSL2 dev flow, SQLite path conventions (ADR-007),
  the Linux shell-bundle path, and the three-repo staged release gating are all preserved.
- The release pipeline adds tag-triggered Tauri desktop-installer jobs + a `SHA256SUMS.txt`
  over all assets, without forking the three-repo gate; it can also produce signed update
  artifacts + `latest.json`, but these stay dormant (the shell does not auto-update — see §5).
  The legacy shell-bundle jobs keep running in parallel until a later increment retires the
  macOS/Windows legacy path (the Linux bundle stays). The thin one-liner bootstraps hard-fail
  on a missing/mismatched checksum.
- v1 deliberately drops auto-start-at-login and tray (mitigated by a close-while-indexing
  confirm added in a later increment); Linux stays on the legacy shell-bundle path; signing/notarization stay
  deferred (the thin bootstrap keeps unsigned tolerable); CSP stays null initially.
