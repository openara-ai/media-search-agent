# FAQ

If your question isn't here, check the [Quick Start](QUICKSTART.md), the
[Installation guide](INSTALL.md), or open an issue on
[GitHub](https://github.com/openara-ai/media-search-agent/issues).

## Privacy

**Does any of my data leave my machine?**
No. All ML inference (CLIP, object detection, faces) runs locally. The app has
no telemetry and no analytics. The only network calls are the model downloads
on first run (or on config changes), after which the app works offline.

**Where is my media stored?**
On disk, exactly where you put it. The app reads from your library; it never
copies, moves, or modifies files. Indexes and thumbnails live in a separate
data directory (see [INSTALL.md](INSTALL.md#what-it-installs-and-where)).

**Does it write to my photo library?**
No. Sources default to read-only. The app refuses to write back to indexed
folders.

**What about the model downloads?**
On first run, the app downloads ~1.5 GB of pre-trained model weights from
the three upstream projects: CLIP from OpenAI's CDN, RT-DETR from Hugging
Face, and facenet-pytorch from its project GitHub releases. After download
they live in per-user cache directories and are loaded locally for every
search and index operation. No content from your library is ever sent anywhere.

## Hardware

**What hardware do I need?**
Minimum: 8 GB RAM, ~3 GB free disk. A reasonably modern CPU is enough for
search; indexing benefits a lot from a GPU.

**Does it work on Intel Macs?**
Not via the install one-liner — release bundles are Apple Silicon only
(`macos-arm64`). Intel Macs may still work from source by cloning the repo
and running `bash scripts/dev-setup.sh`, but ML inference falls back to CPU
and is slow. Intel Macs are not a supported target.

**Does it work on Windows 10?**
No — the desktop app requires Windows 11 or later (x86_64). Windows 11
ships the WebView2 runtime the app is built on.

**Does it work on Linux?**
Yes — Ubuntu 22.04 and equivalents, running headless with the UI in your
browser. Tested on WSL2. NVIDIA GPU optional but highly recommended.

## Formats and media

**Which file formats are supported?**
Images: JPEG, PNG, HEIC, WebP, TIFF. Videos: MP4, MOV, MKV, AVI. RAW formats
are not currently indexed.

**Does it index video?**
Yes. The indexer extracts shot-based keyframes and embeds each one. Search
results that match a video link directly to the matching moment, not just to
the file. See [features/video.md](features/video.md).

**Does it read EXIF / GPS / timestamps?**
Yes. EXIF metadata (camera, lens, date taken, GPS coordinates) is parsed and
made searchable and filterable. GPS coordinates are reverse-geocoded to a
place name (city / region / country) using the local `reverse_geocoder`
Python library, which ships with its own offline dataset — no cloud
geocoding service is called and your coordinates never leave the machine.

**Can I add multiple folders?**
Yes. Add as many media sources as you like from the **Indexer** page; they
all index into one searchable library.

**Can it follow symlinks?**
Yes — symlinked directories under a media source are walked normally.

## Indexing

**How long does indexing take?**
It depends. Per file you'll typically see anywhere from a fraction of a
second to several seconds, driven by:

- **Hardware** — a discrete NVIDIA GPU is fastest, Apple Silicon (MPS) is
  next, CPU-only is the slowest by a wide margin.
- **Media size** — 4K and 50 MP photos take longer than phone-sized JPEGs;
  long videos take longer than short clips because each shot's keyframes
  go through the full model pipeline.
- **What's enabled** — object detection, face recognition, and video shot
  detection each add cost; turning any of them off in `config.yaml` speeds
  things up. Object detection defaults to `auto`, which **skips it on
  CPU-only systems** (no NVIDIA CUDA or Apple Silicon MPS detected) because
  it's prohibitively slow without a GPU. Set `enable_object_detection: true`
  in `config.yaml` to force it on.

The indexer is incremental — re-runs only process new or changed files —
so the long wait only happens once per library.

**Can I search before it finishes?**
Not yet. **Browse** is available as soon as the indexer starts writing
files — you'll see new items appear in the grid as they're processed.
**Search** requires the Qdrant vector index to be built, which currently
happens at the end of an indexing run, so semantic queries return empty
results until the indexer completes. Optimization work to stream
embeddings into Qdrant during indexing — so search comes online
incrementally rather than at the end — is in progress.

**What happens when I add new photos?**
Re-run the indexer manually from the **Indexer** page. It only processes
new and changed files; existing embeddings are preserved. Automatic
background re-indexing when new media is added is in progress.

**What if I move my media folder?**
There's no in-place "edit source path" today — remove the old source on
the **Indexer** page, add the new path, and re-run. Because media items
are tracked by content hash, files that re-appear under the new path are
recognized as already-indexed and aren't re-embedded. Note that removing
a source only updates `config.yaml`; it does not delete the metadata or
embeddings for files that were under it. Cleanup of orphaned rows is on
the roadmap.

## Running the app

**How do I update?**
On macOS and Windows you don't do anything — the desktop app checks for a
new release at launch, verifies its signature, and installs it in the
background; the update takes effect the next time you start the app. On
Linux and headless installs, re-run the install one-liner — it upgrades in
place and preserves your config, index, and labeled people.

**How do I uninstall?**
macOS: drag the app to the Trash. Windows: **Settings → Apps →
MediaSearchAgent → Uninstall**. Linux: `msa uninstall`. Your index and
config are kept unless you delete them yourself, and your media library is
never touched. See [INSTALL.md](INSTALL.md#uninstall) for the full path
list.

**Can I run it on a different port?**
The desktop app doesn't use a fixed port at all — it picks a private local
port each launch, so there's nothing to configure and nothing to conflict.
On browser/headless installs, change `port:` under `api:` in `config.yaml`.
See [CONFIGURATION.md](CONFIGURATION.md).

**Can I run it on a NAS or home server and access it from another device?**
Yes. Install on the server with the headless flag (see
[INSTALL.md](INSTALL.md#install--linux-and-servers--headless)), then run
`msa api start --bind-host 0.0.0.0`. Visit `http://<server-ip>:8000` from
another device on your LAN. Treat this as a trusted-network setup — there's
no auth in front of the API.

**Does it keep running when I close the window?**
The app itself quits when you close its window — there's no tray icon and
no auto-start at login. The one exception is an in-progress indexing run:
it deliberately keeps running in the background so a long first index isn't
lost, and the app reconnects to it when you relaunch. On headless installs,
`msa api start` runs in the foreground of its terminal; use your own
service manager (e.g. systemd) if you want it supervised.

## Project

**Is this project open source?**
Yes — MIT-licensed. See [LICENSE](../LICENSE).

**Where do I report bugs / request features?**
[GitHub Issues](https://github.com/openara-ai/media-search-agent/issues).

**How do I contribute?**
See the [Contributing section](../README.md#contributing) in the README.
