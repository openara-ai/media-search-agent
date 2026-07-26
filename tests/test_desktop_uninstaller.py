"""
Desktop twin of test_shell_uninstaller.py (M-7/S-5.1, ADR-005).

Verifies the two desktop uninstall entry points implement the removal tiers:
  - scripts/uninstall-desktop.sh (macOS) — Tier 1 always, Tier 2 prompt
    default-KEEP (unattended = keep), Tier 3 never enumerated
  - packaging/windows/msa-installer-hooks.nsh POSTUNINSTALL — Tier 2 rides the
    template's "Delete the application data" checkbox (default unchecked)
    inside the $UpdateMode gate (#191)

Tests are static-analysis only — nothing is installed or removed.
"""

import shutil
import subprocess

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / "scripts" / "uninstall-desktop.sh"
NSH = REPO_ROOT / "packaging" / "windows" / "msa-installer-hooks.nsh"


def _sh_code_lines() -> list[str]:
    """Non-comment, non-empty lines of the macOS uninstaller."""
    return [
        ln.strip()
        for ln in SCRIPT.read_text(encoding="utf-8").splitlines()
        if ln.strip() and not ln.strip().startswith("#")
    ]


def _nsh_postuninstall_code_lines() -> list[str]:
    block = NSH.read_text(encoding="utf-8").split("!macro NSIS_HOOK_POSTUNINSTALL")[1].split(
        "!macroend"
    )[0]
    return [ln.strip() for ln in block.splitlines() if ln.strip() and not ln.strip().startswith(";")]


# ── the macOS script ──────────────────────────────────────────────────────────


