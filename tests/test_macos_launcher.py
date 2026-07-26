from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
# The Swift menu-bar launcher (launcher_app/main.swift) and the .pkg/Platypus
# build (build.sh) were retired in M-7/S-5.5 (macOS ships as the Tauri desktop
# app; the menu bar returns Tauri-native in M-8). Their tests are removed. What
# remains here still covers the kept surfaces: scripts/start.sh (browser/Linux
# mode) and the thin macOS bootstrap installer/macos/shell/install.sh.
MACOS_SHELL_INSTALL = REPO_ROOT / "installer" / "macos" / "shell" / "install.sh"
START_SH = REPO_ROOT / "scripts" / "start.sh"


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def test_start_sh_uses_launch_splash_url_when_opening_browser():
    text = _read(START_SH)

    assert '_launch_url()' in text
    assert 'echo "http://localhost:$port/?launch=1"' in text


def test_start_sh_validates_uvicorn_ownership_before_stopping_processes():
    text = _read(START_SH)

    assert "_is_msa_uvicorn_pid()" in text
    assert '[[ "$args" == *"uvicorn"*' in text
    assert '[[ "$args" == *"$VENV"* ]] || return 1' in text
    assert '_pid_listens_on_port "$pid" "$API_PORT"' in text
    assert 'lsof -nP -tiTCP:"$port" -sTCP:LISTEN' in text
    assert "xargs kill -9" not in text


def test_macos_shell_installer_unloads_launch_agent_by_label():
    text = _read(MACOS_SHELL_INSTALL)

    assert 'LAUNCH_AGENT_LABEL="ai.openara.mediasearchagent"' in text
    assert 'launchctl bootout "gui/$uid/$LAUNCH_AGENT_LABEL"' in text
    assert 'launchctl remove "$LAUNCH_AGENT_LABEL"' in text
    assert 'if launchctl load "$plist"; then' in text
    assert 'log_warn "LaunchAgent load failed for $plist"' in text


def test_macos_shell_installer_prints_banner_before_log_and_mode_chatter():
    """Regression mirroring the Windows-side fix: `setup_logging` used to
    `log_info` the log path, mode, markers, and reason BEFORE `print_banner`
    ran. The user's first visible line was the log path, not the installer
    title.

    Contract: `setup_logging` only sets up the tee-to-logfile; the visible
    Log/Mode/Markers/Reason lines must be printed by `print_banner`, after
    the banner header itself."""
    text = _read(MACOS_SHELL_INSTALL)

    # setup_logging body must not emit the user-visible info lines —
    # those moved into print_banner. Grab the function body between
    # `setup_logging()` and the next top-level function.
    setup_idx = text.index("setup_logging() {")
    # Match the closing brace at the start of a line (end of function).
    next_func_idx = text.index("\nprint_banner()", setup_idx)
    setup_body = text[setup_idx:next_func_idx]
    for forbidden in ('log_info "Log:', 'log_info "Mode:',
                      'log_info "Mode markers:', 'log_info "Mode reason:'):
        assert forbidden not in setup_body, (
            f"{forbidden!r} must not be printed inside setup_logging — "
            "it moves the banner below implementation chatter. Move into "
            "print_banner so the banner is the first visible line."
        )

    # print_banner must contain the banner header AND the log/mode block.
    banner_idx = text.index("print_banner() {")
    next_after_banner = text.index("\n# ──", banner_idx)
    banner_body = text[banner_idx:next_after_banner]
    assert "Media Search Agent Installer" in banner_body
    for required in ("Log:", "Mode:", "Markers:", "Reason:"):
        assert required in banner_body, (
            f"print_banner must surface {required!r} after the banner header "
            "so the visible Log/Mode/Markers/Reason lines stay together "
            "instead of preceding the title."
        )

    # The banner header must appear BEFORE the log line within
    # print_banner itself (header line above any of the four log_info lines).
    header_pos = banner_body.index("Media Search Agent Installer")
    log_pos = banner_body.index("Log:")
    assert header_pos < log_pos, (
        "print_banner: the banner header line must precede the Log: line."
    )


