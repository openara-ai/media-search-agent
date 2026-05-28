# ADR-005: Uninstallation Policy

Status: Accepted

## Context

Uninstallation must cleanly remove the application without destroying user data
(media index, thumbnails, embedded Qdrant data, config) without explicit consent, and must not touch shared system
resources (WSL2 distro, shared Python tooling / caches) that other tools or projects may depend on.

Uninstallation is delivered alongside the installer in the same phase — not deferred.
Phase 1A includes uninstall scripts as first-class deliverables.

## Decision

Three tiers of removal:

### Tier 1 — Always Remove (no prompt)

Removed silently as part of every uninstall:

- App source code (`~/media-search-agent/src/` on WSL2;
  `~/Applications/MediaSearchAgent.app/Contents/Resources/src/` on macOS)
- App virtual environment (`.venv/`)
- Bundled binaries (`qdrant`, `exiftool`, `mediainfo`) installed by the app
- Launcher scripts and all shortcuts (Start Menu, Desktop, Applications folder entry)
- `launchd` plist if "Start on Login" was enabled
  (`~/Library/LaunchAgents/com.mediasearchagent.plist`)
- Windows AppData files (`%LOCALAPPDATA%\MediaSearchAgent\`)
- Install and runtime logs

### Tier 2 — Prompt User (default: keep)

Presented as opt-in checkboxes. Defaults to keeping both. Size shown so the user
can make an informed choice:

```
Remove Media Search Agent

The following data will be kept by default.
Check a box to delete it permanently.

☐  Persistent app data               — 3.2 GB
   ~/Library/Application Support/MediaSearchAgent/index/
   ~/Library/Application Support/MediaSearchAgent/data/
   ~/Library/Application Support/MediaSearchAgent/qdrant/
   ~/Library/Application Support/MediaSearchAgent/config.yaml

These cannot be recovered after deletion.

[ Cancel ]                [ Uninstall ]
```

Tier 2 also includes app-private model caches stored under Media Search Agent's own
cache directory, because they are re-downloadable but can be large enough that some
users will want the option to reclaim the space during uninstall.

Rationale for defaulting to keep: re-indexing a large library takes hours. If the
user is reinstalling (e.g. after an upgrade failure), losing the index, thumbnails,
Qdrant store, or config would be a serious regression. The user must explicitly
opt in to deletion.

### Tier 3 — Never Touch (no option presented)

- **WSL2 Ubuntu distro** — `wsl --unregister` would destroy the entire distro
  and all its contents. The user almost certainly uses Ubuntu-22.04 for other
  development work. The uninstaller only removes `~/media-search-agent/` inside
  the distro.
- **Shared ML / tooling caches outside Media Search Agent's own cache directory** —
  these live in standard OS cache locations shared with other tools:
  - `~/.cache/open_clip/` (CLIP — may be used by other ML projects)
  - `~/.insightface/models/` (InsightFace)
  - `~/.cache/huggingface/` (RT-DETR / HuggingFace — may be used by other ML projects)
  - Models re-download automatically on first use after reinstall.
  - This does **not** include Media Search Agent's own app-private cache directory
    such as `~/Library/Caches/MediaSearchAgent/` or `%LOCALAPPDATA%\MediaSearchAgent\Cache\models`,
    which may be offered as a Tier 2 delete option.
- **`uv` binary and managed Python** at `~/.local/share/uv/` — the user may
  rely on uv for their own Python projects.
## Uninstaller Delivery

| Platform | Uninstaller | Phase |
|---|---|---|
| macOS | `Uninstall MediaSearchAgent.app` bundled in the `.dmg` alongside the `.pkg` | 1C |
| Windows (scripts) | `uninstall.ps1` + "Uninstall Media Search Agent" Start Menu entry | 1D |
| Windows (Inno Setup) | `unins000.exe` (auto-generated) calls `uninstall.ps1` for WSL2 side | 3B |

## Uninstaller Sequence

The uninstaller always:
1. Stops running services first (calls `stop.sh` / `stop.ps1`)
2. Removes Tier 1 files
3. Shows Tier 2 prompt
4. Removes selected Tier 2 files (if any)
5. Shows completion summary

It never touches Tier 3 resources regardless of user input.

## Consequences

- `scripts/uninstall.sh` and `scripts/uninstall.ps1` are Phase 1A deliverables,
  not deferred to a later phase.
- The macOS `.dmg` must always contain both the installer and the uninstaller.
- The uninstaller must handle the case where services are not running (idempotent stop).
- If persistent app data is preserved (Tier 2 default) and the user reinstalls, the installer
  must not overwrite it — see ADR-004 idempotency requirements.
