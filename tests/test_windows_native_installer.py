import re
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parents[1]
WINDOWS_TEMPLATE = REPO_ROOT / "installer" / "windows-native" / "config.windows.yaml.template"
WINDOWS_START = REPO_ROOT / "installer" / "windows-native" / "start.ps1"
TRAY_PROGRAM = REPO_ROOT / "installer" / "windows-native" / "tray" / "Program.cs"


def _start_text() -> str:
    return WINDOWS_START.read_text(encoding="utf-8")


def test_windows_native_installer_config_template_is_valid_yaml():
    """The checked-in Windows template must be valid YAML with required fields."""
    assert WINDOWS_TEMPLATE.exists(), f"Template not found: {WINDOWS_TEMPLATE}"
    config = yaml.safe_load(WINDOWS_TEMPLATE.read_text(encoding="utf-8"))
    assert isinstance(config, dict)
    assert config["media_sources"] == [] or config["media_sources"] is None
    assert config["api"]["port"] == 8000
    assert config["retrieval"]["search_score_trace"] is False


def test_windows_native_installer_example_media_source_is_yaml_safe_when_uncommented():
    """The Windows template example media sources must be valid YAML when uncommented."""
    assert WINDOWS_TEMPLATE.exists(), f"Template not found: {WINDOWS_TEMPLATE}"
    config_text = WINDOWS_TEMPLATE.read_text(encoding="utf-8")
    # Simulate a user uncommenting the example sources:
    # replace the empty-list default with a block and strip the comment markers
    lines = []
    for line in config_text.splitlines():
        if line == "media_sources: []":
            lines.append("media_sources:")
        elif re.match(r"^#   - ", line) or re.match(r"^#     ", line):
            lines.append(line[1:])  # strip leading #
        else:
            lines.append(line)
    uncommented = "\n".join(lines)

    config = yaml.safe_load(uncommented)
    sources = config.get("media_sources") or []
    assert isinstance(sources, list), "media_sources must be a list when uncommented"
    assert len(sources) > 0, "at least one example source must be present in the template"
    for source in sources:
        assert "name" in source
        assert "path" in source


def test_shell_installer_falls_back_to_run_key_when_task_scheduler_is_denied():
    text = (
        REPO_ROOT / "installer" / "windows-native" / "shell" / "install.ps1"
    ).read_text(encoding="utf-8")

    assert "function Install-RunKeyAutoStart" in text
    assert "HKCU:\\Software\\Microsoft\\Windows\\CurrentVersion\\Run" in text
    # The fallback message stays - it's how the user knows auto-start is wired up.
    assert "Falling back to per-user Run registry auto-start" in text

    # Regression guard: the verbose "Task Scheduler registration failed: ..."
    # warning was removed because Group Policy / locked-down VMs hit it
    # routinely and the HKCU Run-key fallback works fine. Adding it back
    # would re-introduce confusing red noise on otherwise-successful installs.
    assert "Task Scheduler registration failed" not in text, (
        "install.ps1 must not Write-Warn 'Task Scheduler registration failed' "
        "- the fallback message alone is intentional. Group-Policy and "
        "locked-down VM installs hit the catch block routinely and the "
        "warning was pure noise."
    )
    assert "function Get-AutoStartCommand" in text


def test_shell_installer_creates_start_menu_shortcuts_for_start_and_stop():
    text = (
        REPO_ROOT / "installer" / "windows-native" / "shell" / "install.ps1"
    ).read_text(encoding="utf-8")

    assert "function Install-StartMenuShortcuts" in text
    assert "WScript.Shell" in text
    assert "Microsoft\\Windows\\Start Menu\\Programs\\Media Search Agent" in text
    assert "Media Search Agent.lnk" in text
    assert "Stop Media Search Agent.lnk" in text
    assert "start.ps1" in text
    assert "stop.ps1" in text


def test_shell_installer_copies_bundled_tools_into_launcher_bin():
    text = (
        REPO_ROOT / "installer" / "windows-native" / "shell" / "install.ps1"
    ).read_text(encoding="utf-8")

    assert 'foreach ($tool in @("exiftool.exe")) {' in text
    assert 'Copy-Item (Join-Path $bundleDir "bin\\$tool") (Join-Path $LauncherDir $tool) -Force' in text
    assert 'Write-Ok "Bundled tools installed to $LauncherDir"' in text


def test_windows_tray_menu_opens_on_both_left_and_right_click():
    """New users who discover the tray icon in the overflow row naturally
    left-click it. The Windows default of 'left-click does nothing,
    right-click shows menu' is confusing - both buttons must surface the
    same context menu so the icon is discoverable without prior knowledge."""
    text = TRAY_PROGRAM.read_text(encoding="utf-8")

    # Both button checks must be present in the MouseUp guard; whichever
    # order they're written, the OR-form is what enables left-click.
    has_both_buttons = (
        "MouseButtons.Left" in text and "MouseButtons.Right" in text
    )
    assert has_both_buttons, (
        "Tray MouseUp handler must check both MouseButtons.Left and "
        "MouseButtons.Right to make the menu reachable via left-click."
    )

    # The handler must still drive the menu manually (the
    # SetForegroundWindow trick fixes the RDP/mstsc dismiss-on-first-frame
    # bug; removing it would break menu display under remote sessions).
    assert "SetForegroundWindow(menu.Handle)" in text
    assert "menu.Show(Cursor.Position)" in text


