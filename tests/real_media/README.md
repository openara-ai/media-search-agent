# Real Media Fixtures

This directory holds **publicly reusable media fixtures** for local validation of:

- face detection
- object detection
- metadata extraction
- scene detection
- keyframe extraction
- end-to-end indexing and API checks

These fixtures are intended for a **developer-run local real-data workflow**. See "How To Run" below.

## Goals

The fixture set should be:

- safe to publish in a public GitHub repository
- small enough to clone reasonably
- diverse enough to exercise faces, objects, metadata, and video processing
- stable enough for threshold-based assertions
- capable of deterministic face-labeling and similar-face checks

## Structure

```text
tests/real_media/
  README.md
  MANIFEST.md
  THIRD_PARTY_MEDIA.md
  fixtures/
    originals/
    derived/
```

## Originals vs Derived

### `fixtures/originals/`

Contains the original downloaded public media files.

Rules:

- keep filenames stable
- do not edit these files in place
- preserve provenance in `THIRD_PARTY_MEDIA.md`

### `fixtures/derived/`

Contains reduced-size or metadata-modified copies for fast tests.

Examples:

- EXIF-enriched image copies
- trimmed videos
- lower-resolution copies for smaller repo footprint

Current examples in this repo:

- `exif_face_expressive_01.jpg`
- `exif_gps_face_01.heic`
- `trimmed_video_people_crowd_01.webm`
- `trimmed_video_dog_object_01.webm`
- `trimmed_video_dog_motion_01.webm`

Rules:

- generate from `originals/`
- record significant changes in `THIRD_PARTY_MEDIA.md`
- use these files in automated tests unless the original is required

## Suggested Fixture Set

### Images

- `face_single_01.jpg`
- `face_single_02.jpg`
- `face_expressive_01.jpg`
- `face_group_01.jpg`
- `face_same_person_01.jpg`
- `face_same_person_02.jpg`
- `object_dog_01.jpg`
- `object_landscape_01.jpg`
- `exif_gps_face_01.heic`

### Videos

- `video_face_single_01.webm`
- `video_people_crowd_01.webm`
- `video_street_objects_01.webm`
- `video_dog_object_01.webm`
- `video_dog_motion_01.webm`

## How To Add Media

1. Verify the license on the source page.
2. Prefer `CC0`, public domain, or plain `CC BY`.
3. Download the file into `fixtures/originals/`.
4. Add a provenance entry to `THIRD_PARTY_MEDIA.md`.
5. Add expected assertions to `MANIFEST.md`.
6. If needed, generate a smaller or metadata-modified copy in `fixtures/derived/`.

## How To Run

### Fast fixture checks

Run the fixture-only checks from the main repo. This path is supported on both
Linux / WSL2 and macOS:

```bash
bash scripts/dev-cli.sh pytest tests/real_media/test_real_media_fixtures.py -v -m "not slow"
```

This validates:

- fixture inventory
- EXIF-enriched derived images
- video metadata and duration fallback
- frame extraction
- scene/keyframe smoke behavior

### Slow model-backed checks

Run the optional model-backed checks:

```bash
bash scripts/dev-cli.sh pytest tests/real_media/test_real_media_fixtures.py -v -m "slow"
```

These currently exercise:

- face detection on a real portrait
- object detection on the dog fixture

### Full local real-data harness

Run the end-to-end local harness. This is intended for both Linux / WSL2 and
macOS developer environments.

```bash
bash tests/real_media/run-local.sh --report-dir .artifacts/real-data
```

This will:

- create an isolated temp workspace under `/tmp/msa-realdata-<branch>-<timestamp>-<suffix>/`
- materialize the current branch snapshot there
- run the fast fixture checks
- index `tests/real_media/fixtures/`
- start the API
- run the runtime validation suite
- copy logs and summary to `.artifacts/real-data/<run-id>/`

Useful variants:

