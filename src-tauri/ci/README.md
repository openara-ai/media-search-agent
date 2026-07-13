# src-tauri/ci — CI lives in `.github/workflows/`, not here (GitHub constraint)

The template's design (REPO_STRUCTURE §6.1) wanted the CI as a **reusable workflow vendored
in `src-tauri/ci/`**, called by a thin per-project caller. **GitHub Actions does not support
that:** a reusable workflow referenced via `uses:` **must live directly in
`.github/workflows/`** — not a subdirectory, not `src-tauri/ci/`. This is why the original
`src-tauri/ci/build.yml` "was never executed" in the spike — it could not have run.

**So the runnable CI is:**

- [`.github/workflows/build.yml`](../../.github/workflows/build.yml) — the **reusable**
  cross-platform build (macOS + Windows; bundled-uv; `publish` input toggles build-test vs
  sign-and-publish).
- [`.github/workflows/ci.yml`](../../.github/workflows/ci.yml) — push/PR caller → `publish: false`
  (the cross-platform **regression test**: compile + bundle + integer-cents engine check).
- [`.github/workflows/release.yml`](../../.github/workflows/release.yml) — tag `v*` caller →
  `publish: true` (build + sign + publish installers + `latest.json` to a GitHub Release).

**Template implication (a CI finding):** the vendored unit can still *carry* the reusable
workflow, but the sync step must **copy it into `.github/workflows/`** (where GitHub requires
it), not leave it in `src-tauri/ci/`. The thin callers (`ci.yml`, `release.yml`) are the
project-authored part. Updater signing uses one fleet key: private key in the GH secret
`TAURI_SIGNING_PRIVATE_KEY` (empty password), public half committed in `tauri.conf > pubkey`.