def test_windows_tray_first_launch_balloon_stays_at_least_30_seconds():
    """The 4-second balloon was too short for a new user to notice the
    overflow-hidden tray icon and act on it. 30s is the upper bound -
    Windows dismisses the balloon the moment the user clicks anywhere
    else, so there's no risk of overstaying."""
    text = TRAY_PROGRAM.read_text(encoding="utf-8")

    # Constant must be 30s, and the ShowBalloon helper must use it.
    assert "BalloonDurationMs = 30_000" in text, (
        "BalloonDurationMs constant must be 30_000 (ms)."
    )
    assert "_tray.ShowBalloonTip(BalloonDurationMs)" in text, (
        "ShowBalloon helper must use the BalloonDurationMs constant so all "
        "three first-launch states (starting / ready / failed) share the "
        "same dismissal window."
    )
    # Regression guard - the old 4-second value must not creep back in.
    assert "_tray.ShowBalloonTip(4_000)" not in text


def test_windows_tray_balloon_reflects_three_first_launch_states():
    """Static 'Starting up...' text was stale after the API actually came
    up (~5-10s later) and gave no signal at all when the API failed to
    respond within ReadyTimeoutSec. Wire three balloon updates into the
    first-launch sequence so the balloon is the live status surface:

      1. starting -> 'Starting up...'                    (Info icon)
      2. ready    -> 'Ready. Opening your browser...'    (Info icon)
      3. failed   -> 'Did not start within Ns. ...'      (Warning icon)
    """
    text = TRAY_PROGRAM.read_text(encoding="utf-8")

    # WaitForReadyAsync must return bool so the launch sequence can branch.
    assert "private async Task<bool> WaitForReadyAsync()" in text, (
        "WaitForReadyAsync must return Task<bool> so callers can tell "
        "ready-within-timeout apart from timed-out."
    )
    # Return value must be honoured by LaunchSequenceAsync.
    assert "bool ready = await WaitForReadyAsync();" in text

    # Three balloon strings, in order.
    assert '"Starting up' in text
    assert '"Ready. Opening your browser' in text
    assert "Did not start within" in text

    # Failure must use the Warning icon (not Info) so the visual cue
    # matches the message severity.
    assert "ToolTipIcon.Warning" in text, (
        "The startup-failure balloon must use ToolTipIcon.Warning so the "
        "icon visually distinguishes failure from the Info-coloured "
        "starting and ready balloons."
    )

    # Failure message must include the menu-path remediation so the user
    # can find the View Logs entry without prior knowledge.
    assert "More → View Logs" in text, (
        "Failure balloon must guide the user to More -> View Logs."
    )

    # OnOpenBrowser (menu action) must also surface the failure balloon -
    # otherwise clicking 'Open Media Search' when the API is dead is silent.
    open_idx = text.index("private async void OnOpenBrowser")
    next_method_idx = text.index("\n    private", open_idx + 1)
    open_body = text[open_idx:next_method_idx]
    assert "bool ready" in open_body and "ShowBalloon" in open_body, (
        "OnOpenBrowser must branch on WaitForReadyAsync's bool result and "
        "ShowBalloon a failure message when the API doesn't come up."
    )


def test_windows_tray_cli_opens_in_user_data_folder():
    """The tray's 'Open CLI' menu must open cmd.exe in the user data folder
    (where config.yaml lives), not the install's bin/ directory."""
    text = TRAY_PROGRAM.read_text(encoding="utf-8")

    # OnOpenCmd builds a ProcessStartInfo for cmd.exe ...
    assert "private void OnOpenCmd()" in text
    assert 'new ProcessStartInfo("cmd.exe")' in text
    # ... explicitly set WorkingDirectory to the user data folder ...
    assert "WorkingDirectory = _paths.DataDir" in text
    # ... and create the directory first so the launch cannot fail.
    assert "Directory.CreateDirectory(_paths.DataDir)" in text


def test_windows_native_start_script_shows_startup_window():
    text = _start_text()

    assert "function Show-StartupWindow" in text
    assert "function Update-StartupWindow" in text
    assert 'Text = "Starting Media Search Agent..."' in text
    assert 'Text = "Open Logs"' in text
    assert "This app runs locally on your computer." in text
    assert 'MessageBoxIcon]::Error' in text


def test_shell_installer_banner_communicates_per_user_install_scope():
    """The installer is per-user (everything under %LOCALAPPDATA%) but that
    isn't obvious from the one-liner. Multi-user machines silently get a
    private install per Windows account (~3 GB each). The banner must
    surface this so the user isn't surprised."""
    text = (
        REPO_ROOT / "installer" / "windows-native" / "shell" / "install.ps1"
    ).read_text(encoding="utf-8")

    assert "current user only" in text
    assert "other Windows accounts" in text


def test_shell_installer_runs_system_requirement_pre_flight():
    """Pre-flight system checks must catch the common "this won't work on
    your machine" cases (old Windows, full disk, low RAM) before any
    network IO. OS version is fatal below Win10; disk space is fatal
    below 5 GB; RAM is warning-only since small libraries work on less."""
    text = (
        REPO_ROOT / "installer" / "windows-native" / "shell" / "install.ps1"
    ).read_text(encoding="utf-8")

    assert "function Test-SystemRequirements" in text

    # OS version floor: Win10 RTM (10240) hard, 17763 (1809) recommended.
    assert "[Environment]::OSVersion.Version" in text
    assert "Windows 10 or newer required" in text
    assert "17763" in text
    assert "1809+ recommended" in text

    # Disk space: 5 GB floor, fatal.
    assert "Get-PSDrive" in text
    assert "5 GB free" in text or "5 GB" in text

    # RAM: warning only via Win32_ComputerSystem.
    assert "Win32_ComputerSystem" in text
    assert "TotalPhysicalMemory" in text
    assert "8+ GB recommended" in text

    # Must run BEFORE Resolve-MsaVersion so we don't hit the network if
    # the machine fails the check.
    sysreq_idx = text.index("Test-SystemRequirements")
    resolve_idx = text.index("Resolve-MsaVersion", sysreq_idx)
    assert sysreq_idx < resolve_idx, (
        "System requirements check must run before Resolve-MsaVersion."
    )