```bash
bash tests/real_media/run-local.sh --skip-slow-model-checks --report-dir .artifacts/real-data
bash tests/real_media/run-local.sh --source-mode dirty --keep-workspace always --report-dir .artifacts/real-data
```

## Bundle Installer Validation (BVT)

The dev-env harness above validates the source tree. Bundle installer validation
runs the **same `tests/real_media/`** against an *installed product*, exercising
the artifacts a real user would download. This is the most important basic
validation test for releases — green BVT means the released bundle works.

Driven by [`.github/workflows/bvt.yml`](../../.github/workflows/bvt.yml)
on `workflow_dispatch` (and `workflow_call` from `release.yml`). Not wired to
PRs while we're in pre-release; manually dispatched to control CI cost.

For a downloadable desktop installer that can exercise a manual upgrade over an
installed release, set the dispatch input `artifact_version` to a SemVer
prerelease greater than the installed version (for example, `0.4.3-bvt.1` over
`0.4.2`) and enable `upload_installer`. Leaving `artifact_version` empty keeps
the routine BVT placeholder `0.0.0`. The input affects only `bvt.yml`; tagged
release builds continue to derive their version from the release tag.

### Three contracts verified end-to-end

1. **Bundle-install contract.** The tarball/zip produced by `build-bundle.sh`
   installs cleanly on a clean user space, lays down the directories the
   launcher expects, and the `msa` launcher resolves correctly.
2. **Indexer contract.** Running the *installed* indexer against fixture media
   produces a SQLite with the expected rows, embedding tables, GPS data, etc. —
   same assertions as `test_real_media_runtime.py::TestIndexedArtifacts`.
3. **API contract.** Hitting `/search`, `/media`, `/faces`, `/people`,
   `/videos/{id}/shots` against the indexed state returns the expected shapes
   and content — same assertions as `TestRuntimeApi`.

### Same shape as dev-env, different layers

| Step | Dev-env | Bundle |
| --- | --- | --- |
| 0. Build artifact | n/a | `build-bundle.sh` produces `MediaSearchAgent-X.Y.Z-<plat>.<tar.gz\|zip>` from `git archive HEAD` + UI dist + bundled `uv` / `exiftool` / `mediainfo` (and the tray exe on Windows). The *same artifact* a release would publish. |
| 1. Fresh workspace | `git clone` to a temp dir | `install.sh --bundle …` (or `install.ps1 -Bundle …`) into an isolated user space — `$RUN_ROOT/msa-<plat>-home` on Linux/macOS, `$RUN_ROOT/msa-app` + `$RUN_ROOT/msa-data` on Windows |
| 2. Bring up the runtime | `python -m venv .venv` + `pip install` + preload models + `start.sh` | Installer already created the venv via the bundled `uv`. Add test-only deps via the *installed* `uv`. Index fixtures via `msa index run`; start API via `msa api start`. |
| 3. Run the contracts | `pytest tests/real_media/` | **Same** `tests/real_media/` files, copied to `$RUN_ROOT/msa-real-media-tests/` (outside the install). Run them with the *installed venv's* Python. |

Step 0 is the only new step. Step 3 runs the *exact same test files*; what
differs is (a) the Python that runs them, (b) the path to fixtures, (c) the
path to SQLite — all communicated via `MSA_REALDATA_*` env vars.

### Walkthrough — Linux

[`tests/real_media/scripts/validate-installed-bundle-linux.sh`](scripts/validate-installed-bundle-linux.sh)