def test_macos_shell_installer_end_printout_is_two_stage():
    """End of install is now two stages instead of one verbose block:

      1. '✓ Media Search Agent installed!'   (install steps complete)
      2. 'Starting the app........'          (live dots while polling /health)
      3. '✓ Media Search Agent started!'     (when /health returns 200)

    The previous version printed a static Starting/Relaunch/Open block
    AFTER the success line, but that ran while the API was still coming
    up - the user saw 'installed!' + static help, then a silent 10-30 s
    gap, then the browser finally opened. The two-stage version uses
    `wait_for_api_ready` to bridge the gap with live feedback.

    This test also pins the no-double-tab contract: `wait_for_api_ready`
    must POLL /health, never `open` the URL itself. The .app's Swift
    launcher is the sole browser opener; calling `open <url>` here too
    would produce a duplicate tab."""
    text = _read(MACOS_SHELL_INSTALL)

    # Stage 1 success line (install).
    assert "Media Search Agent installed!" in text

    # The poll-and-bridge helper must exist and be invoked.
    assert "wait_for_api_ready()" in text, (
        "install.sh must define wait_for_api_ready to bridge the silent "
        "gap between 'installed' and 'browser opens'."
    )
    # Stage 2 live-progress line (printed by wait_for_api_ready).
    assert "Starting the app" in text
    # Stage 3 success line (printed by wait_for_api_ready on /health ready).
    assert "Media Search Agent started!" in text

    # No-double-tab contract: the wait helper must NOT call `open` on
    # the URL. The .app is the sole browser opener.
    waitfn_idx = text.index("wait_for_api_ready()")
    next_fn_idx = text.index("\n}\n", waitfn_idx) + 2
    waitfn_body = text[waitfn_idx:next_fn_idx]
    assert "open http://" not in waitfn_body and 'open "http' not in waitfn_body, (
        "wait_for_api_ready must NOT open the browser URL - that's the "
        ".app's job. Double-opening would produce two tabs."
    )

    # Helper must poll /health (the readiness signal that triggers the
    # .app's own browser-open).
    assert "/health" in waitfn_body, (
        "wait_for_api_ready must poll the /health endpoint to detect "
        "API readiness."
    )

    # 2s post-success pause so the .app's parallel /health poll has time
    # to fire and open the browser tab BEFORE the installer exits and
    # the shell prompt returns. Without it the user can see a brief
    # confusing silence between the '✓ started!' line and the tab.
    # `sleep 2` must appear in the success path (between the started
    # message printf and `return 0`).
    assert "sleep 2" in waitfn_body, (
        "wait_for_api_ready must `sleep 2` after a successful health "
        "check so the .app's parallel poll has time to open the browser "
        "before the installer exits."
    )

    # Probe 127.0.0.1, not localhost, for parity with the Windows fix on
    # PR #133. On macOS curl is normally fine with localhost, but using
    # 127.0.0.1 across both platforms removes a DNS / dual-stack variable
    # and makes the poll behaviour identical.
    assert "127.0.0.1" in waitfn_body, (
        "wait_for_api_ready must probe http://127.0.0.1:<port>/health "
        "for parity with the Windows fix (PS 5.1 hits an IPv6-first "
        "resolution bug otherwise; macOS uses 127.0.0.1 for consistency)."
    )
    # And --max-time must be at least 2s; the previous 1s was tight.
    import re
    m = re.search(r"--max-time\s+(\d+)\b", waitfn_body)
    assert m and int(m.group(1)) >= 2, (
        "wait_for_api_ready's `curl --max-time` must be >= 2s for "
        "breathing room while the API is binding the port."
    )

    # Wall-clock timeout (Codex P2 on PR #133): the timeout MUST be
    # measured by `date +%s` against a start timestamp, not by
    # incrementing a counter once per iteration. Each iteration costs
    # up to curl --max-time + sleep 1 = ~4 s, so a 90-iteration counter
    # could spend ~360 s on a failure path and the warning
    # ("didn't respond within ${timeout}s") would be wildly wrong.
    assert "date +%s" in waitfn_body, (
        "wait_for_api_ready must use `date +%s` to track wall-clock "
        "elapsed time, not an iteration counter. Iteration-counted "
        "timeouts can take 3-4x longer than the printed value because "
        "each iteration blocks on curl + sleep."
    )
    # And the iteration-counter pattern that was there before must be
    # gone so the test catches a regression to the wrong shape.
    assert "elapsed=$((elapsed + 1))" not in waitfn_body, (
        "wait_for_api_ready must not increment a local elapsed counter "
        "per iteration - use wall-clock `date +%s` arithmetic instead."
    )