def test_shell_installer_fails_fast_on_unsupported_windows_architecture():
    """ARM64 Windows machines would fail deep inside `pip install torch` with
    a confusing wheel-resolution error. The installer must detect arch
    early (after the banner, before downloading the bundle) and fail with
    a clear message. This brings Windows to parity with install.sh which
    already rejects Intel Mac."""
    text = (
        REPO_ROOT / "installer" / "windows-native" / "shell" / "install.ps1"
    ).read_text(encoding="utf-8")

    assert "function Test-WindowsArchitecture" in text
    assert "[Environment]::Is64BitOperatingSystem" in text
    # Prefer OS arch over process arch so a 32-bit PowerShell host on
    # 64-bit Windows (rare but real - some legacy admin shells, RDP from
    # x86 clients) doesn't get falsely rejected. PROCESSOR_ARCHITECTURE
    # reflects the running process; PROCESSOR_ARCHITEW6432 is set in
    # 32-bit processes on 64-bit OS and reports the OS arch.
    assert "$env:PROCESSOR_ARCHITEW6432" in text, (
        "Test-WindowsArchitecture must check PROCESSOR_ARCHITEW6432 to "
        "avoid false-positive rejection on a 32-bit PowerShell host."
    )
    assert "$env:PROCESSOR_ARCHITECTURE" in text  # fallback path still present
    assert "AMD64|x86_64" in text
    assert "ARM64 Windows is not yet supported" in text

    # Must run AFTER Write-Banner (so user sees what's being installed)
    # but BEFORE Resolve-MsaVersion (so we don't hit the network on a
    # machine we can't install on anyway).
    banner_idx = text.index("Write-Banner")
    arch_call_idx = text.index("Test-WindowsArchitecture", banner_idx)
    resolve_idx = text.index("Resolve-MsaVersion", arch_call_idx)
    assert banner_idx < arch_call_idx < resolve_idx, (
        "Architecture check must run between Write-Banner and Resolve-MsaVersion."
    )


def test_shell_installer_verifies_bundle_sha256_against_published_sums():
    """The unsigned installer fetches a .zip over HTTPS from GitHub Releases.
    SHA256SUMS.txt is published alongside; the installer must verify the
    downloaded bundle against it before Expand-Archive. A mismatched bundle
    must die *before* touching any install directory."""
    text = (
        REPO_ROOT / "installer" / "windows-native" / "shell" / "install.ps1"
    ).read_text(encoding="utf-8")

    # Helper exists and is called from the download path.
    assert "function Test-BundleSha256" in text
    assert "Test-BundleSha256 $bundleZip" in text

    # Fetched from the release-relative URL, not a hardcoded one.
    assert "SHA256SUMS.txt" in text

    # Uses Get-FileHash (built into PowerShell 5.1+; no extra deps).
    assert "Get-FileHash $BundleFile -Algorithm SHA256" in text

    # Mismatch is fatal and runs BEFORE Expand-Archive (so a tampered
    # bundle never touches the install dir).
    assert "Bundle SHA256 mismatch" in text
    sha_idx = text.index("Test-BundleSha256 $bundleZip")
    extract_idx = text.index("Expand-Archive -Path $bundleZip")
    assert sha_idx < extract_idx, (
        "SHA256 check must run before Expand-Archive so a tampered bundle "
        "never reaches the extract step."
    )

    # Missing SHA256SUMS.txt (HTTP 404) is a warning, not a hard fail -
    # older releases predate this file and must still install. But this
    # fallback path must be gated on the explicit 404 status code; other
    # fetch failures (transient TLS / proxy / 5xx / connection reset)
    # must hard-fail because the alternative is silently extracting an
    # unverified bundle - exactly the supply-chain guard bypass this
    # whole function exists to prevent. Caught in PR #132 review (Codex).
    assert "skipping integrity check" in text
    assert "HTTP 404" in text, (
        "Test-BundleSha256 must distinguish HTTP 404 (the legacy-release "
        "fallback) from other fetch failures. The previous catch-everything "
        "form let transient TLS/proxy/5xx failures silently bypass "
        "verification."
    )
    # The status-code branch must Write-Fail on non-404 errors.
    sha_func_idx = text.index("function Test-BundleSha256")
    next_func_idx = text.index("\nfunction ", sha_func_idx + 1)
    sha_body = text[sha_func_idx:next_func_idx]
    assert "Refusing to install an unverified bundle" in sha_body, (
        "Non-404 SHA256SUMS fetch failures must Write-Fail with a clear "
        "remediation, not warn-and-proceed."
    )
    assert "$_.Exception.Response.StatusCode" in sha_body, (
        "Status-code-based 404 detection must use Exception.Response.StatusCode "
        "to distinguish legacy releases from transport failures."
    )

    # Local -Bundle path skips verification (caller is trusted).
    bundle_branch_idx = text.index("Using local bundle:")
    next_else_idx = text.index("} else {", bundle_branch_idx)
    local_branch = text[bundle_branch_idx:next_else_idx]
    assert "Test-BundleSha256" not in local_branch, (
        "Local -Bundle path must skip Test-BundleSha256."
    )


