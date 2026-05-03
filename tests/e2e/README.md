# E2E Functional Tests

Phase 4D.3 adds Playwright browser checks on top of the working Hyper-V installer
flow from `tests/infra/`.

## What lives here

* `package.json` / `package-lock.json` - Playwright test package
* `playwright.config.ts` - browser config and report locations
* `specs/` - user-facing browser scenarios only

The Hyper-V harness remains responsible for VM orchestration, install/launch/smoke,
and artifact collection. The Playwright suite is responsible only for browser-level
checks against a ready application.

## Local entry point

From WSL, use:

```bash
bash tests/infra/run-local.sh \
  --vm-name "Windows 11 dev environment" \
  --checkpoint "clean-slate-2" \
  --scenario installer \
  --run-playwright \
  --guest-username e2euser \
  --guest-password 'StrongTemp123!'
```

The harness copies this package into the guest VM, installs dependencies with `npm ci`,
runs `playwright test` against `http://127.0.0.1:8000`, and copies the Playwright
report/test-results directories back into `.artifacts/local-e2e/<timestamp>/`.