def test_macos_shell_installer_port_parser_uses_posix_compatible_regex():
    """Codex/Copilot P2 on PR #133: `get_configured_api_port` used `\\b`
    inside an awk regex, which on macOS's default BSD awk is interpreted
    as a backspace escape (not a word-boundary). The pattern then never
    matched the `api:` stanza header in config.yaml, so the function
    always returned the 8000 default - and `wait_for_api_ready` polled
    the wrong port whenever a user customised api.port.

    Replacement is a POSIX-clean anchored pattern that matches the
    stanza header regardless of leading whitespace."""
    text = _read(MACOS_SHELL_INSTALL)

    # Locate the function body so the assertions don't accidentally match
    # the same pattern in unrelated comments.
    fn_idx = text.index("get_configured_api_port()")
    next_fn_idx = text.index("\n# Bridge the gap", fn_idx)
    fn_body = text[fn_idx:next_fn_idx]

    # The broken word-boundary form must be gone from actual awk regex
    # usage. Anchor on the regex-literal form `/^[^#]*\bapi` (or anything
    # starting `/` and immediately containing `\b`) so the historical
    # mention of `\bapi` in the explanatory comment doesn't trigger.
    assert "/^[^#]*\\bapi" not in fn_body, (
        "get_configured_api_port must NOT use `\\bapi` inside an awk "
        "regex literal - BSD awk on macOS treats `\\b` as backspace, "
        "not a word-boundary, so the pattern silently never matches. "
        "Use the POSIX-anchored form instead."
    )
    # POSIX-clean anchored form for the YAML stanza header.
    assert "^[[:space:]]*api[[:space:]]*:[[:space:]]*$" in fn_body, (
        "get_configured_api_port must use the POSIX-safe anchored "
        "pattern `^[[:space:]]*api[[:space:]]*:[[:space:]]*$` to "
        "match the stanza header."
    )

    # The bridging poll must be wired into the main flow's launch path
    # so it runs only when we actually launched the .app.
    assert "wait_for_api_ready 90" in text or "wait_for_api_ready " in text, (
        "wait_for_api_ready must be called from the main install flow "
        "after the .app is launched."
    )

    # Old blocks must be gone.
    assert "print_post_install_message" not in text, (
        "print_post_install_message was folded into the two-stage end; "
        "the function and its caller must be removed."
    )
    assert "Next steps" not in text, (
        "The enumerated 'Next steps' block was removed."
    )
    # The previous static three-line Starting:/Relaunch:/Open: block has
    # been replaced by the live wait helper.
    assert "Starting:  Use the menu bar icon" not in text, (
        "The static 'Starting:  Use the menu bar icon ...' line was "
        "replaced by the live 'Starting the app...' dots in wait_for_api_ready."
    )
    assert "Relaunch:" not in text, (
        "The static 'Relaunch:' line was removed in the two-stage redesign."
    )


def test_macos_shell_installer_banner_communicates_per_user_install_scope():
    """install.sh is per-user (under ~/Applications, ~/Library, etc.) but
    that isn't obvious from the one-liner. The banner must surface this
    so users on shared machines aren't surprised."""
    text = _read(MACOS_SHELL_INSTALL)

    assert "current user only" in text
    assert "other accounts on this machine" in text