def test_shell_installer_refuses_version_downgrade_by_default():
    """A user re-running the one-liner with -Version v0.2.0 after upgrading
    to v0.3.0 must NOT silently downgrade - downgrades can corrupt
    index/media.sqlite if the schema moved forward between versions. The
    guard must be opt-out via -AllowDowngrade, not opt-in."""
    text = (
        REPO_ROOT / "installer" / "windows-native" / "shell" / "install.ps1"
    ).read_text(encoding="utf-8")

    # CLI plumbing
    assert "[switch] $AllowDowngrade," in text
    assert "-AllowDowngrade" in text  # appears in help text too

    # Version-compare helper strips `v` prefix and `-prerelease` suffix so
    # 0.7.3-test6 compares equal to 0.7.3.
    assert "function ConvertTo-MsaVersionObject" in text
    assert "$bare = $tag -replace '^v', ''" in text
    assert "$numeric = ($bare -split '-')[0]" in text

    # Guard reads $VersionFile and refuses to install when the new version
    # is lower than the recorded one.
    assert "function Test-VersionDowngrade" in text
    assert "$VersionFile = \"$AppDir\\version.txt\"" in text
    assert "Refusing to downgrade" in text
    assert "$AllowDowngrade" in text

    # Legacy installs (no version file) must NOT trip the guard - missing
    # file is a silent no-op, not an error.
    assert "if (-not (Test-Path $VersionFile)) {" in text

    # Local-bundle installs skip the check since the version is unknown.
    assert '$NewTag -eq "(local bundle)"' in text

    # Marker is written only after Install-TaskScheduler succeeds - if
    # anything above failed we must NOT advance the recorded version.
    assert "Install-TaskScheduler" in text
    assert "Set-Content -Path $VersionFile" in text


def test_shell_installer_runs_downgrade_guard_for_upgrade_and_repair_modes():
    """Regression caught in PR #132 review: `Test-VersionDowngrade` was only
    called when `$installMode -eq "upgrade"` (3+ markers). Repair-mode
    installs (1-2 markers) skipped the check, so a user with a partial but
    real prior install (existing `version.txt` + index data) could
    unintentionally downgrade and hit the same SQLite schema corruption
    the guard exists to prevent. The check must fire whenever an existing
    install is detected, regardless of marker completeness; the check
    itself is a silent no-op when `version.txt` is absent so fresh
    installs aren't affected."""
    text = (
        REPO_ROOT / "installer" / "windows-native" / "shell" / "install.ps1"
    ).read_text(encoding="utf-8")

    # Must include both modes in the call-site guard.
    assert '$installMode -in @("upgrade", "repair")' in text, (
        "Test-VersionDowngrade must be invoked when $installMode is "
        "either 'upgrade' or 'repair' - the previous 'upgrade'-only "
        "guard let partial-state installs bypass the safety check."
    )
    # Must NOT be the old upgrade-only form.
    assert 'if ($installMode -eq "upgrade") {' not in text, (
        "The old 'upgrade'-only guard call site must be replaced with "
        "the upgrade+repair form."
    )


def test_shell_installer_announces_same_version_reinstall():
    """A rerun of the one-liner against an already-installed version isn't
    a no-op - Install-Bundle wipes $RepoDir and re-extracts, pip install
    re-runs, launcher/tray/scheduled task all get re-registered. Surface
    that explicitly so the user can see the rerun did something. Before
    this guard, same-version reruns produced no version-related message
    between the downgrade-refuse and upgrade-announce branches, which left
    users unsure whether the rerun changed anything (the most common
    'how do I repair a broken install?' path is rerun-the-one-liner)."""
    text = (
        REPO_ROOT / "installer" / "windows-native" / "shell" / "install.ps1"
    ).read_text(encoding="utf-8")

    # Same-version branch must Write-Info a clear reinstall message and
    # call out that user data is preserved.
    assert "Reinstalling $NewTag" in text, (
        "Test-VersionDowngrade must emit a 'Reinstalling vX.Y.Z' line on "
        "the same-version branch so reruns of the one-liner aren't silent."
    )
    assert "user data preserved" in text, (
        "Same-version notice must reassure the user their config / index / "
        "model cache / logs are NOT touched by the rerun."
    )

    # Live in the same Test-VersionDowngrade function as the upgrade /
    # downgrade branches, structured as an `else` after `elseif (... -gt)`.
    dg_idx = text.index("function Test-VersionDowngrade")
    next_func_idx = text.index("\nfunction ", dg_idx + 1)
    body = text[dg_idx:next_func_idx]
    assert "Upgrading from" in body
    assert "Refusing to downgrade" in body
    assert "Reinstalling" in body, (
        "Same-version branch must live inside Test-VersionDowngrade next to "
        "the upgrade/downgrade branches so all three version-relation "
        "outcomes are visible in one place."
    )


