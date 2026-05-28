# ADR-001: Deployment Architecture — Hybrid Option B

Status: Partially superseded — see "Later update" below

## Later update: embedded Qdrant supersedes Docker prerequisite

Docker is no longer a prerequisite. Qdrant runs in `qdrant-client path=` (embedded)
mode on both macOS and Windows-native, eliminating the Docker Desktop dependency
for end users. The "Hybrid Option B" decision below is preserved for historical
context; the current end-user runtime is fully native on each platform, with
Qdrant embedded in-process. The Docker Compose file is retained only as an
optional full-stack deployment mode (e.g. self-hosted server) and is not the
primary install path.

The "Platform-Specific Notes" section below reflects current behavior; the
"Decision" and "Rationale" sections describe the original Docker-required design.

## Context

The application has four components: indexer + SQLite/FAISS, FastAPI query engine,
Qdrant vector DB, and a browser UI. It needs to run on Windows (current) and distribute
to prerelease users on Mac and Windows. The ML stack (CLIP, RT-DETR, InsightFace)
has strong GPU/CUDA requirements that constrain deployment options.

Four deployment architectures were evaluated:

| Option | Description |
|---|---|
| A | Full Docker stack — all services containerised |
| B | Hybrid — Qdrant in Docker, Python stack (API + indexer + UI) native per platform |
| C | Electron/Tauri desktop app bundling Python as a sidecar process |
| D | PyInstaller single-binary |

## Decision

**Hybrid Option B.** Qdrant runs in a Docker container (`qdrant/qdrant:latest`).
The Python stack (FastAPI, indexer, ML models) runs natively in the appropriate
environment for each platform — WSL2 on Windows, native Python on macOS.

"Hybrid" refers specifically to this split: only Qdrant runs in Docker; everything
else is native. Docker Desktop is a prerequisite but its footprint is limited to a
single lightweight container.

## Rationale

### Why not Option A (Full Docker for everything)

- Docker on macOS uses a Linux VM (Apple Virtualization framework). MPS (Metal
  Performance Shaders) is not accessible from inside the VM, so all ML inference
  is CPU-only on Mac regardless of hardware. This is a significant performance
  regression on Apple Silicon.
- GPU passthrough in Docker on Windows requires NVIDIA Container Toolkit inside
  WSL2 — which is the exact same setup as running native Python in WSL2, with
  added complexity and slower cold start.
- Media library paths (`/mnt/d/...`, WSL2 mount points) must be bind-mounted into
  containers, which is confusing for users and fragile across system configurations.

### Why not Option C (Electron/Tauri)

- Bundling PyTorch + InsightFace + RT-DETR + CLIP model weights produces a 4–8 GB
  installer — impractical for distribution.
- CUDA/GPU support is nearly impossible to bundle cross-platform.
- Electron adds ~150 MB baseline overhead. Tauri is lighter but requires Rust knowledge
  and has complex Python sidecar lifecycle management.

### Why not Option D (PyInstaller)

- PyInstaller + PyTorch + ONNX Runtime + InsightFace is notoriously fragile across
  OS versions and Python patch releases.
- Produces a 4–10 GB binary once model weights are embedded.
- Bundled PyTorch is CPU-only in practice — no viable CUDA path.

### Why Qdrant in Docker rather than as a native binary

- Qdrant in Docker is the documented, supported deployment path (per the project README
  and existing `docker-compose.yml`). All existing indexed data was written by the
  Docker container — no migration required.
- Keeps the ML Python stack fully native (direct CUDA/MPS access) while isolating
  Qdrant's storage and network in a container.
- Docker Desktop is already a widely available prerequisite on both Windows and macOS.
- A future milestone may migrate to a pinned native Qdrant binary for a zero-Docker
  install experience, but that is deferred until after the prerelease milestone.

## Platform-Specific Notes

### Windows

**Two supported runtimes as of Phase 4B:**

#### Windows Native Python (Phase 4B — recommended for new installs)

A spike confirmed the full ML stack runs natively on Windows Python 3.12
with the following constraints:

| Package | Constraint | Reason |
|---|---|---|
| PyTorch | `--index-url https://download.pytorch.org/whl/cu128` | cu121/cu126 max out at sm_90; Blackwell RTX 4000/5000 requires cu128 |
| InsightFace | Gourieff unofficial wheel (`insightface-0.7.3-cp312-cp312-win_amd64.whl`) | Official PyPI wheel requires MSVC compilation |
| numpy | `==1.26.4` | InsightFace wheel incompatible with numpy 2.x |
| opencv | `==4.9.0.80` | Must match numpy downgrade; scenedetect has no opencv version constraint |
| faiss-cpu | Standard PyPI wheel | Available since ~1.7.4; no compilation needed |

**Known behaviour:** InsightFace auto-download on Windows extracts the antelopev2 model
pack with double-nesting (`models/antelopev2/antelopev2/*.onnx` instead of
`models/antelopev2/*.onnx`). `faces.py` self-heals this at runtime; `install.ps1` also
fixes it during setup.

Path UX: `resolve_for_access()` is a no-op on `win32` — stored Windows paths (e.g.
`D:\Photos`) are used directly for file I/O. No path translation layer needed.

#### Windows via WSL2 (Phase 3B — legacy, still supported)

WSL2 provides a Linux environment with full CUDA passthrough. This is the original
deployment path. The browser UI is accessed from Windows at `http://localhost:8000`.
Requires WSL2 install and Ubuntu provisioning (handled by `installer/windows/install.ps1`).
Still functional but the native path is preferred for new installs due to simpler setup
and better UX (no `/mnt/d/` path confusion).

### macOS

- `faiss-cpu` has pip wheels for macOS (Intel + Apple Silicon).
- PyTorch has the MPS backend for Apple Silicon (`device: "mps"`).
- InsightFace compiles with Xcode CLT — no special toolchain needed.
- All binary dependencies (exiftool, mediainfo) are bundled in the installer;
  Homebrew is not required.
- Qdrant is embedded via `qdrant-client path=` mode (Phase 2F complete). Docker is
  not required on macOS.

## Consequences

- Three platform setup paths: `install.sh` (Linux/WSL2), `setup.sh` (macOS),
  `installer/windows-native/install.ps1` (Windows native Python).
- `device` in `config.yaml` uses runtime auto-detection: cuda → mps → cpu.
  Implemented in `src/msa_settings/config.py` (`detect_device()`). `config.yaml`
  defaults to `device: auto`.
- `onnxruntime-gpu` (in `requirements.txt`) becomes `onnxruntime` on macOS.
  This split is handled in platform setup scripts, not the requirements file.
  `requirements-windows.txt` keeps `onnxruntime-gpu` (CUDA available natively).
- numpy is pinned to `==1.26.4` on Windows native (InsightFace wheel constraint).
  The codebase has no numpy 2.0-only API usage (confirmed by spike Stage 8a).
- InsightFace on Apple Silicon uses `CoreMLExecutionProvider` when `device == "mps"`.
  Inference is hardware-accelerated but slower than CUDA on Windows.
- `docker/docker-compose.yml` is kept as an optional full-stack deployment mode
  (e.g. server/NAS) but is not the primary path for install/launch.