def test_macos_shell_installer_runs_system_requirement_pre_flight():
    """Pre-flight system checks must catch "this won't work on your machine"
    cases (old macOS, full disk, low RAM) before any network IO. macOS
    version below 12 is fatal; disk under 5 GB is fatal; RAM is
    warning-only."""
    text = _read(MACOS_SHELL_INSTALL)

    assert "check_system_requirements()" in text

    # macOS version floor: 12 (Monterey).
    assert "sw_vers -productVersion" in text
    assert "macOS 12 (Monterey) or newer required" in text

    # Disk space: 5 GB floor using df -k.
    assert "df -k" in text
    assert "5 GB" in text

    # RAM: warning only via sysctl on macOS, /proc/meminfo on Linux.
    assert "hw.memsize" in text
    assert "/proc/meminfo" in text
    assert "8+ GB recommended" in text

    # Must run BEFORE resolve_version so we don't hit the network if
    # the machine fails the check.
    sysreq_idx = text.index("check_system_requirements\n")
    resolve_idx = text.index('version="$(resolve_version)"', sysreq_idx)
    assert sysreq_idx < resolve_idx, (
        "System requirements check must run before resolve_version."
    )


def test_macos_shell_installer_distinguishes_intel_mac_from_rosetta_translation():
    """`uname -m` reports x86_64 both on real Intel Macs and on Apple Silicon
    running under Rosetta. Without distinguishing these, an M-series user
    who happens to invoke the installer from an x86_64-translated shell
    gets the wrong (and unactionable) "Intel Mac not supported" error.
    The installer must check sysctl.proc_translated and tell Apple Silicon
    users to re-run under arm64."""
    text = _read(MACOS_SHELL_INSTALL)

    assert "sysctl -n sysctl.proc_translated" in text
    assert "Apple Silicon Mac" in text
    assert "arch -arm64" in text

    # Rosetta detection must come BEFORE the generic "Intel Mac" die so
    # the more-specific error wins.
    rosetta_idx = text.index("sysctl.proc_translated")
    intel_die_idx = text.index('die "Intel Mac (x86_64) is not yet supported')
    assert rosetta_idx < intel_die_idx, (
        "Rosetta detection must run before the Intel Mac die so the "
        "more-specific error message wins for Apple Silicon users."
    )


def test_macos_shell_installer_verifies_bundle_sha256_against_published_sums():
    """The unsigned installer fetches a .tar.gz over HTTPS from GitHub
    Releases. SHA256SUMS.txt is published alongside; the installer must
    verify the downloaded bundle against it before tar -xzf. A mismatched
    bundle must die *before* touching any install directory."""
    text = _read(MACOS_SHELL_INSTALL)

    # Helper exists and is called from the download path.
    assert "verify_bundle_sha256()" in text
    assert 'verify_bundle_sha256 "$bundle_archive"' in text

    # Fetched from the release-relative URL, not a hardcoded one.
    assert "SHA256SUMS.txt" in text

    # Uses shasum (macOS default) or sha256sum (Linux default).
    assert "shasum -a 256" in text
    assert "sha256sum" in text

    # Mismatch is fatal (die) and runs BEFORE tar -xzf.
    assert "Bundle SHA256 mismatch" in text
    sha_call_idx = text.index('verify_bundle_sha256 "$bundle_archive"')
    extract_idx = text.index('tar -xzf "$bundle_archive"')
    assert sha_call_idx < extract_idx, (
        "SHA256 check must run before tar -xzf so a tampered bundle never "
        "reaches the extract step."
    )

    # M-7/S-4: the legacy bundle verify now HARD-FAILS on a missing / 404 /
    # not-listed checksum — the last warn-and-continue is closed now that every
    # release ships SHA256SUMS.txt over all assets (parity with the thin
    # bootstraps' verify_thin_sha256 / Test-SetupSha256). It still captures the
    # HTTP status so the 404 case gets a specific, actionable die() message.
    assert "--write-out '%{http_code}'" in text, (
        "verify_bundle_sha256 must capture the HTTP status code via curl "
        "--write-out so a 404 gets a specific message."
    )
    sha_func_idx = text.index("verify_bundle_sha256()")
    next_func_idx = text.index("\n# ── Version resolution", sha_func_idx)
    sha_body = text[sha_func_idx:next_func_idx]
    assert "skipping integrity check" not in sha_body, (
        "M-7/S-4 closed the warn-and-continue path — a missing/404/not-listed "
        "checksum must die(), not warn-and-proceed."
    )
    assert "HTTP 404" in sha_body  # still branched — but now a hard fail
    assert "Refusing to install an unverified bundle" in sha_body, (
        "A 404 / non-200 SHA256SUMS fetch must die() with a clear remediation."
    )
    assert "Refusing to install a bundle with no published checksum" in sha_body, (
        "A bundle not listed in SHA256SUMS.txt must die(), not warn-and-proceed."
    )

    # Local --bundle path skips verification.
    bundle_branch_idx = text.index("Using local bundle:")
    next_else_idx = text.index("else", bundle_branch_idx)
    local_branch = text[bundle_branch_idx:next_else_idx]
    assert "verify_bundle_sha256" not in local_branch, (
        "Local --bundle path must skip verify_bundle_sha256."
    )


