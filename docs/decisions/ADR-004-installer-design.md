# ADR-004: Installer and Launcher Design

Status: Partially superseded — see "Later update" below

## Later update: Windows pivot to shell bundles

The **Windows Inno Setup `.exe` installer** path described below was removed
for the v0.2.0 release. The Windows install path is now a shell bundle invoked
via a one-line PowerShell bootstrap (same pattern as macOS). The PowerShell
launcher and lifecycle scripts (`install.ps1`, `start.ps1`, `stop.ps1`,
`cli.ps1`, `uninstall.ps1`) originally written for the Inno wrapper are reused
by the shell bundle.

**Why the pivot:**

- Code signing was deferred indefinitely (Phase 3A). Unsigned `.exe` installers
  trigger Windows SmartScreen warnings that hurt the prerelease launch.
- The shell bundle approach works without signing and is simpler to distribute.
- Inno Setup added Windows-specific CI dependencies and complexity.

**Files removed in this pivot:**

- `installer/windows-native/setup.iss` (Inno config)
- `installer/windows-native/build.sh` (Inno build wrapper)
- `.github/workflows/ci.yml`'s `build-installer` job (Inno build validation)

**What's preserved:** the macOS `.pkg` + `.dmg` path described below remains as
designed (build available via `workflow_dispatch` on `ci.yml`); the Windows
PowerShell scripts continue to exist and are used by the shell bundle.

The original decisions and rationale below are preserved for historical context.

## Context

The app needs platform-native installers and launchers that feel appropriate for each OS,
handle platform-specific ML stack requirements, produce timestamped logs for debugging,
and are robust to pre-existing partial installations (idempotency).

## Decisions

### macOS Installer

**Format:** `.pkg` package inside a `.dmg` disk image — the standard macOS distribution
format used by VLC, Obsidian, and most open source desktop apps.

