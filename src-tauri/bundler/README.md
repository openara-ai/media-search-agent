# src-tauri/bundler — packaging machinery (TEMPLATE-OWNED, vendored)

Generic, fleet-wide packaging config. A project never edits this.

## macOS entitlements — NOT needed for the bundled-uv Python backend

The PyInstaller spike shipped a `macos-entitlements.plist` granting
`com.apple.security.cs.disable-library-validation` (+ `allow-dyld-environment-variables`,
`allow-unsigned-executable-memory`). That entitlement existed for **one reason**: a
PyInstaller `--onefile` sidecar `dlopen()`s an embedded `Python.framework` **inside the
host process**, and macOS **Library Validation** (active under Hardened Runtime) refused to
load that framework because it carried a different Team ID than the app
(`different Team IDs`, exit 255).

**The bundled-uv backend removes the need for it entirely.** With uv, the interpreter is a
**separate child process** (`…/.venv/bin/python -m app`) that the shell *spawns*, not an
in-process framework it `dlopen`s. Library Validation governs in-process library loading,
**not** child-process `exec`. So a Hardened-Runtime app — signed with **no entitlements** —
spawns the (ad-hoc-signed, no-Team-ID) uv CPython and it runs fine. Verified in the macOS
uv re-validation spike (see the repo `FRICTION_LOG.md`, §1): the wall is **gone, not moved.**

That is why this directory ships **no entitlements plist**: a Rust sidecar never needed one,
and the uv Python sidecar doesn't either. If a future backend genuinely needs an in-process
`dlopen` of a differently-signed dylib, re-introduce a plist here — and keep it
**comment-free** (`codesign`/AMFI rejects XML comments).
