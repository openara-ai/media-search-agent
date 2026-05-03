# Quick Start

A 5-minute walkthrough from zero to your first semantic search.

By the end you'll have Media Search Agent running at <http://localhost:8000>,
indexing a folder of your photos, and answering natural-language queries.

## 1. Install

One line. The installer fetches the latest release bundle, sets up an
isolated Python environment, and starts the app.

**macOS (Apple Silicon) and Linux (x86_64):**

```bash
curl -fsSL https://github.com/openara-ai/media-search-agent/releases/latest/download/install.sh | bash
```

**Windows (PowerShell 5.1+):**

```powershell
powershell -c "irm https://github.com/openara-ai/media-search-agent/releases/latest/download/install.ps1 | iex"
```

No admin rights, no git, no Node required.

First run downloads ~1.5 GB of ML model weights — CLIP from OpenAI's CDN,
RT-DETR from Hugging Face, facenet-pytorch from its project GitHub
releases. Allow 5–10 minutes on a typical home connection. Subsequent
launches start in seconds.

For platform requirements, troubleshooting, and uninstall, see [INSTALL.md](INSTALL.md).

## 2. Starting the app

When the install finishes, the app opens automatically in your browser at
<http://localhost:8000>. If it doesn't, visit that URL manually.

A small icon also appears in your **menu bar** (macOS — picture-frame
icon) or **system tray** (Windows — magnifying-glass icon). This is how
you control the app from now on — start, stop, view logs, launch the CLI,
or quit.

**macOS:**

<img src="images/menu-bar-app.png" alt="macOS menu bar app" width="360">

**Windows:**

<img src="images/msa-tray-app.png" alt="Windows system tray app" width="360">

The full menu reference is in [INSTALL.md](INSTALL.md#menu-bar--system-tray-app).
The app re-launches automatically on login by default.

If you click **Quit**, the icon goes away. To bring it back:

- **macOS** — open `MediaSearchAgent` from Spotlight (`⌘ Space`, type
  "media search"), or double-click `~/Applications/MediaSearchAgent.app`
  in Finder.
- **Windows** — open it from the **Start Menu** → "Media Search Agent".

## 3. Add a media folder

1. Click **Indexer** in the navigation.
2. Click **Add media source**.
3. Use the directory picker to navigate to a folder of photos or videos
   (Pictures, a Photos library export, an external drive — anything works).
4. Leave **Read-only** turned on (the default — the app never writes to your
   library).
5. Click **Save**.

You can add more than one source from the same Indexer page. They all index
into the same searchable library.

## 4. Start indexing

Still on the **Indexer** page, click **Run**.

You'll see live progress — file counts, current stage (CLIP embeddings, object
detection, faces), and a streaming log. Indexing speed varies a lot with
hardware (NVIDIA GPU > Apple Silicon > CPU-only), media size, and which
detectors are enabled — see the [FAQ](FAQ.md#indexing) for details.

**Browse** works while indexing is running — new items appear in the grid
as they're processed. **Search** comes online once the run completes.

## 5. Search

Click **Search** and type a natural-language query. A few that work well:

- `sunset over water`
- `kids playing in snow`
- `person holding a coffee cup`
- `dog on a beach`
- `red car at night`

Drag the **threshold slider** to filter by similarity score. Click any result
to open it in the detail drawer with EXIF, GPS, and detected faces.

## 6. Browse by face

Click **People** to see automatically-clustered identities. Click a face to
browse all photos of that person, or label the cluster with a name to make
that person searchable.

## Where to go next

- [Configuration](CONFIGURATION.md) — `config.yaml` reference (models, ports, thresholds)
- [Search guide](features/search.md) — how scoring works, query tips
- [People guide](features/people.md) — face labeling workflow
- [Video guide](features/video.md) — semantic search across video keyframes
- [FAQ](FAQ.md) — privacy, hardware, supported formats, common questions