class TestDesktopUninstallerScript:
    def test_exists_and_syntax_ok(self):
        assert SCRIPT.exists(), f"desktop uninstaller not found: {SCRIPT}"
        bash = shutil.which("bash") or "bash"
        proc = subprocess.run([bash, "-n", str(SCRIPT)], capture_output=True, text=True)
        assert proc.returncode == 0, f"bash -n failed: {proc.stderr}"

    def test_tier1_runtime_and_bundle_removed_before_any_data_gate(self):
        code = _sh_code_lines()
        gate = code.index('if [[ "$REMOVE_DATA" == "true" ]]; then')
        head = code[:gate]
        assert 'remove_path "$APP_BUNDLE"' in head, "Tier 1 must always remove the app bundle"
        assert 'remove_path "$RUNTIME_DIR"' in head, (
            "Tier 1 must always remove the provisioned runtime (venv/CPython/uv-cache)"
        )

    def test_tier2_data_removed_only_inside_the_opt_in_branch(self):
        code = _sh_code_lines()
        gate = code.index('if [[ "$REMOVE_DATA" == "true" ]]; then')
        for target in ('remove_path "$DATA_DIR"', 'remove_path "$LOG_DIR"', 'remove_path "$MODEL_CACHE"'):
            assert target in code, f"Tier 2 removal missing entirely: {target}"
            assert code.index(target) > gate, f"{target} runs without the Tier-2 opt-in (ADR-005)"

    def test_removes_both_bootstrap_and_dmg_app_locations(self):
        # Finding (Codex): the one-liner installs into ~/Applications but the .dmg
        # path drags the app into /Applications — the uninstaller must remove both,
        # or a DMG user gets a "clean uninstall" with the app still installed.
        text = SCRIPT.read_text(encoding="utf-8")
        assert 'APP_BUNDLE="${HOME:?}/Applications/${APP_NAME}.app"' in text
        assert 'APP_BUNDLE_SYSTEM="/Applications/${APP_NAME}.app"' in text
        code = _sh_code_lines()
        gate = code.index('if [[ "$REMOVE_DATA" == "true" ]]; then')
        head = code[:gate]
        assert 'remove_path "$APP_BUNDLE"' in head
        assert 'remove_path "$APP_BUNDLE_SYSTEM"' in head

    def test_detached_indexer_stopped_before_removing_runtime_or_data(self):
        # Finding (Codex): closing the window during an index leaves `msa index run`
        # detached; uninstall must stop it (cooperative sentinel) BEFORE deleting the
        # venv it runs from ($RUNTIME_DIR) or the DB it writes ($DATA_DIR), or we
        # orphan a GPU job and remove state under a live writer.
        code = _sh_code_lines()
        assert any("stop_detached_indexer()" in ln for ln in code), "helper must exist"
        # It is invoked, and the invocation precedes the Tier-1 runtime removal.
        call = code.index("stop_detached_indexer")  # the bare call line
        # find the *call* (not the definition) — the last occurrence is the call
        call = max(i for i, ln in enumerate(code) if ln.strip() == "stop_detached_indexer")
        runtime_rm = code.index('remove_path "$RUNTIME_DIR"')
        data_rm = code.index('remove_path "$DATA_DIR"')
        assert call < runtime_rm, "indexer must be stopped before the runtime is removed"
        assert call < data_rm, "indexer must be stopped before the index DB is removed"
        # Uses the cooperative stop sentinel + reads the tracked PID file.
        text = SCRIPT.read_text(encoding="utf-8")
        assert 'run_dir="$LOG_DIR/run"' in text and 'indexer.pid' in text
        assert 'indexer.stop' in text, "must write the cooperative stop sentinel"

    def test_detached_indexer_stop_verifies_pid_identity_before_killing(self):
        # A stale indexer.pid (crash leftover) can have its PID reused by an
        # unrelated process; the stop must verify the cmdline looks like our
        # indexer (mirrors the app's _pid_is_indexer) before TERM/KILL, so we never
        # signal a stranger.
        body = SCRIPT.read_text(encoding="utf-8").split("stop_detached_indexer() {")[1].split("\n}")[0]
        assert 'ps -p "$pid" -o args=' in body, "must read the process cmdline for the identity check"
        assert '"$args" == *msa*' in body and '"$args" == *index* && "$args" == *run*' in body, (
            "identity check must require the indexer cmdline shape before signalling"
        )
        # The identity gate must precede any harmful signal (kill -0 is a liveness
        # probe, but TERM/KILL and the stop sentinel must come after the check).
        id_gate = body.index('ps -p "$pid" -o args=')
        for after in ("kill -TERM", "kill -9", "indexer.stop"):
            assert id_gate < body.index(after), f"identity check must run before {after}"

    def test_unattended_keeps_data(self):
        text = SCRIPT.read_text(encoding="utf-8")
        # REMOVE_DATA starts false and the Tier-2 ask defaults to "no" when there
        # is no terminal, so a truly non-interactive run keeps everything
        # (unattended = keep).
        assert "REMOVE_DATA=false" in text
        assert 'ask_tty "Delete this data permanently? [y/N] " no' in text, (
            "Tier-2 delete must default to keep when no terminal is present"
        )
        assert "--keep-data" in text and "--remove-data" in text

    def test_prompts_read_from_dev_tty_so_the_piped_one_liner_confirms(self):
        # Finding #2: the documented uninstall is `curl … | bash`, where stdin is
        # the pipe (`-t 0` false). The confirm must read from /dev/tty so it still
        # fires — otherwise the one-liner removes the app + runtime unconfirmed.
        text = SCRIPT.read_text(encoding="utf-8")
        assert "read -r -p \"$prompt\" answer < /dev/tty" in text, (
            "ask_tty must read from /dev/tty so the piped one-liner still prompts"
        )
        code = _sh_code_lines()
        # Tier-1 confirm routes through ask_tty (not a raw -t 0 gate that the pipe skips).
        assert any('ask_tty "Uninstall Media Search Agent? [y/N] " yes' in ln for ln in code), (
            "Tier-1 confirmation must go through ask_tty (fires on the piped one-liner)"
        )
        # In code (comments stripped), the ONLY `-t 0` is ask_tty's secondary stdin
        # fallback — combined with the /dev/tty attempt via `||`, never a standalone
        # prompt gate at a callsite, which the piped one-liner would defeat.
        t0_lines = [ln for ln in code if "-t 0" in ln]
        assert t0_lines == ['|| { [[ -t 0 ]] && read -r -p "$prompt" answer; }; then'], (
            f"-t 0 must appear only as ask_tty's stdin fallback, got: {t0_lines}"
        )
        ask_body = text.split("ask_tty() {")[1].split("\n}")[0]
        # When NO read succeeds (no terminal, or /dev/tty open-for-read fails), the
        # answer must fall back to the tier default — NOT an empty "no" that would
        # wrongly cancel an unattended Tier-1 uninstall (default "yes").
        assert '[[ "$default" == "yes" ]]' in ask_body, (
            "ask_tty must return the supplied default when the read fails"
        )
        assert 'answer=""' not in ask_body, (
            "a failed read must not fall through to an empty (=no) answer"
        )
        assert '&& read -r -p "$prompt" answer < /dev/tty' in ask_body

    def test_runtime_is_identifier_keyed_and_data_is_name_keyed(self):
        text = SCRIPT.read_text(encoding="utf-8")
        assert 'RUNTIME_DIR="${HOME:?}/Library/Application Support/${APP_ID}"' in text
        assert 'DATA_DIR="${HOME:?}/Library/Application Support/${APP_NAME}"' in text
        assert 'APP_ID="ai.openara.mediasearchagent"' in text
        assert 'APP_NAME="MediaSearchAgent"' in text

    def test_tier3_shared_resources_never_enumerated_in_code(self):
        code = _sh_code_lines()
        for shared in ("huggingface", "open_clip", "insightface", ".local/share/uv"):
            hits = [ln for ln in code if shared in ln]
            assert not hits, f"Tier 3 resource {shared!r} must never appear in code lines: {hits}"
        assert not [ln for ln in code if "sudo" in ln], "uninstaller must never use sudo"

    def test_cli_launcher_removal_is_guarded_to_our_own_symlink(self):
        # Tier 1 removes the opt-in `msa` symlink only when it points into RUNTIME_DIR,
        # never a user's own ~/.local/bin/msa. The guard is -L + readlink + a case match
        # anchored on "$RUNTIME_DIR"/*, and it removes only inside that branch.
        code = _sh_code_lines()
        assert 'CLI_LAUNCHER="${HOME:?}/.local/bin/msa"' in "\n".join(
            SCRIPT.read_text(encoding="utf-8").splitlines()
        )
        assert any('if [[ -L "$CLI_LAUNCHER" ]]' in ln for ln in code)
        assert any('"$RUNTIME_DIR"/*)' in ln for ln in code), (
            "launcher removal must be gated on the symlink resolving into RUNTIME_DIR"
        )
        # The symlink rm sits on the same line as the RUNTIME_DIR case match — never
        # unconditional.
        rm_line = next(ln for ln in code if 'rm -f "$CLI_LAUNCHER"' in ln and "case" not in ln)
        assert '"$RUNTIME_DIR"/*)' in rm_line

    def test_cli_launcher_wrapper_form_removed_only_when_it_targets_our_runtime(self):
        # Finding (Codex): the headless install writes ~/.local/bin/msa as a shell
        # WRAPPER (execs the app-private venv), not a symlink. The symlink-only branch
        # left it behind. The wrapper is removed only when its CONTENTS reference our
        # runtime, so an unrelated user launcher of the same name is preserved.
        code = _sh_code_lines()
        guard = next(
            (ln for ln in code if '[[ -f "$CLI_LAUNCHER" ]]' in ln and "grep -qF" in ln),
            None,
        )
        assert guard is not None, "wrapper removal must be gated on -f + a content match"
        assert '"$RUNTIME_DIR"' in guard, "wrapper removal must match OUR runtime path in the file"
        # An existing-but-foreign launcher (neither our symlink nor our wrapper) is kept.
        assert any('log_skip "Kept: $CLI_LAUNCHER (not ours)"' in ln for ln in code)

    def test_every_rm_is_anchored_to_a_guarded_home(self):
        # All removable paths are fixed constants anchored at ${HOME:?} — the :?
        # guard makes an unset HOME a hard error instead of rm -rf /...
        text = SCRIPT.read_text(encoding="utf-8")
        for var in ("APP_BUNDLE", "RUNTIME_DIR", "DATA_DIR", "LOG_DIR", "MODEL_CACHE", "PREFS_PLIST"):
            assert f'{var}="${{HOME:?}}/' in text, f"{var} must be anchored at ${{HOME:?}}"
        code = _sh_code_lines()
        # remove_path() wraps `rm -rf "$path"`. The one deliberate raw rm is the CLI
        # launcher symlink, which needs an -L + readlink-into-runtime guard remove_path
        # can't express; it is still anchored at ${HOME:?} via CLI_LAUNCHER.
        raw_rm = [
            ln for ln in code
            if ln.startswith("rm ") and '"$path"' not in ln and '"$CLI_LAUNCHER"' not in ln
        ]
        assert not raw_rm, f"rm outside remove_path() (besides the guarded CLI launcher): {raw_rm}"