```text
1. Isolate the workspace
   export HOME=$RUNNER_TEMP/msa-linux-home
   (runtime ignores XDG_* on Linux per ADR-009, so HOME alone redirects
    every platform default)

2. Stage tests OUTSIDE the install
   cp -r $REPO/tests/real_media/. $RUNNER_TEMP/msa-real-media-tests/
   (the bundle does not ship tests; they're external collateral)

3. Install the bundle as an end user would
   bash $REPO/installer/macos/shell/install.sh --bundle <tarball> --skip-autostart
   ⇒ ~/.local/share/MediaSearchAgent/{src,scripts,bin,.venv,…}
      ~/.config/MediaSearchAgent/config.yaml
      ~/.local/bin/msa     ← the launcher

4. Add test-only deps to the installed venv
   $APP_DIR/bin/uv pip install --python $APP_DIR/.venv/bin/python \
       -r $REPO/tests/requirements-ci.txt

5. Patch the *installed* config to point at staged fixtures
   media_sources: [{ name: "Real Media Fixtures",
                     path: <staged>/fixtures, read_only: true }]
   api: { host: 127.0.0.1, port: 18080 }

6. Run the indexer through the installed launcher
   msa index run --config <staged config>
   (the launcher exports MSA_ROOT/MSA_DATA_DIR/MSA_VENV_DIR/MSA_CONFIG_PATH/…
    and execs the bundle's start.sh + venv python)
   ⇒ ~/.local/share/MediaSearchAgent/index/media.sqlite,
      data/thumbnails/, data/face_thumbnails/

7. Start the API through the installed launcher
   msa api start --no-browser &
   poll http://127.0.0.1:18080/health until "ready"

8. Run the contract tests with the installed venv's Python
   $APP_DIR/.venv/bin/python -m pytest \
       $TEST_ROOT/test_real_media_fixtures.py     # bundled exiftool/mediainfo + fixtures
   $APP_DIR/.venv/bin/python -m pytest \
       $TEST_ROOT/test_real_media_runtime.py      # SQLite + /search /faces /people /videos

9. Stop the API
   msa api stop
```

### Per-platform variations

Same nine steps; the differences are install layout and launcher invocation:

| | Linux | macOS | Windows |
| --- | --- | --- | --- |
| Install command | `bash install.sh --bundle …` | `bash install.sh --bundle …` | `powershell -File install.ps1 -Bundle … -AppDir … -DataDir …` |
| App dir | `~/.local/share/MediaSearchAgent/` | `~/Applications/MediaSearchAgent.app/Contents/Resources/` | `<AppDir>\` (under `runner.temp`) |
| Venv | `<AppDir>/.venv/` | `<AppDir>/.venv/` | `<AppDir>\.venv\` |
| Launcher | `~/.local/bin/msa` | `~/.local/bin/msa` | `<AppDir>\bin\msa.cmd` |
| API port | 18080 | 18081 | 18082 |
| Validator | [validate-installed-bundle-linux.sh](scripts/validate-installed-bundle-linux.sh) | [validate-installed-bundle-macos.sh](scripts/validate-installed-bundle-macos.sh) | [validate-installed-bundle-windows.ps1](scripts/validate-installed-bundle-windows.ps1) |

Because steps 6 and 7 go through the launcher, the bundle path exercises:

- the *bundled* `uv`, `exiftool`, `mediainfo`
- the *installed* venv's pinned package versions (not whatever the dev tree has)
- the launcher's env-var exports (`MSA_*`)
- the start/stop scripts as packaged
- the runtime's platform path defaults

### Where this is *not* a true end-user flow

These are intentional deviations from "exactly what a user does":

| Deviation | Reason | End-user impact |
| --- | --- | --- |
| `HOME` / `-AppDir` / `-DataDir` overridden to `$RUN_ROOT` | Keeps the runner's actual home clean; lets the job tear itself down | None — the install is real, just under a different root |
| Pytest invoked via `$VENV_PY -m pytest` directly, not through the launcher | The launcher has no `pytest` passthrough subcommand | If a user ran `python -m msa_indexer.pipeline` outside the launcher, they'd hit whatever the runtime platform default resolves to. Linux/macOS validators after the path-alignment fix verify that path; Windows still sets `MSA_CONFIG_PATH` because of the `-AppDir` override. |
| Test deps added to the installed venv after the fact | Bundle ships product deps only; tests need pytest, faiss-cpu, transformers, etc. | None — purely a test-rig concern |
| `MSA_DEVICE=cpu` set in all three validators | The macos-14 runner can't reliably hold MPS for back-to-back model loads; pinning CPU on the others keeps behavior consistent | None for users — local installs detect their own device |

### Mental shortcut

Think of the validator as: *"`npm install` the released package, then run the
package's docs against a tiny demo project."* The package = the bundle. The
demo project = `tests/real_media/`. The "docs" being run = pytest assertions
about indexer output and API behavior.

Three signals on every dispatched run:

- **Build is shippable** (step 0)
- **Install + launcher work as a user would experience them** (steps 1–3, 6–7)
- **The product behaves correctly on real fixtures** (step 8)

## Logs And Reports

### Temp workspace logs

Each harness run writes full logs inside the temp workspace:

```text
/tmp/msa-realdata-<branch>-<timestamp>-<suffix>/artifacts/
  indexer.log
  api.log
  pytest-fast.log
  pytest-runtime.log
  pytest-slow.log
  summary.md
