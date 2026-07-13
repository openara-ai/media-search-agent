"""MSA desktop-shell provisioning shim (PROJECT-OWNED, committed).

The vendored Tauri supervisor (``src-tauri/src/main.rs``) hardcodes ``python -m app``
(a template gap) and provisions only a **bare** venv — ``uv python install`` +
``uv venv``, no ``uv pip install`` (the upstream template's reference backend was
pure-stdlib). This shim closes both gaps without editing the read-only vendored unit
(ADR-012 vendoring discipline):

  - :mod:`app.responder` binds ``SIDECAR_PORT`` the instant the shim starts and serves
    ``GET /health`` with ``status=provisioning`` + stage + pct, so the supervisor's
    120 s ``wait_ready`` budget succeeds within seconds even on MSA's multi-gigabyte
    first run — with zero vendored-Rust change.
  - :mod:`app.provision` installs MSA's real dependency stack into the app-private venv
    (torch CUDA/CPU gate, requirements, app, facenet-pytorch, the ranker wheel), keyed
    on a fingerprint marker so it is a no-op on every launch after the first.
  - :mod:`app.__main__` wires the order of operations and hands the bound port to the
    real backend sidecar (``msa_apps.search_api.sidecar``).

Everything here is stdlib-only (it runs *before* the venv has MSA's deps), and the
provisioning logic is injectable so the test suite exercises it offline.
"""
