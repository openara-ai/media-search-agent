# Command-line interface

Media Search Agent ships a single `msa` command on installs that run in
the browser: Linux installs, and servers set up with the headless flag
(see [INSTALL.md](INSTALL.md#install--linux-and-servers--headless)). It's
the most direct way to run the indexer, control the API server, and check
what's installed and running.

The **desktop app** on macOS and Windows does not install the CLI — the
app manages its own runtime, indexing runs from the **Indexer** page, and
updating is just installing a newer release, so there's nothing for a CLI
to do. (A dev checkout also gets `msa` via `scripts/dev-setup.sh`.)

After a headless/Linux install the launcher lives at `~/.local/bin/msa`
(macOS/Linux) or is dropped on `PATH` by the Windows bootstrap. Run `msa`
with no arguments to see top-level help.

## Top-level commands

```text
msa status      Show install, service, and index status
msa index       Index media files, export to Qdrant
msa api         Start, stop, restart, and check the API server
msa uninstall   Remove Media Search Agent (installed builds only)
```

Run `msa <command> --help` for full per-command help.

## `msa status`

A single-screen snapshot: install paths, whether the API server and
indexer are running, and how many media items / faces / labeled people
are in the index.

```bash
msa status              # human-readable
msa status --json       # machine-readable, for scripts
```

Useful when something looks wrong — it shows the running PIDs, the log
file paths, and whether the SQLite index is reachable.

## `msa index`

Drives the indexer pipeline.

```bash
msa index run                                # index all configured sources
msa index run --media-source-override /path  # one-off, ignore config sources
msa index run --image-only                   # skip videos
msa index run --dry-run                      # scan and report, no ML, no DB writes
msa index export                             # push embeddings to Qdrant
```

The indexer is incremental — re-runs only touch files that are new or
changed.

## `msa api`

Foreground API server with a small set of lifecycle subcommands. On
headless and Linux installs this is how you start the app; the browser UI
is then at <http://localhost:8000>.

```bash
msa api start                       # foreground; Ctrl+C to stop
msa api start --port 8080           # override port
msa api start --bind-host 0.0.0.0   # accept LAN connections (use with care)
msa api stop                        # stop the running server
msa api restart                     # stop, then start
msa api status                      # is it running?
msa api status --json               # exit 0 = running, 1 = stopped
```

`--bind-host 0.0.0.0` exposes the API to other machines on your network.
The default `127.0.0.1` keeps it local-only. There's no auth in front of
the API today, so only bind to a wider interface on networks you trust.

## `msa uninstall`

Removes the installed app and prompts you about user data:

- **The app itself** (binaries, venv, launcher) is always removed.
- **The index, config, logs, and cache** are kept by default — you'll be
  prompted before anything destructive happens.
- **Your media files** are never touched.

Routing is platform-specific: on macOS and Linux the launcher invokes
`uninstall.sh`; on Windows the `msa.cmd` launcher invokes
`uninstall.ps1`. Only available when run through the installed launcher
— invoking it directly from a dev checkout exits with an error.

## Environment variables

A handful of variables are honoured by every subcommand:

| Variable | Effect |
|---|---|
| `MSA_CONFIG_PATH` | Default config path when `--config` isn't passed |
| `MSA_ROOT` | Install root; set by the launcher, used by `uninstall` |
| `MSA_LOG_DIR` | Override the log directory |
| `MSA_VENV_DIR` | Override the venv path used for status reporting |

For most users none of these need to be set — the launcher wires them up
automatically.

## See also

- [QUICKSTART.md](QUICKSTART.md) — getting from install to first search
- [CONFIGURATION.md](CONFIGURATION.md) — what lives in `config.yaml`
- [FAQ.md](FAQ.md) — common questions about indexing, hardware, privacy