# ── the Windows NSIS hook ─────────────────────────────────────────────────────


class TestDesktopUninstallTiersWindowsNsh:
    def test_tier2_rides_the_template_checkbox_inside_the_update_gate(self):
        code = _nsh_postuninstall_code_lines()
        update_gate = code.index("${If} $UpdateMode <> 1")
        checkbox = code.index("${If} $DeleteAppDataCheckboxState = 1")
        assert checkbox > update_gate, "Tier 2 must sit inside the $UpdateMode gate (#191)"
        block_end = checkbox + code[checkbox:].index("${Else}")
        block = code[checkbox:block_end]
        for target in (
            'RMDir /r "$PROFILE\\MediaSearchAgent"',
            'RMDir /r "$LOCALAPPDATA\\MediaSearchAgent\\logs"',
            'RMDir /r "$LOCALAPPDATA\\MediaSearchAgent\\Cache\\models"',
        ):
            assert target in block, f"Tier 2 removal missing from the checkbox block: {target}"

    def test_datadir_is_never_removed_outside_the_checkbox_block(self):
        code = _nsh_postuninstall_code_lines()
        checkbox = code.index("${If} $DeleteAppDataCheckboxState = 1")
        block_end = checkbox + code[checkbox:].index("${Else}")
        outside = code[:checkbox] + code[block_end:]
        hits = [ln for ln in outside if "$PROFILE\\MediaSearchAgent" in ln]
        assert not hits, f"DataDir touched outside the Tier-2 opt-in (ADR-005): {hits}"

    def test_default_keep_is_the_documented_posture(self):
        code = _nsh_postuninstall_code_lines()
        assert any("user data kept" in ln for ln in code), (
            "the unchecked/default path must state that Tier-2 data is kept"
        )