```

Default retention:

- successful runs: temp workspace is deleted
- failed runs: temp workspace is preserved

### Durable report copy

If `--report-dir` is provided, the harness copies logs and summary into:

```text
.artifacts/real-data/<run-id>/
```

This is the easiest place to inspect results after a successful run.

Start with:

- `summary.md` for overall PASS/FAIL and phase status
- `indexer.log` for indexing and shot/keyframe processing details
- `api.log` for uvicorn startup and endpoint activity
- `pytest-runtime.log` for live API/runtime assertion failures

## Recommended Video Trimming

Keep video fixtures short for repo size and test speed.

Example trim with `ffmpeg`:

```bash
ffmpeg -ss 00:00:02 -i fixtures/originals/video_street_objects_01.webm -t 8 -c:v libvpx-vp9 -crf 36 -b:v 0 -an fixtures/derived/trimmed_video_street_objects_01.webm
```

Guidelines:

- target 5-15 seconds
- drop audio unless the test needs it
- prefer a lower bitrate for repo-friendly size
- keep enough motion for scene/keyframe tests

## Recommended EXIF Injection

Apply metadata only to files in `fixtures/derived/`.

Example:

```bash
exiftool \
  -overwrite_original \
  -Make="NIKON CORPORATION" \
  -Model="NIKON D2X" \
  -LensModel="AF-S VR Zoom-Nikkor 70-200mm f/2.8G IF-ED" \
  -GPSLatitude=38.715384 \
  -GPSLatitudeRef=N \
  -GPSLongitude=9.141461 \
  -GPSLongitudeRef=W \
  fixtures/derived/exif_gps_face_01.jpg
```

Example HEIC conversion from an existing public fixture:

```bash
magick fixtures/originals/face_single_01.jpg -quality 92 fixtures/derived/exif_gps_face_01.heic
exiftool \
  -overwrite_original \
  -Make="Apple" \
  -Model="iPhone 15 Pro" \
  -LensModel="iPhone 15 Pro back triple camera 6.86mm f/1.78" \
  -GPSLatitude=37.7749 \
  -GPSLatitudeRef=N \
  -GPSLongitude=122.4194 \
  -GPSLongitudeRef=W \
  fixtures/derived/exif_gps_face_01.heic
```

## Assertion Style

Use threshold-based assertions instead of exact model outputs.

Good:

- at least 1 face detected
- at least 2 keyframes extracted
- expected labels include `dog`

Avoid:

- exactly 3 faces
- exact floating-point confidence values
- exact keyframe counts across model/runtime changes

## Notes

- Some publicly licensed files that depict identifiable people may still carry
  personality/privacy considerations separate from copyright.
- Keep this fixture set focused. It is better to have 10 well-understood files
  than 200 weakly documented ones.