def test_macos_shell_installer_refuses_version_downgrade_by_default():
    """A user re-running the one-liner with --version v0.2.0 after upgrading
    to v0.3.0 must NOT silently downgrade - downgrades can corrupt
    index/media.sqlite if the schema moved forward between versions. The
    guard must be opt-out via --allow-downgrade, not opt-in."""
    text = _read(MACOS_SHELL_INSTALL)

    # CLI plumbing
    assert "--allow-downgrade)" in text
    assert "OPT_ALLOW_DOWNGRADE=1" in text
    assert "--allow-downgrade" in text  # appears in usage too

    # Version-compare helper strips `v` prefix and `-prerelease` suffix.
    assert "parse_msa_version()" in text
    assert 'tag="${1#v}"' in text
    assert 'echo "${tag%%-*}"' in text

    # Guard reads VERSION_FILE, uses the portable `version_lt` comparator,
    # and dies on downgrade unless --allow-downgrade is set. The previous
    # implementation used `sort -V` which isn't portable to minimal BSDs /
    # older macOS / stripped Linux containers, so it was replaced by a
    # pure-bash dotted-numeric comparator.
    assert "check_version_downgrade()" in text
    assert "VERSION_FILE=" in text
    assert "version.txt" in text
    assert "version_lt()" in text, (
        "install.sh must define a `version_lt` function — replaces the "
        "non-portable `sort -V` comparator."
    )
    assert "| sort -V" not in text, (
        "install.sh must NOT pipe into `sort -V` for version comparison; "
        "older BSDs and minimal containers do not ship it, and `set -euo "
        "pipefail` would abort the installer on those targets. Use the "
        "bash-native `version_lt` helper instead. (The historical comment "
        "may still mention `sort -V` as context — that's fine; this "
        "assertion targets actual usage via the pipe form.)"
    )
    # The comparator must guard against non-numeric components so a bad
    # version_file entry can't crash the arithmetic compare.
    assert "[[ \"$x\" =~ ^[0-9]+$ ]] || x=0" in text
    # And it must drive both branches of check_version_downgrade.
    assert 'if version_lt "$new_num" "$existing_num"; then' in text
    assert 'elif version_lt "$existing_num" "$new_num"; then' in text
    assert 'Refusing to downgrade' in text
    assert 'OPT_ALLOW_DOWNGRADE' in text
    assert 'if [[ ( "$INSTALL_MODE" == "upgrade" || "$INSTALL_MODE" == "repair" ) && -n "$version" ]]; then' in text, (
        "install.sh must run the downgrade guard for both upgrade and repair "
        "modes. Partial install state can still include version.txt, so "
        "repair-mode downgrades carry the same SQLite schema risk."
    )
    assert 'if [[ "$INSTALL_MODE" == "upgrade" && -n "$version" ]]; then' not in text, (
        "The old upgrade-only guard call site must not come back."
    )

    # Legacy installs (no version file) must NOT trip the guard - missing
    # file is a silent no-op, not an error.
    assert '[[ -f "$version_file" ]] || return 0' in text

    # Local-bundle installs skip the check since the version is unknown.
    assert '[[ "$new_tag" == "(local bundle)" ]] && return 0' in text

    # Marker is written only after auto-start setup succeeds - if anything
    # above failed we must NOT advance the recorded version.
    assert "install_systemd_service" in text
    assert 'echo "$version" > "$VERSION_FILE"' in text