def test_shell_installer_selects_torch_wheel_based_on_nvidia_presence():
    """Installing the CUDA-enabled torch wheel on a no-NVIDIA Windows machine
    crashes subprocess torch imports at the Windows loader with
    STATUS_DLL_INIT_FAILED (0xC0000142) - no Python traceback, just an opaque
    Application Error dialog. Install-Torch must detect NVIDIA hardware via
    WMI and choose the CUDA index URL only when an NVIDIA GPU is actually
    present; otherwise install the default CPU-only wheel from PyPI."""
    text = (
        REPO_ROOT / "installer" / "windows-native" / "shell" / "install.ps1"
    ).read_text(encoding="utf-8")

    # Hardware presence detection via WMI - not nvidia-smi, which needs the
    # driver loaded and PATH configured.
    assert "function Test-NvidiaPresent" in text
    assert "Get-CimInstance Win32_VideoController" in text
    # Filter out RDP virtual adapters and the Microsoft Basic Display Adapter
    # that Windows substitutes when no real driver is loaded.
    assert "'Virtual|Remote|Basic'" in text

    # Install-Torch must call Test-NvidiaPresent and branch on the result.
    assert "$hasNvidia = Test-NvidiaPresent" in text

    # CUDA path: uses the cu128 index URL (Blackwell-compatible).
    assert "--index-url $TorchIndexUrl" in text
    assert "PyTorch (CUDA) installed" in text

    # CPU path: omits --index-url so pip resolves to the default PyPI wheel,
    # which is CPU-only on Windows. This is the path that prevents the
    # STATUS_DLL_INIT_FAILED crash on no-NVIDIA machines.
    assert "No NVIDIA GPU detected" in text
    assert "PyTorch (CPU) installed" in text

    # WMI failure must default to CPU (the safe choice that installs
    # everywhere).
    assert "defaulting to CPU-only torch wheels" in text

    # The loader-failure reference is in the function-level comment so future
    # readers know why this branching exists.
    assert "STATUS_DLL_INIT_FAILED" in text


def test_shell_installer_prints_full_url_for_every_network_download():
    """Trust signal: when the installer reaches out over the network, the
    user must be able to read the exact URL it's hitting - same idea as
    surfacing model-download sources on the first-launch SetupPage. The
    three current network call sites are:

      1. Resolve-MsaVersion -> api.github.com/repos/.../releases/latest
      2. Test-BundleSha256  -> <release>/SHA256SUMS.txt
      3. Get-FileFromUrl    -> the bundle .zip

    Each must Write-Info the URL before the actual call, so the install
    log is auditable and a user watching the console can confirm the
    bytes are coming from github.com rather than a typo'd host."""
    text = (
        REPO_ROOT / "installer" / "windows-native" / "shell" / "install.ps1"
    ).read_text(encoding="utf-8")

    def _function_body(name: str) -> str:
        idx = text.index(f"function {name}")
        nxt = text.index("\nfunction ", idx + 1)
        return text[idx:nxt]

    # 1. Get-FileFromUrl - generic downloader used by the bundle pull.
    dl_body = _function_body("Get-FileFromUrl")
    assert 'Write-Info "  from $url"' in dl_body, (
        "Get-FileFromUrl must Write-Info the URL on its own line before "
        "Invoke-WebRequest. This is what surfaces the bundle host."
    )

    # 2. Test-BundleSha256 - downloads SHA256SUMS.txt for integrity check.
    sha_body = _function_body("Test-BundleSha256")
    assert 'Write-Info "  from $sumsUrl"' in sha_body, (
        "Test-BundleSha256 must Write-Info the SHA256SUMS.txt URL so the "
        "user sees where the integrity reference is coming from."
    )

    # 3. Resolve-MsaVersion - hits api.github.com.
    ver_body = _function_body("Resolve-MsaVersion")
    assert 'Write-Info "  from $releasesUrl"' in ver_body, (
        "Resolve-MsaVersion must Write-Info the GitHub API URL it queries "
        "for the latest release."
    )

    # The 'from <url>' pattern is the same shape in all three so future
    # readers can grep for it; lock that uniformity.
    from_count = text.count('Write-Info "  from $')
    assert from_count >= 3, (
        f"Expected >= 3 'Write-Info \"  from $...\"' lines (one per "
        f"network call site); found {from_count}. New network calls "
        "should follow the same pattern."
    )


def test_shell_installer_normalises_appdir_trailing_backslash():
    """Codex P2 on PR #132 (post-merge): `-AppDir C:\\MSA\\` produced a
    pattern `C:\\MSA\\\\*` in subsequent `-like "$AppDir\\*"` ownership
    checks, which never matches real process paths. The installer would
    then treat its own API process as outside AppDir and skip
    termination - reopening the half-deleted-venv corruption class the
    Stop-RunningServices guard was added to close.

    Fix: TrimEnd('\\') on $AppDir and $DataDir right after the param
    block. Symmetric fix in uninstall.ps1."""
    install_text = (
        REPO_ROOT / "installer" / "windows-native" / "shell" / "install.ps1"
    ).read_text(encoding="utf-8")
    uninstall_text = (
        REPO_ROOT / "installer" / "windows-native" / "shell" / "uninstall.ps1"
    ).read_text(encoding="utf-8")

    for text, fname in [(install_text, "install.ps1"), (uninstall_text, "uninstall.ps1")]:
        assert "$AppDir = $AppDir.TrimEnd('\\')" in text, (
            f"{fname} must strip a trailing backslash from $AppDir so "
            "`-like \"$AppDir\\*\"` ownership checks work whether the "
            "caller passed `-AppDir C:\\MSA` or `-AppDir C:\\MSA\\`."
        )
        assert "$DataDir = $DataDir.TrimEnd('\\')" in text, (
            f"{fname} must also strip a trailing backslash from $DataDir "
            "for symmetric prefix-check safety."
        )

    # The trim must run BEFORE the actual usage. Anchor on the specific
    # ownership-check expression from Stop-RunningServices so the test
    # doesn't accidentally match the explanatory comment in the trim
    # block itself (which mentions `-like "$AppDir\*"` for readers).
    appdir_trim_idx = install_text.index("$AppDir = $AppDir.TrimEnd('\\')")
    ownership_check_idx = install_text.index(
        '($procPath -and ($procPath -like "$AppDir\\*"))'
    )
    assert appdir_trim_idx < ownership_check_idx, (
        "install.ps1 must trim $AppDir BEFORE the ownership-check "
        "`-like \"$AppDir\\*\"` in Stop-RunningServices; trimming after "
        "wouldn't fix the spurious-mismatch bug."
    )


