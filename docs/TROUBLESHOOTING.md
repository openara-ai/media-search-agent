# Troubleshooting

When Media Search Agent won't start, gets stuck during setup, or misbehaves at
runtime, everything it did is recorded in a small set of local log files. This
guide shows where those files live and how to read the most common failure
signatures yourself.

For install-time basics (Gatekeeper/SmartScreen prompts, slow model downloads,
GPU detection) see the [Installation guide's troubleshooting
section](INSTALL.md#troubleshooting) first.

## Where the logs live

| Platform | Log folder |
|---|---|
| macOS | `~/Library/Logs/MediaSearchAgent/` |
| Windows | `%LOCALAPPDATA%\MediaSearchAgent\logs\` |
| Linux / headless | `~/.local/share/MediaSearchAgent/logs/` |

Open it quickly:

- **macOS** — in Finder press `Cmd-Shift-G` and paste the path, or in Terminal:
  `open ~/Library/Logs/MediaSearchAgent`
- **Windows** — press `Win-R` and paste `%LOCALAPPDATA%\MediaSearchAgent\logs`

What each file is:

| File | Contents |
|---|---|
| `msa-desktop.log` | The unified desktop log: every launch, setup progress, backend startup, and any error with a full traceback. **Start here.** |
| `provision-<timestamp>.log` | One per setup attempt — the full output of the dependency installer. The setup error screen points at the newest one. |
| `msa.log` | The search and indexing engine (queries, indexer runs, model loading). |
| `sidecar-port` | The local port the backend bound on the most recent launch (plain text). |
| `msa-api.lock` | The single-instance lock — the process ID of the running backend. |

All of it is plain text, local-only, and safe to open in any editor.

## Reading a failure

Open `msa-desktop.log`, jump to the **end**, and scan upward for the last
`ERROR` line or Python traceback — the final few lines almost always name the
problem. The most common signatures:

### "Media Search Agent API is already running (PID …)"

A previous backend process didn't exit and is still holding the
single-instance lock, so every new launch shuts itself down and the window
sits on "Starting up". To recover:

1. Quit the Media Search Agent window if one is open.
2. Find and stop the leftover backend process:

   **macOS** (Terminal):

   ```bash
   ps aux | grep -i mediasearchagent | grep -v grep
   kill <PID>
   ```

   **Windows** (PowerShell):

   ```powershell
   Get-CimInstance Win32_Process |
     Where-Object { $_.CommandLine -match 'mediasearchagent' } |
     Select-Object ProcessId, Name
   Stop-Process -Id <PID>
   ```

3. Relaunch the app.

If the message names a PID that doesn't exist at all, delete the
`msa-api.lock` file from the log folder and relaunch (the app also does this
cleanup itself on the next start).

### "Setup could not finish" / "Installing MediaSearchAgent failed (uv exit 1)"

Open the newest `provision-<timestamp>.log`; its last lines show the exact
installer command that failed and why. Two frequent causes:

- **macOS: `Read-only file system`** — the app was launched straight from the
  downloaded disk image (or from the Downloads folder). Drag
  **MediaSearchAgent** into **Applications**, eject the disk image, and launch
  it from Applications.
- **A network drop during the first-run downloads** — just relaunch. Setup
  resumes from the step that failed; finished steps aren't re-downloaded.

### Slow launch while the splash shows "Loading backend"

Normal, especially on Windows with an NVIDIA GPU: before the window opens,
the backend loads the ML libraries, and the CUDA build maps several gigabytes
of libraries on first load — commonly 30 seconds or more on the first launch
after a boot (antivirus scanning of newly loaded libraries adds to it). Later
launches in the same session are much faster. If it goes well past a minute
with no progress, treat it as stuck (next section).

### Stuck on "Starting up"

The very first launch installs Python and the ML libraries and can genuinely
take a few minutes. If it goes well past that, check the end of
`msa-desktop.log`:

- New lines still appearing → setup is working; leave it running.
- The log ends in an error → match it against the signatures above.
- The log shows `Application startup complete` but the window never changes →
  do the [clean restart](#clean-restart-checklist) below.

## Clean restart checklist

1. Quit Media Search Agent.
2. Check for leftover processes and stop any you find (commands in the
   ["already running" section](#media-search-agent-api-is-already-running-pid-)).
3. Relaunch. Watch the end of `msa-desktop.log` if you want to follow along.

Your index, configuration, and labeled people are never affected by a
restart — they live in the data folder, not the runtime.

---

For anything not covered here, check [the FAQ](FAQ.md) or open an issue at
[github.com/openara-ai/media-search-agent/issues](https://github.com/openara-ai/media-search-agent/issues).
The logs are yours and stay on your machine — if you do quote log lines in an
issue, note that they can include the names and paths of your media folders.
