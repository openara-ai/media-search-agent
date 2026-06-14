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

[Unreleased]: https://github.com/openara-ai/media-search-agent/compare/v0.3.2...HEAD
[0.3.2]: https://github.com/openara-ai/media-search-agent/compare/v0.3.0...v0.3.2
[0.3.0]: https://github.com/openara-ai/media-search-agent/compare/v0.2.6...v0.3.0
[0.2.6]: https://github.com/openara-ai/media-search-agent/compare/v0.2.0...v0.2.6
[0.2.0]: https://github.com/openara-ai/media-search-agent/releases/tag/v0.2.0