**Build tools:**
- `pkgbuild` + `productbuild` (Apple's own tools, free, built into Xcode)
- `create-dmg` (open source shell script for professional DMG layout)

**Bundled binaries (no Homebrew dependency):**

| Binary | Source |
|---|---|
| `exiftool` | Standalone Perl binary from exiftool.org |
| `mediainfo` | Official CLI binary from mediaarea.net |
| `qdrant` | Official macOS binary from GitHub Releases (ARM64 + x86_64) |
| `uv` | universal2 binary from astral.sh/uv releases |

Bundling these binaries means users never need to install Homebrew. The DMG contains
both the `.pkg` installer and `Uninstall MediaSearchAgent.app`.

**Scripts:**
- `preinstall` — checks macOS 12+; prompts Xcode CLT install if absent
- `postinstall` — runs `setup.sh` as the console user (not root) using
  `launchctl asuser` with the logged-in user's UID; installs Python via uv,
  pip installs packages, pre-downloads ML models

### macOS Launcher

**Format:** `MediaSearchAgent.app` built with Platypus when available (wraps a shell
script in a native `.app` bundle with a menu bar status item). If Platypus is not
available at build time, ship a plain double-clickable `.app` fallback that starts
the local service, waits for readiness, opens the browser, and exits.

Behaviour:
- Launches as a menu bar app (not in the Dock) — appropriate for a background service
- Menu bar icon: spinner (grey) during startup → solid (green) when ready
- Menu items: Open Media Search | — | Status: Running | Qdrant: ● | API: ● | — |
  Restart Services | View Logs | — | Start on Login ✓ | — | Quit
- "Start on Login" writes/removes `~/Library/LaunchAgents/com.mediasearchagent.plist`
- Double-clicking the app when already running: detects services up and opens browser only
- Quitting: SIGTERM to uvicorn + Qdrant; waits for clean exit

### Windows Installer — Phase 1D (Prerelease, WSL2 users only) — historical, superseded by update above

**Scope:** Existing WSL2 users only. Users without WSL2 receive a clear message:
"WSL2 is required. See setup-guide.md." Full automation for non-WSL2 users is Phase 3B.

**Format:** PowerShell scripts delivered as a zip or via a one-line invocation.

| Script | Purpose |
|---|---|
| `install-windows.ps1` | One-time setup: detects WSL2 distro, runs install.sh, creates Start Menu + Desktop shortcuts |
| `launch.ps1` | Start Menu shortcut target: starts start.sh in WSL2, polls /health, fires toast, opens browser |
| `stop.ps1` | Sends stop signal into WSL2; confirmation toast |
| `uninstall.ps1` | Removes WSL2 app dir, Windows-side shortcuts, AppData entries |

Toast notifications use the Windows built-in `BurntToast` PowerShell module or fall
back to `msg.exe` for machines where BurntToast is unavailable.

### Windows Installer — Phase 3B (Public, any Windows user) — historical, superseded by update above

**Format:** Inno Setup `.exe` wizard — the standard for open source Windows installers
(used by VS Code, Python.org, Git for Windows).

Additions over Phase 1D:
- WSL2 detection: if absent, runs `wsl --install --distribution Ubuntu-22.04`
- Reboot-resume: writes a `InstallStage` value to
  `HKCU\Software\MediaSearchAgent` and adds itself to `RunOnce` if a reboot is
  required. On resume after reboot, reads the flag and continues from the correct step.
- Non-interactive Ubuntu user creation inside WSL2
- Auto-generated `unins000.exe` handles Windows-side removal; calls `uninstall.ps1`
  for the WSL2 side

### Windows Launcher — Phase 3C (Public) — historical, superseded by update above

**Format:** Small Go binary (~3 MB, no runtime dependency) providing a system tray icon,
mirroring the macOS menu bar experience exactly.

- Tray icon: grey (stopped) / green (running) / orange (error)
- Menu: Open Media Search | — | Restart Services | View Logs | Start on Login | — | Quit
- Bundled inside the Phase 3B Inno Setup installer

### Installer Logging (Both Platforms)

Every installer writes a timestamped log from its first line:

| Platform | Location |
|---|---|
| macOS | `~/Library/Logs/MediaSearchAgent/install-<timestamp>.log` |
| Windows | `%LOCALAPPDATA%\MediaSearchAgent\logs\install-<timestamp>.log` |

Runtime launch logs use the same directories with `launch-<timestamp>.log`.

On failure: a dialog shows the log path with "Open Log File" and "Copy Path" buttons.
"Open Log File" opens in the OS default text viewer (not a terminal).

### Idempotency Requirements

Every installer step checks before acting. For users with existing WSL2 (like the
developer), this means most steps are skipped:

| Check | Skip condition |
|---|---|
| WSL2 enabled | Windows feature already active |
| Distro installed | Ubuntu-22.04 (or compatible) already present |
| Python 3.12 available | Found in PATH or uv-managed location |
| System binaries | exiftool, mediainfo already in PATH |
| ML models cached | Check ~/.cache/open_clip, ~/.insightface, ~/.cache/huggingface |
| Existing venv | `~/Library/Application Support/MediaSearchAgent/.venv/` already present → upgrade mode |
| `config.yaml` | Never overwritten; "existing config preserved" notice shown |
| `index/` directory | Never touched during install or upgrade |

## Consequences

- **End-user config comes from platform-specific checked-in templates, never from the dev repo.**
  `installer/macos/config.macos.yaml.template` and
  `installer/windows-native/config.windows.yaml.template` are the canonical
  user-facing config files. They are checked into the repo, reviewed in PRs, and
  validated for YAML syntax in the CI `test` job. Each contains platform-specific
  comments, path examples, and guidance (e.g. MPS on macOS, CUDA on Windows, correct
  path format per OS). The macOS `build.sh` copies `config.macos.yaml.template` into
  the installer payload as `config.yaml.template`; the Windows `build.sh` and CI/release
  workflows add `config.windows.yaml.template` to `app.zip` as `config.yaml.template`.
  The repo-root `config.yaml` is a developer-only override (WSL2-specific paths, dev
  port) and is explicitly excluded from all `git archive` calls. End-user installers
  must never read, copy, or reference the repo-root `config.yaml`.
  Linux is not yet a supported end-user platform; `scripts/install.sh` is a dev tool.
  When a Linux end-user installer is built, it will follow the same pattern and ship
  `installer/linux/config.linux.yaml.template`.

- macOS installer requires Xcode Command Line Tools. The preinstall script detects
  their absence and prompts the user; the install cannot proceed without them.
- Code signing (Phase 3A) is required to eliminate Gatekeeper "Open Anyway" friction
  on macOS and SmartScreen warnings on Windows. Milestone 1 and 2 work without signing;
  friends must click through a one-time OS warning.
- The Platypus `.app` approach for macOS is sufficient for Milestones 1 and 2.
- A hand-built `.app` bundle is not a drop-in replacement for Platypus status-menu
  behaviour. If Platypus CLI is missing at build time, the fallback launcher must be
  treated as a simpler browser launcher, not as a menu bar app.
  If a native Swift/SwiftUI launcher is ever needed (e.g. for Mac App Store distribution),
  that is out of scope for the current roadmap.
