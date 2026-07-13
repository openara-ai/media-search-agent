# ADR-009: Runtime Path Ownership and Naming

Status: Accepted

## Context

The project has accumulated multiple overlapping path conventions across installer
docs, shell scripts, Python config defaults, and platform notes:

- App-visible names vary between `msa`, `media-search-agent`, and `MediaSearchAgent`
- Some docs describe macOS user data/config under platform directories, while older
  installer code assumed writable state could live under `/Applications/MediaSearchAgent`
- Shell scripts and launchers use a mix of `MSA_ROOT`, `AppDir`, `DataDir`,
  `MSA_DATA_DIR`, and ad-hoc derived paths

This caused a real bug: the macOS installer attempted to create a virtualenv inside
the root-owned app install directory instead of a user-writable location.

ADR-007 covers **user media/source path representation** and conversion. It does not
define installer/runtime directory ownership for app-managed files.

## Decision

### 1. Canonical app-name token

When the app name appears in a user-visible or installer/runtime directory name, use
**`MediaSearchAgent`** consistently.

Examples:

- `%LOCALAPPDATA%\MediaSearchAgent`
- `%USERPROFILE%\MediaSearchAgent`
- `~/Applications/MediaSearchAgent.app`
- `~/Library/Logs/MediaSearchAgent`

Avoid introducing new user-visible directory names based on `msa` or
`media-search-agent` unless there is a strong compatibility reason.

`msa` remains acceptable only where short internal identifiers are beneficial and not
normally user-facing:

- Python package/module names (`msa_settings`, `msa_indexer`)
- Internal config/cache subfolders where already established and low-risk
- CLI command names (`msa-index`)

### 2. Separate app-owned files from user-owned files

Runtime paths must follow an ownership model, not just a platform default:

| Category | Ownership | Rule |
| --- | --- | --- |
| App install dir | Installer-managed | May be read-only at runtime; do not place mutable state here |
| User data dir | User-writable | Index, thumbnails, embedded Qdrant, other persistent data |
| User config dir | User-writable | `config.yaml` and future editable config files |
| User log dir | User-writable | Installer logs, launch logs, `msa.log`, PID/run files |
| User cache dir | User-writable, disposable | Downloaded models and other re-creatable files |

On macOS specifically, the shell installer places the app at
`~/Applications/MediaSearchAgent.app` — per-user, no `sudo` / admin elevation
required. (`/Applications` is reserved for system-wide installs that need
admin rights; we deliberately avoid it.) Code, the venv, and bundled binaries
live under `Contents/Resources/` inside that bundle and are treated as
installer-managed. Mutable runtime state (index, thumbnails, config, logs,
cache) belongs in the user-writable platform directories listed in section 6,
never under the `.app` bundle.

### 3. Standardized core directory vocabulary

Scripts and launchers should use a consistent set of core directory variables:

| Variable | Meaning |
| --- | --- |
| `APP_DIR` | Installer-managed application root / internals root |
| `DATA_DIR` | Persistent user data root |
| `CONFIG_DIR` | User config directory |
| `CACHE_DIR` | Re-downloadable cache root |
| `LOG_DIR` | User log directory |

All other paths derive from those:

| Derived variable | Definition |
| --- | --- |
| `CONFIG_PATH` | `$CONFIG_DIR/config.yaml` |
| `RUN_DIR` | `$LOG_DIR/run` |
| `BIN_DIR` | `$APP_DIR/bin` |
| `LIB_DIR` | `$APP_DIR/lib` |
| `VENV_DIR` | Platform-defined runtime venv location; must not be placed under a read-only install dir |

Do not compute unrelated path roots independently when one of the core directories
already determines the value.

### 4. Environment variable naming

Environment variables that cross a process boundary (launcher → runtime,
installer → runtime, user shell → CLI) MUST use the `MSA_*` prefix:

- `MSA_ROOT` (app code root)
- `MSA_DATA_DIR`, `MSA_CONFIG_PATH`, `MSA_CACHE_DIR`, `MSA_LOG_DIR`
- `MSA_VENV_DIR`
- `MSA_DEV` (toggle dev-mode path resolution)
- `MSA_DEVICE` (override `device: auto` resolution)

Why prefix:

- Env vars are a process-global namespace; bare names like `DATA_DIR` or
  `CONFIG_PATH` collide with other tools and shell scripts.
- The prefix makes ownership obvious in `printenv` output and in stack traces.
- It matches established conventions: `PG_*`, `RUST_*`, `NODE_*`, `JAVA_HOME`,
  `XDG_*`.

The bare directory names from section 3 (`APP_DIR`, `DATA_DIR`, `CONFIG_DIR`,
`CACHE_DIR`, `LOG_DIR`, `BIN_DIR`, `LIB_DIR`, `VENV_DIR`, `RUN_DIR`,
`CONFIG_PATH`) remain the **shared vocabulary for local shell variables and
documentation** — i.e. inside a single script, or in path tables like the one
in section 6. They must not be `export`ed across process boundaries.

### 5. Review gate

Any change that introduces a new installer/runtime path must answer:

1. Is this path app-owned or user-writable?
2. Which core directory does it derive from?
3. Is the name user-visible? If yes, why is it not `MediaSearchAgent`?

Missing answers are a review issue.

### 6. Per-platform default directory map

Installer and runtime defaults must agree on every platform. Both layers resolve
the same paths when no `MSA_*` override is set; the `msa` launcher's env-var
exports are then a defensive override, not a load-bearing requirement.

