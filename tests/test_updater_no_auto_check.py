"""Guard: the desktop shell performs NO automatic update check / phone-home at launch.

Global invariant: Media Search Agent is a local-first appliance — it makes no unsolicited
network request when it starts. There is no auto-update; update timing is the user's
decision, not the app's. The Tauri updater must never be invoked automatically at launch.

An automatic launch-time check would contradict the app's "fully offline / nothing phones
home" promise (see README and docs/FAQ.md Privacy), so this guard exists to keep one from
creeping back in.

The updater *plugin* may stay registered — it is inert (no network call unless `.check()`
runs) — so a future *user-initiated* "Check for updates" button can drive it from an
explicit Tauri command. If that lands, gate the check behind that command and update this
guard to scope the exception; never add a launch-time check.

No CI job runs the packaged Rust shell (a human-only DoD — see the tauri.conf.json guards),
so this source-shape guard is what keeps the invariant from silently regressing.
"""
from __future__ import annotations

from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
MAIN_RS = REPO_ROOT / "src-tauri" / "src" / "main.rs"


@pytest.mark.skipif(not MAIN_RS.exists(), reason="src-tauri/src/main.rs absent (public mirror)")
def test_shell_has_no_automatic_update_entrypoint() -> None:
    src = MAIN_RS.read_text()
    # The launch-time auto-update entry point must be gone entirely.
    assert "check_for_updates" not in src, (
        "src-tauri/src/main.rs references `check_for_updates` — the shell must NOT check for "
        "updates automatically at launch (no auto-update, no phone-home). Remove the automatic "
        "check; a user-initiated 'Check for updates' Tauri command is the only allowed path, "
        "and this guard must then be updated to scope that exception."
    )


@pytest.mark.skipif(not MAIN_RS.exists(), reason="src-tauri/src/main.rs absent (public mirror)")
def test_shell_never_invokes_the_updater_at_runtime() -> None:
    src = MAIN_RS.read_text()
    # `.check().await` is the updater's network-egress call. Its presence anywhere in the shell
    # means something can trigger an update fetch; nothing may until a user explicitly asks.
    assert ".check().await" not in src, (
        "src-tauri/src/main.rs invokes the updater (`.check().await`) — that is an unsolicited "
        "network call at launch. The updater may run ONLY from an explicit user action; if you "
        "add that, gate it behind a Tauri command invoked by a UI button and update this guard."
    )
