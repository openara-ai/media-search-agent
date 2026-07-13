# Installation

Media Search Agent installs as a **native desktop app** on macOS and Windows —
a self-contained app that manages its own Python runtime and ML stack. Linux
(and servers) run the same engine headless, serving the UI in your browser at
port 8000.

No admin rights, no `git`, no Node, no system Python required on any platform.
All ML inference (CLIP, RT-DETR, facenet-pytorch) happens on your machine.

If you'd rather install from source for development, see
[Install from source (developers)](#install-from-source-developers) at the
bottom of this page.

## Requirements

| Platform | Minimum | Notes |
|---|---|---|
| **macOS** | 12 (Monterey), Apple Silicon | Self-contained — no Xcode, Homebrew, or system Python required. Intel Macs are not a supported target. |
| **Windows** | Windows 11, x86_64 | NVIDIA GPU optional but strongly recommended for indexing speed. Driver only — the app installs the CUDA-enabled ML libraries itself. ARM64 Windows is not supported. |
| **Linux** | x86_64, glibc 2.31+ (Ubuntu 22.04 / Fedora 36 or newer) | NVIDIA GPU optional. Tested on Ubuntu 22.04 (including WSL2). |

Disk: allow **~5 GB free** for the app, its Python environment, ML libraries,
and model weights — first-launch setup checks this before it starts. Your
media library is indexed in place and is never copied or moved.

RAM: 8 GB minimum, 16 GB+ comfortable for large libraries (50k+ items).

## Install — macOS (Apple Silicon)

Download the latest **`.dmg`** from the
[Releases page](https://github.com/openara-ai/media-search-agent/releases/latest),
open it, and drag **MediaSearchAgent** into Applications. Launch it — the app
sets itself up on first run (see [First launch](#first-launch)).

Because pre-1.0 builds are not notarized, macOS may warn that the app is from
an unidentified developer the first time. Right-click the app → **Open** (or
approve it under **System Settings → Privacy & Security → Open Anyway**).

Prefer the terminal? The one-liner downloads the same app, verifies its
checksum, clears the quarantine flag for you, installs it into
`~/Applications`, and launches it:

```bash
curl -fsSL https://github.com/openara-ai/media-search-agent/releases/latest/download/install.sh | bash
```

## Install — Windows 11

Download the latest **`setup.exe`** from the
[Releases page](https://github.com/openara-ai/media-search-agent/releases/latest)
and run it. It installs per-user — no admin prompt, and your system Python (if
any) is untouched. Launch **MediaSearchAgent** from the Start menu — the app
sets itself up on first run (see [First launch](#first-launch)).

Because pre-1.0 builds are unsigned, SmartScreen may show "Windows protected
your PC" — click **More info → Run anyway**. The one-liner alternative
downloads the same installer, verifies its checksum, and runs it without the
SmartScreen prompt:

```powershell
powershell -c "irm https://github.com/openara-ai/media-search-agent/releases/latest/download/install.ps1 | iex"
```

## Install — Linux (and servers / headless)

```bash
curl -fsSL https://github.com/openara-ai/media-search-agent/releases/latest/download/install.sh | bash
```

The installer downloads the latest release bundle, bootstraps `uv` and a
standalone CPython in a private venv, installs dependencies, and serves the
UI in your browser at <http://localhost:8000>.

For servers and CI — including macOS and Windows machines without a GUI
session — add the headless flag. It provisions everything inline and installs
the [`msa` CLI](CLI.md) instead of launching an app window:

```bash
# Linux / macOS
curl -fsSL https://github.com/openara-ai/media-search-agent/releases/latest/download/install.sh | bash -s -- --headless
```

```powershell
# Windows
powershell -c "& ([scriptblock]::Create((irm https://github.com/openara-ai/media-search-agent/releases/latest/download/install.ps1))) -Headless"
```

Then start the server with `msa api start`.

## First launch

The desktop app's window opens immediately with a setup screen — there is
nothing to configure. Setup runs once and shows live progress through each
stage:

1. **Python runtime** — a standalone CPython, private to the app (~55 MB).
2. **ML libraries** — PyTorch and friends. On Windows machines with an NVIDIA
   GPU the CUDA-enabled build is selected automatically (~2 GB); other
   machines get the smaller CPU/Apple Silicon build.
3. **Application dependencies** (~1.5 GB).
4. **Model weights** (~1.5 GB) — CLIP from OpenAI's CDN, RT-DETR from Hugging
   Face, facenet-pytorch from its project GitHub releases.

Allow several minutes on a typical home connection — the setup screen never
sits silent, so if it's moving, it's working. Later launches start in
seconds.

Setup is resumable: if the download dies or you quit partway (even a crash),
the next launch picks up where it left off — finished stages are not
re-downloaded. If a stage fails, the setup screen shows the error with the
log-file path and a **Retry** button.

On Linux/headless installs the same model downloads happen on first run of
the server; the browser UI shows the progress.

## What it installs and where

The app keeps its private runtime separate from your data, so either can be
removed without touching the other.

**macOS**

| Path | Contents |
|---|---|
| `/Applications` or `~/Applications` | The app itself |
| `~/Library/Application Support/ai.openara.mediasearchagent/` | App-private runtime — Python, venv, `uv` cache |
| `~/Library/Application Support/MediaSearchAgent/` | Your data — `config.yaml`, index (SQLite), thumbnails |
| `~/Library/Caches/MediaSearchAgent/` | Caches |
| `~/Library/Logs/MediaSearchAgent/` | Logs |

(Note the two similarly-named folders under `Application Support`: the
identifier-keyed one is the disposable runtime; the `MediaSearchAgent` one is
your data.)

**Windows**

| Path | Contents |
|---|---|
| `%LOCALAPPDATA%\MediaSearchAgent\` | The app itself |
| `%LOCALAPPDATA%\ai.openara.mediasearchagent\` | App-private runtime — Python, venv, `uv` cache, WebView2 data |
| `%USERPROFILE%\MediaSearchAgent\` | Your data — `config.yaml`, index (SQLite), thumbnails |
| `%LOCALAPPDATA%\MediaSearchAgent\Cache`, `...\logs` | Caches and logs |

**Linux**

| Path | Contents |
|---|---|
| `~/.local/share/MediaSearchAgent/` | App code, venv, index, thumbnails, logs |
| `~/.config/MediaSearchAgent/` | `config.yaml` |
| `~/.cache/MediaSearchAgent/` | Caches |

**All platforms** — model weights live in shared per-user caches
(`~/.cache/huggingface/`, `~/.cache/clip/`, `~/.cache/torch/checkpoints/`,
~1.5 GB total) and survive reinstalls.

Your media folders are never copied or written to.

## Updating

The app never updates itself or checks for new versions in the background — you
choose when to update.

**macOS and Windows** — download the latest release and install it over your
current version (drag the new **MediaSearchAgent** into Applications on macOS;
run the new setup on Windows). No admin rights required. Your index,
configuration, and labeled people are preserved; the next launch may briefly
re-check dependencies (fast — everything is cached).

**Linux / headless** — re-run the install one-liner. It upgrades in place and
preserves your config, index, and labeled people.

## Uninstall

Your media library is never touched by any of these steps.

**macOS** — quit the app and drag **MediaSearchAgent** from Applications to
the Trash. That removes the app itself; to reclaim the rest, delete the
runtime and data folders listed in the
[table above](#what-it-installs-and-where) — keep
`~/Library/Application Support/MediaSearchAgent/` if you want your index and
labeled people to survive a reinstall.

**Windows** — **Settings → Apps → Installed apps → MediaSearchAgent →
Uninstall**. This removes the app and its private runtime (Python, venv,
WebView2 data). Your data — `%USERPROFILE%\MediaSearchAgent\` with
`config.yaml`, the index, and labeled people — is deliberately kept; delete
that folder manually if you want it gone.

**Linux** — run `msa uninstall`. The app is removed; you'll be prompted
before anything touches your index or config.

Optionally delete the shared model caches (`~/.cache/huggingface/`,
`~/.cache/clip/`, `~/.cache/torch/checkpoints/`) on any platform.

## Troubleshooting

**Setup fails on first launch** — the setup screen shows the log path (the
`provision-*.log` files in the log directory listed above have the full
detail) and a **Retry** button. Retry resumes from the failed stage; already
completed stages aren't re-downloaded.

**macOS: "MediaSearchAgent can't be opened"** — the Gatekeeper warning for
non-notarized apps. Right-click the app → **Open**, or approve it under
**System Settings → Privacy & Security**. The install one-liner clears the
quarantine flag automatically.

**Windows: SmartScreen blocks the installer** — click **More info → Run
anyway**, or use the one-liner, which verifies the checksum itself and avoids
the prompt.

**Models take forever to download / fail partway** — first-run downloads
total ~1.5 GB across three sources (OpenAI's CDN, Hugging Face, and the
facenet-pytorch GitHub releases). If a download dies, relaunch the app — each
downloader resumes where it left off. On a slow link, expect 5–15 minutes.

**Windows: GPU not detected / inference is slow** — install or update your
NVIDIA driver from <https://www.nvidia.com/Download/index.aspx> (you do not
need the full CUDA Toolkit — driver only). Confirm with `nvidia-smi`. If you
add or remove an NVIDIA GPU later, the app notices on the next launch and
reinstalls the matching ML libraries.

**Linux: "port 8000 already in use"** — another app (or a previous run) is
bound to the port. Find it with `lsof -i :8000` and stop it, or override the
port in `config.yaml`. (The desktop app on macOS/Windows picks its own local
port automatically, so this doesn't apply there.)

**Linux: missing `libGL` / `libglib`** — install the standard image libs:
`sudo apt install libgl1 libglib2.0-0`.

**Search returns nothing** — semantic search comes online when the indexer
finishes; queries run while indexing is still in progress will be empty.
Wait for the run to complete, then retry. If the **Indexer** page shows
zero items and zero progress at all, check that the path you added is
readable and contains supported formats (`.jpg`, `.jpeg`, `.png`, `.heic`,
`.mp4`, `.mov`).

For anything not covered here, check [the FAQ](FAQ.md) or open an issue on
[GitHub](https://github.com/openara-ai/media-search-agent/issues).

## Install from source (developers)

The releases above are the path for end users. Contributors typically work
from a checkout instead:

```bash
git clone https://github.com/openara-ai/media-search-agent.git
cd media-search-agent
bash scripts/dev-setup.sh    # idempotent; ~5–10 min on first run
bash scripts/start.sh        # opens http://localhost:8500 (different from end-user)
```

Supported dev environments are **macOS** and **WSL2 / Linux**. There is no
Windows-native dev path — Windows contributors should work inside a WSL2
Ubuntu shell and follow the commands above.

After `git pull`, re-run `bash scripts/dev-setup.sh` to pick up new
dependencies. See the [Contributing section](../README.md#contributing) in
the README for the full dev workflow.