| Platform | Data | Config | Cache | Logs |
| --- | --- | --- | --- | --- |
| macOS | `~/Library/Application Support/MediaSearchAgent` | `~/Library/Application Support/MediaSearchAgent/config.yaml` | `~/Library/Caches/MediaSearchAgent` | `~/Library/Logs/MediaSearchAgent` |
| Linux | `~/.local/share/MediaSearchAgent` | `~/.config/MediaSearchAgent/config.yaml` | `~/.cache/MediaSearchAgent` | `~/.local/share/MediaSearchAgent/logs` |
| Windows | `%USERPROFILE%\MediaSearchAgent` | `%USERPROFILE%\MediaSearchAgent\config.yaml` | `%LOCALAPPDATA%\MediaSearchAgent\Cache` | `%LOCALAPPDATA%\MediaSearchAgent\logs` |

Notes:

- Windows config sits under `%USERPROFILE%`, not `%APPDATA%`. This is intentional:
  the installer surfaces user data and config in the user's home folder for
  discoverability, and runtime defaults follow.
- **Linux defaults intentionally do not honor `XDG_DATA_HOME` / `XDG_CONFIG_HOME` /
  `XDG_CACHE_HOME`.** The shell installer hardcodes the paths above; the runtime
  matches it. Supporting XDG on one layer but not the other reintroduces the
  installer/runtime divergence this section exists to prevent. Power users who
  need a custom data location set `MSA_DATA_DIR` (already exported by the
  launcher; see section 4) — that is the supported escape hatch. We may revisit
  XDG support once the project has Linux-native end-user uptake to justify the
  test surface.
- The `msa` package/module name is internal and remains; **no user-visible
  directory uses `msa`**.

This table is the contract between `_platform_*_dir()` in `msa_settings/config.py`
and the shell-bundle installers in `installer/macos/shell/install.sh` and
`installer/windows-native/shell/install.ps1`. Changing one without the other
re-introduces the bug that motivated this section.

## Consequences

- ADR-003 and ADR-004 should be read with this ADR as the source of truth for path
  ownership and naming.
- The macOS shell installer places the app at `~/Applications/MediaSearchAgent.app`
  (per-user, no admin elevation). Mutable runtime state lives under `~/Library/...`,
  never inside the `.app` bundle.
- Runtime defaults now use `MediaSearchAgent` everywhere — no more legacy `msa`
  directories in user-visible locations. Pre-release Linux/Windows installs that
  had data under `~/.config/msa` etc. will not auto-migrate; users must move
  files or reinstall.
- Shell scripts should converge on `APP_DIR` / `DATA_DIR` / `LOG_DIR` terminology
  instead of ad-hoc local names.
- `MSA_*` is the canonical prefix for cross-process environment variables; bare
  directory names are local-script vocabulary only.

## Amendment: app-private runtime path for the Tauri desktop shell

The Tauri desktop shell ([ADR-012](ADR-012-desktop-shell-tauri.md)) moves the **runtime**
(the bundled-`uv` CPython, the venv, the uv cache, and the extracted `uv` binary) out of
the app bundle and into a **shell-owned app-private directory**, keyed by the bundle
**identifier** `ai.openara.mediasearchagent`. This does not change any user-data location:
`config.yaml`, `index/`, thumbnails, cache, and logs stay exactly where the per-platform
map in section 6 puts them (keyed by the `MediaSearchAgent` name). The provisioning shim
exports the `MSA_*` env (section 4) so `msa_settings` resolves those user directories
unchanged — the DataDir survives a migration byte-identical.

The app-private root is the vendored supervisor's `app_local_data_dir()` (Tauri):

| Platform | App-private runtime root | Contents |
|---|---|---|
| macOS | `~/Library/Application Support/ai.openara.mediasearchagent/` | `.venv/`, `python/` (`UV_PYTHON_INSTALL_DIR`), `uv-cache/` (`UV_CACHE_DIR`), `bin/uv` |
| Windows | `%LOCALAPPDATA%\ai.openara.mediasearchagent\` | same |
| Linux | `~/.local/share/ai.openara.mediasearchagent/` | same (shell path is macOS/Windows; Linux stays on the legacy bundle) |

Note the **identifier**-keyed runtime root (`ai.openara.mediasearchagent`) is intentionally
distinct from the **name**-keyed user-data root (`MediaSearchAgent`, section 6): the runtime
is disposable and app-owned (a single directory removal is a Tier-1 runtime uninstall), the
user data is durable and user-owned. The exact paths above are derived from the vendored
`app_private_dir()` in `src-tauri/src/main.rs`; they are runtime-verified on a packaged
launch as part of the desktop-shell human definition-of-done.

### Note: the identifier boundary also protects legacy migration

The first Tauri install migrates away the **legacy** shell-bundle runtime, which lived under
the **name**-keyed `%LOCALAPPDATA%\MediaSearchAgent` (Windows) — the SAME dir the new Tauri app
installs into (`productName` == `MediaSearchAgent`). The migration
(`src-tauri/backend/app/migration.py` + the NSIS `NSIS_HOOK_PREINSTALL` in the project-owned
`packaging/windows/msa-installer-hooks.nsh`) removes only the named legacy children (`repo\`,
`.venv\`, `uv\`, `bin\`, `Cache\models\`, `logs\`, `start.ps1`, `stop.ps1`, `version.txt`) plus
the Start-Menu shortcut, scheduled task, HKCU Run value and PATH entry — **never** a wholesale
removal of the root, and **never** the name-keyed DataDir `%USERPROFILE%\MediaSearchAgent`
(config.yaml + index). Because the new runtime is **identifier**-keyed
(`ai.openara.mediasearchagent`) it can never collide with the legacy name-keyed AppDir; the
first-run belt-and-braces sweep additionally refuses `bin\`/`backend\` when the legacy root
resolves to the running install dir, so it can never delete the live app's own resources.