def test_shell_installer_waits_for_api_ready_after_tray_launch():
    """End of install is now two stages instead of one static block:

      1. '+ Media Search Agent installed!' (install steps complete)
      2. 'Starting the app........'        (live dots while polling /health)
      3. '+ Media Search Agent started!'   (when /health returns 200)

    Bridges the silent gap users used to see between install completing
    and the browser opening - the tray launches the API in the background
    and opens the browser only when /health responds, which can be 10-30 s
    on a fresh first launch. Without the poll, the installer would exit
    and leave a returned shell prompt with no indication of what was
    happening.

    Pins the no-double-tab contract: `Wait-ApiReady` must POLL /health,
    never `Start-Process` the URL itself. The tray is the sole browser
    opener; calling Start-Process on the URL here too would produce a
    duplicate tab."""
    text = (
        REPO_ROOT / "installer" / "windows-native" / "shell" / "install.ps1"
    ).read_text(encoding="utf-8")

    # The helper exists and is invoked from the main flow.
    assert "function Wait-ApiReady" in text
    assert "Wait-ApiReady -TimeoutSec 90" in text or "Wait-ApiReady -TimeoutSec" in text
    # And it must use the configured port (not a hardcoded 8000).
    assert "Get-ConfiguredApiPort" in text, (
        "Wait-ApiReady must be called with `(Get-ConfiguredApiPort)` so "
        "users who customised the API port in config.yaml still get a "
        "correct readiness check."
    )

    # Stage 2 live-progress + stage 3 success strings.
    assert "Starting the app" in text
    assert "Media Search Agent started!" in text

    # No-double-tab contract: Wait-ApiReady body must NOT Start-Process
    # the URL or `start` it. Polling only.
    fn_idx = text.index("function Wait-ApiReady")
    next_fn_idx = text.index("\nfunction ", fn_idx + 1)
    fn_body = text[fn_idx:next_fn_idx]
    assert "/health" in fn_body, "Wait-ApiReady must poll the /health endpoint"
    assert "Start-Process" not in fn_body, (
        "Wait-ApiReady must NOT Start-Process the URL - that's the tray's "
        "job. Double-opening would produce two tabs."
    )

    # 2s post-success pause so the tray's parallel /health poll has time
    # to fire and open the browser tab BEFORE the installer exits and
    # the shell prompt returns. Without it the user can see a brief
    # confusing silence between the '+ started!' line and the tab.
    assert "Start-Sleep -Seconds 2" in fn_body, (
        "Wait-ApiReady must Start-Sleep -Seconds 2 after a successful "
        "health check so the tray's parallel poll has time to open the "
        "browser before the installer exits."
    )

    # Probe 127.0.0.1, not localhost. PS 5.1's Invoke-WebRequest resolves
    # `localhost` to ::1 (IPv6) first and waits for IPv6 to time out
    # before retrying IPv4; with the per-attempt timeout the poll then
    # never detects a healthily-serving API on 127.0.0.1. Real-VM symptom
    # the user reported on PR #133: tray + browser come up, installer's
    # "Starting the app........" prints dots forever.
    assert 'http://127.0.0.1:$Port/health' in fn_body, (
        "Wait-ApiReady must probe http://127.0.0.1:$Port/health, not "
        "http://localhost:$Port/health. PS 5.1 resolves `localhost` to "
        "::1 first and times out before retrying IPv4, so the poll "
        "never detects a healthy API. Use 127.0.0.1 explicitly."
    )
    # And the timeout must be at least 2s; 1s was empirically too tight
    # to absorb PS 5.1's per-request overhead during port binding.
    import re
    m = re.search(r"-TimeoutSec\s+(\d+)\b", fn_body)
    assert m and int(m.group(1)) >= 2, (
        "Wait-ApiReady's -TimeoutSec must be >= 2s (was 1s, which was "
        "too tight on Windows where Invoke-WebRequest has noticeable "
        "per-request overhead during port binding)."
    )

    # Wall-clock timeout (Codex P2 on PR #133): the timeout MUST be
    # measured against a [DateTime]::UtcNow start time, not by counting
    # `for` loop iterations. Each iteration costs up to -TimeoutSec
    # (Invoke-WebRequest) plus 1 s sleep = ~4 s, so an iteration-counted
    # TimeoutSec of 90 could spend ~360 s on a failure path and the
    # warning saying "didn't respond within ${TimeoutSec}s" would be
    # wildly misleading.
    assert "[DateTime]::UtcNow" in fn_body, (
        "Wait-ApiReady must use [DateTime]::UtcNow to track wall-clock "
        "elapsed time, not loop iterations. Iteration-counted timeouts "
        "can take 3-4x longer than the printed value because each "
        "iteration blocks on Invoke-WebRequest + Start-Sleep."
    )
    # And the for-loop iteration form must be gone so the test catches
    # a regression to the wrong shape.
    assert "for ($i = 0; $i -lt $TimeoutSec; $i++)" not in fn_body, (
        "Wait-ApiReady must not iterate by `for ($i = 0; $i -lt "
        "$TimeoutSec; $i++)`. Use a `while` on wall-clock elapsed time."
    )

    # Get-ConfiguredApiPort: shared helper used by Stop-RunningServices
    # AND Wait-ApiReady. Inline duplicate config-port parsing must not
    # come back.
    assert "function Get-ConfiguredApiPort" in text


