# ADR-008: Supported Development Environments

## Status
Accepted

## Context
The project targets two end-user platforms (Windows native, macOS) and uses ML
workloads (CUDA, PyTorch, InsightFace) that are best supported on Linux. WSL2
(Ubuntu 22.04) was the original primary dev environment, chosen to leverage
Linux's superior Python and ML ecosystem (CUDA, driver support, package
availability). macOS was later added as a co-equal dev environment as the
project's UI/API surface grew large enough that day-to-day frontend and API
work no longer needed to live in WSL2.

Both environments support the full app workflow; they differ in the kinds of
work they're best suited for:

| Environment | Best for | Constraint |
|---|---|---|
| WSL2/Linux | Indexing at production speed, CUDA-accelerated ML | Required for full-scale indexing of large libraries |
| macOS (native) | UI/API development, lightweight ML (MPS on Apple Silicon, CPU) | Slower than CUDA for large-batch indexing; fine for feature work |

## Decision
**macOS and WSL2/Linux are both first-class development environments.** All
developer-facing scripts (build, test, CI helpers) are bash/sh — these run
natively on both. PowerShell scripts exist only as end-user-facing installers
and launchers on the user's Windows machine.

| Script type | Format | Rationale |
|---|---|---|
| Dev build scripts (`build.sh`) | bash | Runs on macOS and WSL2; calls `iscc.exe` and `powershell.exe` via WSL2 interop where needed |
| CI workflows (`.github/workflows/`) | YAML + bash steps | GitHub Actions runners use bash for Linux/macOS jobs |
| End-user installer (`install.ps1`) | PowerShell 5.1 | Runs on the end-user's Windows machine |
| End-user launcher (`start.ps1`) | PowerShell 5.1 | Runs on the end-user's Windows machine |

## End-User Platforms
Two supported end-user platforms:

- **Windows native** — `installer/windows-native/` — no WSL2 required
- **macOS** — `installer/macos/` — `.pkg` + Platypus menu bar app

The WSL2-based installer (`installer/windows/`) remains in the repo for users
who already have WSL2, but Windows native is the primary Windows target.

## Consequences
- `build.ps1` is not used; only `build.sh` exists for building installers.
- New dev-facing scripts must be bash, not PowerShell.
- PS1 files are subject to the PS 5.1 compatibility rule (CLAUDE.md) because they
  run on end-user machines, not the dev machine.
- Contributors working on Windows without macOS need WSL2 installed for the dev
  workflow; this is documented as a project requirement.
- Contributors on macOS can develop UI/API features and run light indexing; they
  may want a WSL2 or Linux machine for full-scale CUDA-accelerated indexing.
