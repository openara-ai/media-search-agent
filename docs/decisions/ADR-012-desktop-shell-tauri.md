# ADR-012: Desktop Shell (Tauri) — Supervisor + Sidecar, Vendored Verbatim

Status: Accepted (desktop-shell work in progress; the initial increment landed the shell + backend/SPA sidecar contract).
Amended in a later revision — §3 vendoring discipline restated as a guideline, not an invariant.

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
"browser open", plus fixed-port fragility, browser-handoff choreography, and no
auto-update.

The desktop-app-template (validated by its reference spike and by a sibling FastAPI +
uvicorn + Vite/React product's production adoption) replaces all of that with one vendored
Rust supervisor and a native window, and adds signed auto-update. MSA adopts it for macOS
and Windows.

## Decision

### 1. Supervisor + sidecar

The shell is a **supervisor** (vendored Rust) that owns a native window and the lifecycle
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
  vendored-Rust change.

### 2. The two seams

- **`app.config.json`** (repo root) is the ONE project→shell integration file — name,
  identifier, window size, `spa_dir`, sidecar list. It is compiled into the supervisor via
  `include_str!`.
- **`window.__API_BASE__`** is the ONE frontend seam. The supervisor injects the ephemeral
  origin; the SPA routes every fetch/WebSocket/asset URL through `apiUrl()` / `wsUrl()`
  (`src/msa_apps/ui/src/lib/apiBase.ts`). An empty-string fallback keeps plain browser /
  dev mode byte-identical.

### 3. Vendoring discipline (the shell is copied, not forked)

`src-tauri/` (the Rust supervisor, `Cargo.toml`/`.lock`, `build.rs`, `capabilities/`),
`scripts/build-backend.{sh,ps1}`, `scripts/build-app.{sh,ps1}`, the template baseline of
`packaging/windows/installer-hooks.nsh`, and the root Tauri `package.json` are a **vendored
unit**: synced from a pinned template commit recorded in `src-tauri/.template-version`. The
default posture is adopt-as-is: a change that belongs in the template is preferably fixed
upstream first, re-vendored, and the pin bumped. MSA authors ~zero Rust in practice. The current
pin is the spike at `main@b047bae`.

**Project-owned / instantiated files** (edited for MSA, kept in sync manually):
`app.config.json`, `src-tauri/tauri.conf.json`, `src-tauri/tauri.windows.conf.json`,
`src-tauri/icons/`, the provisioning shim `src-tauri/backend/app/`, the backend sidecar
entry, the thin bootstraps, and the release-workflow changes.

Known project-identity deviations (tracked in `.template-version`): the vendored
`Cargo.toml`/root `package.json` originally carried the template's crate/package name; these
were subsequently de-templated to `media-search-agent` (crate name is the default binary
name — an earlier build shipped the wrong executable name; see the consumer-friction log). The bundle
name + identifier come from `tauri.conf.json` (`MediaSearchAgent` /
`ai.openara.mediasearchagent`). `scripts/build-app.sh` calls the vendored
`build-backend.sh`, which stages only a pure-stdlib shim; MSA's real staging is the
project-owned `scripts/stage-desktop-backend.sh` (backend source tree + optional wheels +
exiftool/mediainfo + config template). Wiring `build-app.sh` to the wrapper is an
upstream/re-vendor task.

#### Amendment: vendoring is a guideline, not an invariant

As originally written, this section made the vendored unit **read-only** ("never
hand-edited", fix-upstream-first as a precondition). In practice that invariant produced
more friction than the reuse it protected: the app shipped under the template's binary name
because the "clean" path forbade renaming the crate, and
the strict reading discouraged local fixes the project plainly needed. That inverts the
template's intent.

**Restated intent:** the desktop-app-template is a *guideline*, not a contract. Projects
adopt it as far as it fits, and are **free to diverge locally** — including hand-editing
files in the vendored unit — when the template does not fit or upstream turnaround would
block progress. The discipline that remains:

- **Record divergences.** Ideally each divergence gets an entry in the project's
  consumer-friction log (`desktop-app-template-friction.md`) — that log exists precisely
  to feed template improvements back upstream —
  plus a note in `src-tauri/.template-version` so a future re-vendor diff is explainable.
  If logging a divergence would itself block progress (e.g. for an automated agent
  mid-task), a `.template-version` note or a clear commit-message mention is an acceptable
  minimum; backfill the friction-log entry later.
- **Prefer upstream-first for template-shaped fixes.** When a change is generic (useful to
  any consumer), fixing the template and re-vendoring is still the better path — it just is
  no longer a *precondition* for fixing MSA.
- **Re-vendor consciously.** A re-vendor must reconcile recorded divergences rather than
  silently overwrite them; `.template-version` is the checklist.

### 4. Instantiation values (the desync-risk surface)

`tauri.conf.json`, `app.config.json`, and `Cargo.toml` duplicate values the template does
not yet single-source. The authoritative table:

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
injects `window.__APP_VERSION__` from `env!("CARGO_PKG_VERSION")` (Cargo.toml), which stays at the
template's `0.1.0` in every packaged build — it is a deferred project-identity value (§3), not a
stamped one. UI that shows the app version must therefore source it from the backend `app_version`
(`GET /diagnostics` → `_APP_VERSION`, git-tag-derived via `importlib.metadata`), which is correct in
both shell and browser mode. This bit the Settings › About section: preferring the injected
seam would have shown `0.1.0` on every release. If a future change stamps `Cargo.toml` at build (or
folds it into the release preflight), the seam becomes trustworthy and this note can retire.

### 5. Updater signing — minisign placeholder first, real keypair before release

The updater plugin verifies full-bundle downloads with a minisign public key committed in
`tauri.conf.json`. **The initial increment wires a clearly-marked PLACEHOLDER pubkey** (its
decoded comment flags it as a placeholder to be replaced before signed releases; an all-zero
key so verification always fails closed — and the supervisor's updater check is fail-soft, so
it never blocks launch).

**This supersedes the earlier spec's "generate the keypair up front" wording.** The developer
generates the real minisign keypair and custodies the **private** key as a GitHub Actions
secret (never committed) before the signed-release work, when the release pipeline first produces signed
updater artifacts. `createUpdaterArtifacts` is committed `false` and forced `true` only on
the release path, so key-less contributor builds still succeed. `plugins` is never empty
(an empty block panics the app at launch).

**A later increment wired this end to end.** The release workflow's desktop-build jobs run
`npx tauri build --config` (bypassing the vendored `build-app.sh`), forcing
`createUpdaterArtifacts:true` and injecting `TAURI_SIGNING_PRIVATE_KEY` /
`TAURI_SIGNING_PRIVATE_KEY_PASSWORD` **only on the publishing repos**; they upload the signed
`.app.tar.gz` / `setup.exe` + `.sig` and an assembled `latest.json`, plus a `SHA256SUMS.txt`
over all release assets. The updater resolves `latest.json` **anonymously — which works only
on the public repo**, so a preflight asserts the endpoint host is the public repo and the
self-update end-to-end proof is run there.

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
- The release pipeline adds tag-triggered Tauri desktop-installer jobs + signed
  updater artifacts + `latest.json` + a `SHA256SUMS.txt` over all assets, without forking the
  three-repo gate; the legacy shell-bundle jobs keep running in parallel until a later increment retires the
  macOS/Windows legacy path (the Linux bundle stays). The thin one-liner bootstraps hard-fail
  on a missing/mismatched checksum.
- v1 deliberately drops auto-start-at-login and tray (mitigated by a close-while-indexing
  confirm added in a later increment); Linux stays on the legacy shell-bundle path; signing/notarization stay
  deferred (the thin bootstrap keeps unsigned tolerable); CSP stays null initially.