def test_shell_installer_appdir_trim_preserves_drive_roots():
    """Copilot on PR #133: `$AppDir.TrimEnd('\\')` unconditionally turns
    a drive root like `C:\\` into `C:`. Subsequent `-like \"$AppDir\\*\"`
    then becomes `-like \"C:\\*\"` which spuriously matches anything on
    the drive, making the orphan-process ownership check overly broad.

    Fix: only trim when the path is more than just a drive root.
    Symmetric guard in install.ps1 and uninstall.ps1."""
    install_text = (
        REPO_ROOT / "installer" / "windows-native" / "shell" / "install.ps1"
    ).read_text(encoding="utf-8")
    uninstall_text = (
        REPO_ROOT / "installer" / "windows-native" / "shell" / "uninstall.ps1"
    ).read_text(encoding="utf-8")

    for text, fname in [(install_text, "install.ps1"), (uninstall_text, "uninstall.ps1")]:
        # The guard regex must be present immediately before the trim.
        # The conditional uses -notmatch with a drive-root regex.
        guarded_form = "if ($AppDir -notmatch '^[A-Za-z]:\\\\$') { $AppDir = $AppDir.TrimEnd('\\') }"
        assert guarded_form in text, (
            f"{fname} must guard the $AppDir trim against drive-root "
            "inputs: `if ($AppDir -notmatch '^[A-Za-z]:\\\\$') { ... }`. "
            "Without the guard `C:\\` becomes `C:` and subsequent "
            "`-like \"$AppDir\\*\"` matches the whole drive."
        )
        guarded_data = "if ($DataDir -notmatch '^[A-Za-z]:\\\\$') { $DataDir = $DataDir.TrimEnd('\\') }"
        assert guarded_data in text, (
            f"{fname} must guard the $DataDir trim with the same "
            "drive-root regex check."
        )
        # The unguarded form (which the previous commit had) must NOT
        # appear bare anywhere in the file - it would defeat the guard.
        # Allow the conditional form to contain it as the inner expression.
        bare_trim_lines = [
            line for line in text.splitlines()
            if line.strip().startswith("$AppDir = $AppDir.TrimEnd")
            or line.strip().startswith("$DataDir = $DataDir.TrimEnd")
        ]
        assert not bare_trim_lines, (
            f"{fname} must not have a bare line starting with "
            "`$AppDir = $AppDir.TrimEnd(...)` or `$DataDir = ...`; "
            "the trim must always be inside the `if (-notmatch ...) { ... }` "
            "drive-root guard. Found bare lines: " + str(bare_trim_lines)
        )


def test_shell_installer_prints_banner_before_log_and_mode_chatter():
    """Regression: Initialize-Logging used to Write-Info the log path,
    mode, markers, and reason BEFORE Write-Banner ran. The user's first
    visible line was the log path, not 'Media Search Agent Installer'.

    Contract: Initialize-Logging only starts the transcript; the visible
    Log/Mode/Markers/Reason lines must be printed by Write-Banner, after
    the banner header."""
    text = (
        REPO_ROOT / "installer" / "windows-native" / "shell" / "install.ps1"
    ).read_text(encoding="utf-8")

    # Initialize-Logging body must not emit the user-visible info lines —
    # those moved into Write-Banner. Grab the function body between
    # `function Initialize-Logging` and the next top-level `function`.
    init_idx = text.index("function Initialize-Logging")
    next_func_idx = text.index("\nfunction ", init_idx + 1)
    init_body = text[init_idx:next_func_idx]
    for forbidden in ("Write-Info \"Log:", "Write-Info \"Mode:",
                      "Write-Info \"Markers:", "Write-Info \"Reason:"):
        assert forbidden not in init_body, (
            f"{forbidden!r} must not be printed inside Initialize-Logging — "
            "it moves the banner below implementation chatter. Move into "
            "Write-Banner so the banner is the first visible line."
        )

    # Write-Banner must contain the banner header AND the log/mode block.
    banner_idx = text.index("function Write-Banner")
    next_after_banner = text.index("\nfunction ", banner_idx + 1)
    banner_body = text[banner_idx:next_after_banner]
    assert "Media Search Agent Installer" in banner_body
    for required in ("Log:", "Mode:", "Markers:", "Reason:"):
        assert required in banner_body, (
            f"Write-Banner must surface {required!r} after the banner header "
            "so the visible Log/Mode/Markers/Reason lines stay together "
            "instead of preceding the title."
        )

    # And the banner header must appear BEFORE the log block within
    # Write-Banner itself (header line above any of the four Write-Info lines).
    header_pos = banner_body.index("Media Search Agent Installer")
    log_pos = banner_body.index("Log:")
    assert header_pos < log_pos, (
        "Write-Banner: the banner header line must precede the Log: line."
    )


