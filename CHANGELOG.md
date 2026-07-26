# Changelog

All notable, user-visible changes to Media Search Agent are documented here.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project follows [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

<!--
  This is the public, user-facing changelog: list only user-visible changes
  (features, fixes, performance). Internal-only work does not belong here.
  Curate the real user-visible entries here before cutting the release.
-->

## [0.5.0] - 2026-07-25

### Added
- Incremental search-index (Qdrant) export: after a run that changed a handful
  of files, the indexer now uploads only those files' vectors instead of
  re-uploading the whole library — the export step after a small change drops
  from minutes to seconds on large libraries. Deleting media now also removes
  its entries from the search index on the next runs, and label or rename
  changes made while no indexer is running are picked up and exported by the
  next run automatically. The first export after upgrading may still be a full
  one; every export after that is incremental. `msa index export` and
  `msa index export --recreate` still perform full exports for repair.
- Incremental indexing fast path in the indexer: re-index runs skip re-reading
  files whose size and modification time are unchanged, so a no-op run over a
  large library finishes in seconds-to-minutes instead of re-reading every byte.
  The first run after upgrading rebuilds this state and is as slow as before;
  every run after is fast.
- The indexer now handles moved or renamed files without re-processing them,
  and detects deleted files: after two consecutive complete scans, removed
  media disappears from browse and search. Both behaviors are configurable via
  the new `incremental:` section in `config.yaml` (`fingerprint_enabled`,
  `deletion_sweep`).
- New `msa index run --verify-content` option re-reads every file's content to
  repair the fast-path records — the safety net for files modified without a
  size or timestamp change.
- Settings: an **Uninstall** section with clean, platform-aware removal — the app and its private runtime go, your index and configuration are kept by default.
- Settings: an optional **Command-line tool** section to add the `msa` command to your PATH for headless indexing (`msa index run`).
- Closing the window while an index is running now asks first and explains that indexing keeps running in the background if you quit.
- Search stays available while the indexer runs. Results reflect your library
  as of the previous run, and a banner says so; only the brief final
  "Finalizing index" step still pauses search, after which new content
  appears. Face labeling and person rename/merge return a clear "try again in
  a moment" response during that finalizing window instead of being silently
  skipped.

### Fixed
- Search results no longer include media whose files were deleted from disk.
- Updating the app on Windows no longer re-downloads the ML models.
- The indexing timer now shows the true elapsed time after the app is reopened mid-run, instead of restarting from zero.

## [0.4.2] - 2026-07-13

### Added
- macOS and Windows: a native desktop app (Linux and servers still run headless in the browser).
- First launch shows staged setup progress and resumes if interrupted.
- Settings: a diagnostics section (app version, backend port, log location).

### Changed
- Updates are user-controlled — the app never checks for or installs updates on its own; install the latest release to update.
- macOS/Windows install is now a thin bootstrap that fetches, verifies (SHA-256), and installs the app; `--headless`/`-Headless` provisions without a GUI.
- Windows 11 or later is now required (was Windows 10 2004+); upgrading cleans up the old background runtime automatically (index and config untouched).
- Windows one-line installer options trimmed to `-Version`, `-Setup`, `-Headless`, `-SkipLaunch` (per-user fixed locations).

## [0.3.2] - 2026-06-14

### Fixed
- The web UI now loads the updated version automatically after you upgrade the app —
  a stale browser-cached page no longer requires a manual hard-refresh.
- macOS: object and scene tags work again. A packaging issue had disabled object
  detection in the macOS app, so photos and videos weren't tagged with detected
  objects.

## [0.3.0] - 2026-05-28

### Added
- Browse: expand a face card into a side drawer for quick context without leaving
  the grid.

### Changed
- Faster Browse "people" filtering on large libraries (indexed query).

### Fixed
- Installer: more reliable startup readiness probing (probe `127.0.0.1`, longer
  per-attempt timeout), clearer two-stage progress output, and hardened
  upgrade/uninstall flows.
- Installer (macOS): banner-first ordering; checksum fetch now fails safe.
- Indexer (Windows): correct detection of dead processes; stopping the indexer no
  longer surfaces as an error.
- Tray (Windows): context menu now shows reliably over Remote Desktop.

## [0.2.6] - 2026-05-09

### Changed
- Windows path selection uses the in-app browser picker only; the unreliable native
  folder picker was removed.

### Fixed
- macOS one-liner installer: version string no longer corrupted by log output.
- Release mirroring: tag publish is idempotent and identity-aware.

## [0.2.0] - 2026-05-06

### Added
- First public release. Local-first semantic search over personal photo and video
  libraries — natural-language search (CLIP), object detection (RT-DETR), and face
  recognition; a Browse workspace; a People page with face labeling; an indexer with
  live progress; and one-line installers for macOS, Linux, and Windows (no Docker,
  embedded Qdrant). Fully offline.

[Unreleased]: https://github.com/openara-ai/media-search-agent/compare/v0.5.0...HEAD
[0.5.0]: https://github.com/openara-ai/media-search-agent/compare/v0.4.2...v0.5.0
[0.4.2]: https://github.com/openara-ai/media-search-agent/compare/v0.3.2...v0.4.2
[0.3.2]: https://github.com/openara-ai/media-search-agent/compare/v0.3.0...v0.3.2
[0.3.0]: https://github.com/openara-ai/media-search-agent/compare/v0.2.6...v0.3.0
[0.2.6]: https://github.com/openara-ai/media-search-agent/compare/v0.2.0...v0.2.6
[0.2.0]: https://github.com/openara-ai/media-search-agent/releases/tag/v0.2.0