def test_shell_installer_stop_running_services_kills_orphan_python_by_name():
    """Regression: a prior failed uninstall can leave a python process holding
    port 8000 whose $proc.Path returns null/empty because the venv was
    half-deleted around it. The old install.ps1 used a path-only check
    (`$procPath -like "$AppDir\\*"`) and skipped killing such orphans,
    producing the "PID held outside AppDir - skipping" deadlock seen on
    VM smoke tests.

    Stop-RunningServices must kill if the process name matches python /
    pythonw / uvicorn even when the path is unresolvable, AND verify the
    kill actually completed via WaitForExit before continuing - otherwise
    we half-delete the venv around a still-running process."""
    text = (
        REPO_ROOT / "installer" / "windows-native" / "shell" / "install.ps1"
    ).read_text(encoding="utf-8")

    assert "function Stop-RunningServices" in text

    # Process-name fallback must ONLY apply when $procPath is empty (i.e.
    # the venv was half-deleted under a still-running orphan). If we also
    # kill by name when the path is resolvable, an unrelated dev python
    # on port 8000 (Django, FastAPI dev, Jupyter on alternate port, etc.)
    # would be terminated by reinstall.
    assert "'^(python|pythonw|uvicorn)$'" in text
    # Strong signal: path under $AppDir.
    assert '($procPath -and ($procPath -like "$AppDir\\*"))' in text, (
        "Kill condition must include the path-under-AppDir positive check."
    )
    # Name fallback must be guarded by `(-not $procPath)` so it only fires
    # when path resolution failed.
    assert "(-not $procPath) -and ($proc.ProcessName -match" in text, (
        "Kill-by-name must be guarded by (-not $procPath) so it can't "
        "terminate unrelated dev python processes on the API port."
    )

    # Stop-Process must use -ErrorAction Stop so kill failures surface
    # instead of being silently swallowed by SilentlyContinue.
    stop_running_idx = text.index("function Stop-RunningServices")
    next_func_idx = text.index("\nfunction ", stop_running_idx + 1)
    section = text[stop_running_idx:next_func_idx]
    assert "Stop-Process -Id $proc.Id -Force -ErrorAction Stop" in section, (
        "install.ps1 Stop-RunningServices must use -ErrorAction Stop so "
        "kill failures surface (the previous -ErrorAction SilentlyContinue "
        "swallowed Access Denied and other failures, then we deleted the "
        "venv around a still-running python)."
    )

    # WaitForExit gates the success path - without it Stop-Process can return
    # cleanly while the process is still alive.
    assert "$proc.WaitForExit(5000)" in section
    # Write-Fail with a taskkill remediation string lets the user recover
    # without guessing. Must surface the PID.
    assert "taskkill /F /PID" in section
    assert "Write-Fail" in section


def test_shell_installer_kill_verify_is_wrapped_in_nested_fatal_catch():
    """Regression caught in PR #132 review: the outer try/catch around the
    port-listener kill block was broad enough to swallow exceptions from
    `Stop-Process` and `$proc.WaitForExit(5000)` themselves, not just
    `Get-NetTCPConnection` quirks. If `WaitForExit` threw (e.g. process
    handle / access oddity), the outer catch downgraded it to a
    `Write-Warn` and the installer proceeded into destructive steps -
    re-introducing the half-deleted-venv mode this guard exists to
    prevent.

    The fix: wrap the kill + verify block in its own inner try/catch that
    catches any unexpected exception and calls `Write-Fail` (terminator,
    bubbles past the outer catch via exit 1). The outer catch keeps its
    role for environmental setup oddities (Get-NetTCPConnection / Get-Process)
    where Write-Warn is the right response."""
    text = (
        REPO_ROOT / "installer" / "windows-native" / "shell" / "install.ps1"
    ).read_text(encoding="utf-8")

    stop_running_idx = text.index("function Stop-RunningServices")
    next_func_idx = text.index("\nfunction ", stop_running_idx + 1)
    section = text[stop_running_idx:next_func_idx]

    # The kill+verify block must have a dedicated catch that calls
    # Write-Fail (not Write-Warn) on unexpected verification failure.
    # Look for the specific failure message identifying the path.
    assert "Process-stop verification failed" in section, (
        "install.ps1 Stop-RunningServices must wrap the kill + WaitForExit "
        "block in a nested try/catch whose catch handler Write-Fail's with "
        "'Process-stop verification failed' on unexpected exceptions, so "
        "WaitForExit throws can't be downgraded to a Write-Warn by the "
        "outer environmental-error catch."
    )
    # And Write-Fail must be used in that handler (Write-Warn would defeat the purpose).
    verification_failed_idx = section.index("Process-stop verification failed")
    # Look backwards from the failure message for the nearest Write call.
    nearby = section[max(0, verification_failed_idx - 50):verification_failed_idx + 80]
    assert "Write-Fail" in nearby, (
        "The 'Process-stop verification failed' branch must use Write-Fail "
        "(terminator, exits 1) not Write-Warn (continues into destructive steps)."
    )


def test_shell_installer_stop_section_is_protected_from_inner_catch_swallowing_exit():
    """The outer try/catch around Stop-RunningServices port-kill block must
    only Write-Warn on environmental errors (Get-NetTCPConnection oddities,
    etc.). PowerShell terminators (exit from Write-Fail) bubble through
    catch blocks, so the comment must make that contract explicit - future
    edits that swap exit for throw would silently re-introduce the
    half-deleted-venv bug."""
    text = (
        REPO_ROOT / "installer" / "windows-native" / "shell" / "install.ps1"
    ).read_text(encoding="utf-8")

    stop_running_idx = text.index("function Stop-RunningServices")
    next_func_idx = text.index("\nfunction ", stop_running_idx + 1)
    section = text[stop_running_idx:next_func_idx]
    # Contract reminder for future editors.
    assert "terminators are not caught" in section.lower() or \
           "exit 1" in section.lower() or \
           "bubbles through" in section.lower()
