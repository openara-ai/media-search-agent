#Requires -Version 5.1
<#
.SYNOPSIS
    Media Search Agent - one-line Windows installer.

.DESCRIPTION
    Installs Media Search Agent for the current user only.
    No elevation or UAC required - everything lives under %LOCALAPPDATA%.

    One-liner:
      powershell -c "irm https://github.com/openara-ai/media-search-agent/releases/latest/download/install.ps1 | iex"

    With parameters (must use scriptblock form when piped):
      powershell -c "& ([scriptblock]::Create((irm https://github.com/openara-ai/media-search-agent/releases/latest/download/install.ps1))) -Version v0.2.0"

    Run locally:
      powershell -ExecutionPolicy Bypass -File install.ps1 [OPTIONS]
      powershell -ExecutionPolicy Bypass -File install.ps1 -Help

.PARAMETER Version
    Tagged release to install (e.g. v0.2.0).
    Default: latest published GitHub release.
    Also settable via env: $env:MSA_VERSION

.PARAMETER Bundle
    Path to a pre-downloaded bundle archive (.zip).
    Skips the GitHub download - useful for testing.
    Example: -Bundle C:\Downloads\MediaSearchAgent-0.2.0-windows-x86_64.zip

.PARAMETER AppDir
    App internals directory (venv, repo, logs, uv).
    Default: %LOCALAPPDATA%\MediaSearchAgent

.PARAMETER DataDir
    User data directory (index, config.yaml).
    Default: %USERPROFILE%\MediaSearchAgent

.PARAMETER SkipAutoStart
    Skip Task Scheduler registration (no auto-start on login).

.PARAMETER SkipLaunch
    Skip launching the tray app at the end of installation.

.PARAMETER Help
    Show this help and exit.

.EXAMPLE
    # Install latest release
    powershell -ExecutionPolicy Bypass -File install.ps1

.EXAMPLE
    # Install a specific version
    powershell -ExecutionPolicy Bypass -File install.ps1 -Version v0.2.0

.EXAMPLE
    # Test with a locally built bundle
    powershell -ExecutionPolicy Bypass -File install.ps1 -Bundle C:\builds\MediaSearchAgent-0.2.0-windows-x86_64.zip

.EXAMPLE
    # Custom install paths, no auto-start
    powershell -ExecutionPolicy Bypass -File install.ps1 -AppDir D:\MSA\app -DataDir D:\MSA\data -SkipAutoStart
#>
[Diagnostics.CodeAnalysis.SuppressMessageAttribute('PSAvoidUsingWriteHost', '',
    Justification = 'Interactive install script - coloured console output is intentional.')]
[Diagnostics.CodeAnalysis.SuppressMessageAttribute('PSAvoidUsingEmptyCatchBlock', '',
    Justification = 'Transcript shutdown in Stop-InstallerLogging is best-effort and should not mask the original install error.')]
[Diagnostics.CodeAnalysis.SuppressMessageAttribute('PSReviewUnusedParameter', 'Version',
    Justification = 'Used indirectly by Resolve-MsaVersion; PSScriptAnalyzer does not follow that control flow.')]
[Diagnostics.CodeAnalysis.SuppressMessageAttribute('PSReviewUnusedParameter', 'SkipAutoStart',
    Justification = 'Used indirectly by Install-TaskScheduler; PSScriptAnalyzer does not follow that control flow.')]
[Diagnostics.CodeAnalysis.SuppressMessageAttribute('PSReviewUnusedParameter', 'AllowDowngrade',
    Justification = 'Used indirectly by Test-VersionDowngrade; PSScriptAnalyzer does not follow that control flow.')]
[Diagnostics.CodeAnalysis.SuppressMessageAttribute('PSUseSingularNouns', '',
    Justification = 'Installer helper names prioritise readability and match multi-target side effects.')]
[Diagnostics.CodeAnalysis.SuppressMessageAttribute('PSUseShouldProcessForStateChangingFunctions', '',
    Justification = 'Internal installer helpers only mutate installer-owned temp directories and install state.')]
[CmdletBinding()]
param(
    [string] $Version       = "",
    [string] $Bundle        = "",
    [string] $AppDir        = "$env:LOCALAPPDATA\MediaSearchAgent",
    [string] $DataDir       = "$env:USERPROFILE\MediaSearchAgent",
    [switch] $SkipAutoStart,
    [switch] $SkipLaunch,
    [switch] $AllowDowngrade,
    [switch] $Help
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

# Normalise trailing backslashes on the user-overridable paths. Without this,
# `-AppDir C:\MSA\` produces patterns like `C:\MSA\\*` in subsequent
# `-like "$AppDir\*"` checks (e.g. the orphan-process kill ownership check),
# which never match real process paths - the installer would then declare
# its own API process "outside $AppDir" and skip termination, re-opening the
# corruption/deadlock class the Stop-RunningServices guard exists to prevent.
# Caught by Codex on PR #132 right before merge.
#
# Drive-root guard: leave `C:\`, `D:\`, etc. as-is. Trimming would turn
# `C:\` into `C:` which is a valid path syntax but produces *much* broader
# `-like "C:\*"` patterns that would match anything on the drive - making
# the ownership guard overly broad rather than spuriously narrow. Caught
# by Copilot on PR #133.
if ($AppDir -notmatch '^[A-Za-z]:\\$') { $AppDir = $AppDir.TrimEnd('\') }
if ($DataDir -notmatch '^[A-Za-z]:\\$') { $DataDir = $DataDir.TrimEnd('\') }

# -- Help ---------------------------------------------------------------------

if ($Help) {
    Write-Host ""
    Write-Host "  Media Search Agent Installer" -ForegroundColor Cyan
    Write-Host "  Local-first semantic search for your photos and videos" -ForegroundColor Gray
    Write-Host ""
    Write-Host "USAGE" -ForegroundColor White
    Write-Host '  powershell -c "irm https://github.com/openara-ai/media-search-agent/releases/latest/download/install.ps1 | iex"'
    Write-Host "  powershell -ExecutionPolicy Bypass -File install.ps1 [OPTIONS]"
    Write-Host ""
    Write-Host "OPTIONS" -ForegroundColor White
    Write-Host "  -Version <tag>         Release tag to install (e.g. v0.2.0)"
    Write-Host "                         Default: latest published GitHub release"
    Write-Host "                         Also: set env MSA_VERSION=v0.2.0"
    Write-Host ""
    Write-Host "  -Bundle <path>         Path to a pre-downloaded bundle .zip"
    Write-Host "                         Skips GitHub download; useful for testing"
    Write-Host "                         Example: -Bundle C:\builds\MediaSearchAgent-0.2.0-windows-x86_64.zip"
    Write-Host ""
    Write-Host "  -AppDir <path>         App internals (venv, repo, uv, logs)"
    Write-Host "                         Default: $env:LOCALAPPDATA\MediaSearchAgent"
    Write-Host ""
    Write-Host "  -DataDir <path>        User data (index, config.yaml)"
    Write-Host "                         Default: $env:USERPROFILE\MediaSearchAgent"
    Write-Host ""
    Write-Host "  -SkipAutoStart         Skip Task Scheduler registration"
    Write-Host ""
    Write-Host "  -SkipLaunch            Skip launching the tray app after install"
    Write-Host ""
    Write-Host "  -AllowDowngrade        Allow installing an older version over a newer one"
    Write-Host "                         (default: refuse - downgrades can corrupt the index)"
    Write-Host ""
    Write-Host "  -Help                  Show this help and exit"
    Write-Host ""
    Write-Host "INSTALL PATHS" -ForegroundColor White
    Write-Host "  Repo:    $env:LOCALAPPDATA\MediaSearchAgent\repo"
    Write-Host "  Venv:    $env:LOCALAPPDATA\MediaSearchAgent\.venv"
    Write-Host "  Logs:    $env:LOCALAPPDATA\MediaSearchAgent\logs"
    Write-Host "  Config:  $env:USERPROFILE\MediaSearchAgent\config.yaml"
    Write-Host "  Launcher:$env:LOCALAPPDATA\MediaSearchAgent\bin\msa.cmd"
    Write-Host ""
    exit 0
}

# -- Paths (user-space only, no elevation required, ADR-009) ------------------

$RepoDir    = "$AppDir\repo"
$VenvDir    = "$AppDir\.venv"
$CacheDir   = "$AppDir\Cache"
$LogDir     = "$AppDir\logs"
$LauncherDir = "$AppDir\bin"
$Launcher   = "$LauncherDir\msa.cmd"
$TrayExe    = "$LauncherDir\MediaSearchAgentTray.exe"
$TrayPathsEnv = "$LauncherDir\msa-paths.env"
$StartScript = "$AppDir\start.ps1"
$StopScript  = "$AppDir\stop.ps1"
$StartMenuDir = Join-Path $env:APPDATA "Microsoft\Windows\Start Menu\Programs\Media Search Agent"
$ConfigPath = "$DataDir\config.yaml"
# Written at the end of a successful install; read at the top of the next
# install run to detect (and refuse) version downgrades.
$VersionFile = "$AppDir\version.txt"

$PythonVersion = "3.12"
$GithubRepo    = "openara-ai/media-search-agent"

# PyTorch CUDA index - cu128 wheels build for Blackwell sm_120 (RTX 5000) and
# remain compatible with Ampere/Ada. Used by Install-Torch *only* when an
# NVIDIA GPU is detected at install time (Test-NvidiaPresent). Installing
# CUDA-enabled wheels on a machine without NVIDIA hardware crashes subprocess
# torch imports at the Windows loader (STATUS_DLL_INIT_FAILED, 0xC0000142)
# with no Python traceback - see Test-NvidiaPresent for the full failure mode.
$TorchIndexUrl = "https://download.pytorch.org/whl/cu128"

# -- Logging ------------------------------------------------------------------

function Write-Ok($msg)   { Write-Host "  + $msg" -ForegroundColor Green }
function Write-Info($msg) { Write-Host "  * $msg" -ForegroundColor Gray }
function Write-Skip($msg) { Write-Host "  - $msg" -ForegroundColor DarkGray }
function Write-Warn($msg) { Write-Host "  ! $msg" -ForegroundColor Yellow }
function Stop-InstallerLogging {
    if ($script:TranscriptStarted) {
        try {
            Stop-Transcript | Out-Null
        } catch {
            # Best-effort only - we still want the installer to fail with the
            # original error rather than masking it with transcript shutdown noise.
        }
        $script:TranscriptStarted = $false
    }
}

function Write-Fail($msg) {
    Write-Host "  x $msg" -ForegroundColor Red
    # PowerShell 5.1 does not reliably guarantee finally will run on exit from
    # inside a try block, so close the transcript here before terminating.
    Stop-InstallerLogging
    exit 1
}
function Write-Step($msg) { Write-Host "`n$msg" -ForegroundColor White }

# Invoke a native command and fail hard if it exits non-zero.
# PowerShell 5.1 does not throw on native command failures - this wrapper
# ensures every uv call is checked explicitly.
function Invoke-Native {
    param([string]$Desc, [scriptblock]$Cmd)
    & $Cmd
    if ($LASTEXITCODE -ne 0) {
        Write-Fail "$Desc failed (exit $LASTEXITCODE)"
    }
}

function Write-Banner {
    # The banner header must be the very first thing the user sees; install
    # log path + mode/markers/reason come after so they look like context
    # for the install, not preamble before the installer announces itself.
    Write-Host ""
    Write-Host "  Media Search Agent Installer" -ForegroundColor Cyan
    Write-Host "  Local-first semantic search for your photos and videos" -ForegroundColor Gray
    Write-Host ""
    Write-Info "AppDir:  $AppDir"
    Write-Info "DataDir: $DataDir"
    Write-Info "Scope:   current user only (other Windows accounts on this machine need their own install)"
    if ($script:InstallerLog) {
        Write-Info "Log:     $script:InstallerLog"
        Write-Info "Mode:    $script:InstallerMode"
        Write-Info "Markers: $script:InstallModeMarkers"
        Write-Info "Reason:  $script:InstallModeReason"
    }
    Write-Host ""
}

function Test-WindowsArchitecture {
    # Windows currently supports x86_64 only. ARM64 Windows would fail deep
    # inside `pip install torch` with a confusing wheel-resolution error;
    # fail fast here with a clear message instead.
    #
    # macOS already enforces this in install.sh (dies on Intel Mac); this
    # is the Windows parity check.
    # PROCESSOR_ARCHITECTURE reflects the *process* architecture, not the OS.
    # On 64-bit Windows launched from a 32-bit PowerShell host this comes back
    # as "x86" even though the OS is 64-bit, producing a false-positive
    # "x86_64 only" hard stop. The PROCESSOR_ARCHITEW6432 env var is set in
    # 32-bit processes on 64-bit Windows and reports the underlying OS arch;
    # prefer it when present and fall back to PROCESSOR_ARCHITECTURE for the
    # native (most common) case.
    $arch = if ($env:PROCESSOR_ARCHITEW6432) { $env:PROCESSOR_ARCHITEW6432 } else { $env:PROCESSOR_ARCHITECTURE }
    $is64 = [Environment]::Is64BitOperatingSystem
    if (-not $is64 -or ($arch -notmatch '^(AMD64|x86_64)$')) {
        Write-Fail "Media Search Agent currently supports Windows x86_64 only. Detected: arch=$arch (process=$env:PROCESSOR_ARCHITECTURE, OS=$env:PROCESSOR_ARCHITEW6432), Is64BitOS=$is64. ARM64 Windows is not yet supported - check https://github.com/$GithubRepo/releases for updates."
    }
}

function Test-SystemRequirements {
    # Windows version floor: build 10240+ is the hard floor (Win10 RTM).
    # Build 17763 (1809) is recommended for reliable Scheduled Tasks /
    # WebView2 behaviour; below that we warn but continue.
    $winVer = [Environment]::OSVersion.Version
    if ($winVer.Major -lt 10) {
        Write-Fail "Windows 10 or newer required. Detected: $($winVer.Major).$($winVer.Minor) (build $($winVer.Build))."
    }
    if ($winVer.Major -eq 10 -and $winVer.Build -lt 17763) {
        Write-Warn "Windows 10 1809+ recommended (build 17763+). Detected build $($winVer.Build); auto-start and tray may be flaky on older builds."
    }

    # Free disk: bundle (~50 MB) + venv (~500 MB) + torch wheels (~2 GB CUDA
    # / ~500 MB CPU) + scratch. 5 GB is the comfortable floor; under that
    # uv pip install can fail mid-resolve with cryptic disk errors.
    $drive = Split-Path $AppDir -Qualifier
    $driveLetter = $drive.TrimEnd(':')
    try {
        $freeGb = [math]::Round((Get-PSDrive $driveLetter -ErrorAction Stop).Free / 1GB, 1)
        if ($freeGb -lt 5) {
            Write-Fail "Need at least 5 GB free on $drive drive for the install. Available: ${freeGb} GB. Free up space or pass -AppDir to install on a drive with more room."
        }
    } catch {
        Write-Warn "Could not check free space on $drive ; continuing."
    }

    # RAM is best-effort warning only - small libraries index fine on less,
    # and we don't want to block users who just want to try the app on a
    # small collection.
    try {
        $ramGb = [math]::Round(
            (Get-CimInstance Win32_ComputerSystem -ErrorAction Stop).TotalPhysicalMemory / 1GB, 1)
        if ($ramGb -lt 8) {
            Write-Warn "Only ${ramGb} GB RAM detected. 8+ GB recommended; indexing large libraries (10k+ items) may OOM."
        }
    } catch {
        # Silent skip - WMI hiccups shouldn't block install.
    }
}

function Get-InstallMode {
    $markers = 0
    $found = @()

    if (Test-Path $RepoDir) {
        $markers += 1
        $found += "repo_dir"
    }
    if (Test-Path $VenvDir) {
        $markers += 1
        $found += "venv"
    }
    if (Test-Path $Launcher) {
        $markers += 1
        $found += "launcher"
    }
    if (Test-Path $ConfigPath) {
        $markers += 1
        $found += "config"
    }

    if ($markers -eq 0) {
        $script:InstallModeReason = "No existing install markers found."
        $script:InstallModeMarkers = "none"
        return "install"
    }
    if ($markers -ge 3) {
        $script:InstallModeReason = "Found 3 or more install markers, treating this run as an upgrade."
        $script:InstallModeMarkers = ($found -join ",")
        return "upgrade"
    }
    $script:InstallModeReason = "Found partial install state, treating this run as a repair."
    $script:InstallModeMarkers = ($found -join ",")
    return "repair"
}

function Initialize-Logging {
    # Set up transcript only; do NOT print anything here. The user-visible
    # log/mode/markers/reason lines are printed by Write-Banner after the
    # banner header so the very first console line is "Media Search Agent
    # Installer" rather than implementation chatter.
    param([string]$Mode)

    if (-not (Test-Path $LogDir)) {
        New-Item -ItemType Directory -Path $LogDir -Force | Out-Null
    }

    $timestamp = Get-Date -Format "yyyy-MM-dd_HHmmss"
    $script:InstallerLog = Join-Path $LogDir "$Mode-$timestamp.log"
    $script:InstallerMode = $Mode
    Start-Transcript -Path $script:InstallerLog -Force | Out-Null
    $script:TranscriptStarted = $true
}

# -- Download helper ----------------------------------------------------------

function Get-FileFromUrl($url, $dest, $desc) {
    # Print the full URL so the user can confirm bytes are coming from
    # github.com (or wherever) rather than a random host. Trust-signal
    # parity with the first-launch SetupPage which surfaces model sources.
    Write-Info "Downloading $desc..."
    Write-Info "  from $url"
    $dir = Split-Path $dest -Parent
    if (-not (Test-Path $dir)) { New-Item -ItemType Directory -Path $dir -Force | Out-Null }
    Invoke-WebRequest -Uri $url -OutFile $dest -UseBasicParsing
}

function Test-BundleSha256 {
    # Verify the downloaded bundle against SHA256SUMS.txt published alongside
    # the release. Closes the obvious supply-chain hole on an unsigned
    # installer: a hijacked / MITM'd / corrupted bundle now fails before
    # Expand-Archive instead of getting installed and run.
    #
    # Skipped for local --Bundle installs (caller handles trust). If
    # SHA256SUMS.txt is missing from the release (older releases predate
    # this file), warn rather than fail so existing releases still install.
    param(
        [string] $BundleFile,
        [string] $BundleName,
        [string] $ReleaseBaseUrl
    )

    $sumsUrl = "$ReleaseBaseUrl/SHA256SUMS.txt"
    $sumsText = $null
    Write-Info "Verifying bundle integrity..."
    Write-Info "  from $sumsUrl"
    try {
        $sumsText = Invoke-WebRequest -Uri $sumsUrl -UseBasicParsing `
            -ErrorAction Stop | Select-Object -ExpandProperty Content
    } catch {
        # Distinguish "release predates SHA256SUMS.txt" (HTTP 404, the legacy
        # fallback case) from "fetch failed for some other reason" (transient
        # TLS / proxy / 5xx / connection reset). The legacy fallback warns and
        # proceeds; everything else hard-fails because the alternative is
        # silently extracting an unverified bundle - exactly the supply-chain
        # guard bypass this whole function was added to prevent.
        $status = $null
        try {
            if ($_.Exception.Response) { $status = [int]$_.Exception.Response.StatusCode }
        } catch {
            $status = $null
        }
        if ($status -eq 404) {
            Write-Warn "SHA256SUMS.txt not found (HTTP 404) at $sumsUrl - skipping integrity check. The release may predate signed checksums."
            return
        }
        $statusDesc = if ($status) { "HTTP $status" } else { "transport failure" }
        Write-Fail "Could not fetch SHA256SUMS.txt from $sumsUrl ($statusDesc): $($_.Exception.Message). Refusing to install an unverified bundle - retry once the network is healthy, or pass -Bundle <local-path> to install a copy you've verified yourself."
    }

    $expected = $null
    foreach ($line in $sumsText -split "`n") {
        $line = $line.Trim()
        if (-not $line -or $line.StartsWith("#")) { continue }
        $parts = $line -split '\s+', 2
        if ($parts.Length -lt 2) { continue }
        # SHA256SUMS.txt files commonly prefix filenames with `*` or `./`;
        # strip both before comparing.
        $name = $parts[1].TrimStart('*', '.', '/').Trim()
        if ($name -eq $BundleName) {
            $expected = $parts[0].ToLower()
            break
        }
    }

    if (-not $expected) {
        Write-Warn "$BundleName not listed in SHA256SUMS.txt - skipping integrity check."
        return
    }

    $actual = (Get-FileHash $BundleFile -Algorithm SHA256).Hash.ToLower()
    if ($actual -ne $expected) {
        Write-Fail "Bundle SHA256 mismatch for ${BundleName}: expected $expected, got $actual. The download may be corrupted or tampered with - aborting before extract."
    }
    Write-Ok "Bundle SHA256 verified ($($expected.Substring(0,12))...)"
}

# -- Version resolution -------------------------------------------------------

function Resolve-MsaVersion {
    if ($Version) { return $Version }
    if ($env:MSA_VERSION) { return $env:MSA_VERSION }
    # If a local bundle is supplied the version is only used for display; skip API call.
    if ($Bundle) { return "(local bundle)" }
    $releasesUrl = "https://api.github.com/repos/$GithubRepo/releases/latest"
    Write-Info "Resolving latest version from GitHub..."
    Write-Info "  from $releasesUrl"
    try {
        $rel = Invoke-RestMethod $releasesUrl
        return $rel.tag_name
    } catch {
        Write-Fail "Could not resolve latest version from GitHub. Use -Version v0.2.0 to specify one."
    }
}

function ConvertTo-MsaVersionObject($tag) {
    # Parse a tag like "v0.7.70" or "0.7.3-test6" into a [version] for compare.
    # Pre-release suffixes (-test6, -rc1) are stripped so the numeric portion
    # is what's compared - good enough for the downgrade-guard use case.
    # Returns $null on unparseable input.
    if (-not $tag) { return $null }
    $bare = $tag -replace '^v', ''
    $numeric = ($bare -split '-')[0]
    try { return [version]$numeric } catch { return $null }
}

function Test-VersionDowngrade($NewTag) {
    # Reads the existing $VersionFile (written by a prior successful install)
    # and refuses to install an older version on top, unless -AllowDowngrade
    # was passed. Silent no-op for fresh installs and for installs where the
    # version file is missing (legacy installs from before this guard).
    #
    # Downgrades are dangerous because the SQLite schema in index/media.sqlite
    # can move forward between versions; an older binary opening a newer DB
    # can fail or, worse, silently corrupt rows.
    if ($NewTag -eq "(local bundle)") {
        # Local-bundle installs don't know their version reliably; skip.
        return
    }
    if (-not (Test-Path $VersionFile)) {
        return
    }
    $existingTag = $null
    try {
        $existingTag = (Get-Content $VersionFile -ErrorAction Stop |
            Select-Object -First 1).Trim()
    } catch {
        Write-Warn "Could not read $VersionFile ; skipping downgrade check."
        return
    }
    if (-not $existingTag) { return }

    $newVer = ConvertTo-MsaVersionObject $NewTag
    $existingVer = ConvertTo-MsaVersionObject $existingTag
    if (-not $newVer -or -not $existingVer) {
        Write-Warn "Could not parse versions (new=$NewTag, existing=$existingTag); skipping downgrade check."
        return
    }

    if ($newVer -lt $existingVer) {
        if ($AllowDowngrade) {
            Write-Warn "Downgrading from $existingTag to $NewTag (forced by -AllowDowngrade)."
        } else {
            Write-Fail "Refusing to downgrade from $existingTag to $NewTag. Re-run with -AllowDowngrade to force. Downgrades can corrupt index/media.sqlite if the schema moved forward between versions."
        }
    } elseif ($newVer -gt $existingVer) {
        Write-Info "Upgrading from $existingTag to $NewTag"
    } else {
        # Same version: a rerun is effectively a force-repair, not a no-op.
        # Install-Bundle wipes $RepoDir and re-extracts; pip install re-runs;
        # launcher / tray / scheduled task get re-registered. Only user data
        # (config.yaml, index, model cache, logs) is preserved. Surface this
        # so the user can see the rerun did something rather than appearing
        # to be a silent no-op - matters most when the user reruns to repair
        # a broken install.
        Write-Info "Reinstalling $NewTag (re-syncs app files; user data preserved)"
    }
}

# -- Service helpers (port lookup, readiness polling) ------------------------
#
# Small reusable utilities consumed by both the pre-upgrade teardown
# (Stop-RunningServices needs the configured API port to find the right
# listener) and the end-of-install bridge (Wait-ApiReady polls /health
# so the user sees live progress between "tray launched" and "browser
# opens"). Kept in their own section so they aren't misread as part of
# the teardown flow.

# Parse the API port from config.yaml (best-effort; defaults to 8000 if
# the config doesn't exist yet or doesn't declare a port). Used by both
# Stop-RunningServices (to find the process to kill) and Wait-ApiReady
# (to know which URL to poll after launching the tray).
function Get-ConfiguredApiPort {
    $defaultPort = 8000
    if (-not (Test-Path $ConfigPath)) { return $defaultPort }
    try {
        $inApi = $false
        foreach ($line in Get-Content $ConfigPath -ErrorAction SilentlyContinue) {
            if ($line -match '^[^#]*\bapi\s*:\s*$') { $inApi = $true; continue }
            if ($inApi -and $line -match '^[^ \t]') { break }
            if ($inApi -and $line -match '^\s*port\s*:\s*([0-9]+)') {
                return [int]$Matches[1]
            }
        }
    } catch {
        # Config parse failure is non-fatal - fall through to default.
    }
    return $defaultPort
}

# Bridge the gap between "tray process spawned" (Start-Process returns
# immediately) and "browser tab is open" (tray polls /health and opens
# the browser when it goes ready). On a fresh first launch the API can
# take 10-30 s to bind the port, and the installer used to exit during
# that wait, leaving the user staring at a returned shell prompt with
# no idea what was happening. Now we poll /health ourselves and print
# dots until it responds, then announce the "started" stage. The tray's
# own /health poll fires in parallel; the two polls race harmlessly and
# the browser opens within a second or two of our "started" announcement.
#
# IMPORTANT: this function only POLLS /health - it does NOT open the
# browser. The tray is the sole opener; calling Start-Process here too
# would produce a duplicate tab.
function Wait-ApiReady {
    param(
        [int] $TimeoutSec = 90,
        [int] $Port = 8000
    )
    Write-Host -NoNewline "  Starting the app" -ForegroundColor DarkGray
    # Probe 127.0.0.1 explicitly rather than `localhost`. PowerShell 5.1's
    # Invoke-WebRequest goes through Windows name resolution, which on
    # modern Windows resolves `localhost` to ::1 (IPv6) FIRST and only
    # falls back to 127.0.0.1 after the IPv6 connect times out. uvicorn
    # binds IPv4 only, so probing `localhost` from PS 5.1 burns the full
    # -TimeoutSec on IPv6 every iteration and never detects the API even
    # when it's healthily serving on 127.0.0.1. The C# tray uses
    # HttpClient and avoids this; only the installer's PS poll trips it.
    # Real-VM symptom: tray + browser come up, installer's "Starting the
    # app........" keeps printing dots forever.
    #
    # Track WALL-CLOCK elapsed time, not loop iterations. Each iteration
    # blocks for up to `Invoke-WebRequest -TimeoutSec 3` plus
    # `Start-Sleep -Seconds 1`, so a TimeoutSec of 90 counted by
    # iterations could actually spend ~360 s on a failure path - and the
    # warning saying "didn't respond within ${TimeoutSec}s" would be
    # wildly misleading. [DateTime]::UtcNow gives us a real elapsed
    # measure that respects the contract the warning prints.
    $start = [DateTime]::UtcNow
    while (([DateTime]::UtcNow - $start).TotalSeconds -lt $TimeoutSec) {
        try {
            $r = Invoke-WebRequest -Uri "http://127.0.0.1:$Port/health" `
                -UseBasicParsing -TimeoutSec 3 -ErrorAction Stop
            if ($r.StatusCode -eq 200) {
                Write-Host ""
                Write-Host ""
                Write-Host "  + Media Search Agent started!" -ForegroundColor Green
                Write-Host "  Your browser will open at http://localhost:$Port" -ForegroundColor DarkGray
                Write-Host ""
                # Give the tray's parallel /health poll ~2s to fire and
                # open the browser tab before this installer exits.
                # Without this pause the shell prompt can return before
                # the browser opens (the installer's poll wins the race
                # by a fraction of a second) and the user sees a brief
                # confusing silence between the success line and the tab.
                Start-Sleep -Seconds 2
                return $true
            }
        } catch {
            # Health endpoint not up yet; keep dotting.
        }
        Write-Host -NoNewline "."
        Start-Sleep -Seconds 1
    }
    Write-Host ""
    Write-Warn "API didn't respond on http://localhost:$Port/health within ${TimeoutSec}s. Check the tray app menu and the install log at $script:InstallerLog."
    return $false
}

# -- Pre-upgrade teardown ------------------------------------------------------
#
# On upgrade/repair we must stop any running MSA processes before touching files.
# The tray exe is locked by Windows while the process runs, so Copy-Item fails.
# The API venv may hold locks on Python files too.
# Order: stop API first (graceful), then kill tray.

function Stop-RunningServices {
    # 1. Stop API gracefully via the existing launcher if present.
    if (Test-Path $Launcher) {
        try {
            Write-Info "Stopping API service..."
            $p = Start-Process -FilePath "cmd.exe" `
                -ArgumentList "/c `"$Launcher`" api stop" `
                -WindowStyle Hidden -PassThru -Wait
            if ($p.ExitCode -eq 0) {
                Write-Ok "API stopped"
            } else {
                Write-Warn "msa api stop exited $($p.ExitCode) - will attempt force-kill"
            }
        } catch {
            Write-Warn "Could not stop API via launcher: $_"
        }
    }

    # 2. Force-kill any lingering python/uvicorn process on the API port.
    try {
        $configPort = Get-ConfiguredApiPort
        $conn = Get-NetTCPConnection -LocalPort $configPort -State Listen `
            -ErrorAction SilentlyContinue | Select-Object -First 1
        if ($conn) {
            $proc = Get-Process -Id $conn.OwningProcess -ErrorAction SilentlyContinue
            if ($proc) {
                $procPath = try { [string]$proc.Path } catch { '' }
                # Kill if EITHER:
                #   (a) the exe path is resolvable AND lives under $AppDir
                #       (strong signal: this is our process); OR
                #   (b) the exe path is unresolvable (null/empty) AND the
                #       process name is python/pythonw/uvicorn (recovery for
                #       the half-deleted-venv orphan case where $proc.Path
                #       comes back null because the file was removed under
                #       a still-running process).
                #
                # The name-only check used to be OR'd unconditionally, which
                # would kill UNRELATED dev python processes happening to
                # listen on port 8000 (Django, FastAPI dev, Jupyter on
                # alternate port, etc.). Guarding it behind "$procPath is
                # empty" keeps the orphan-recovery path while protecting
                # external processes whose path is fully resolvable.
                $isMsaApi = ($procPath -and ($procPath -like "$AppDir\*")) -or
                            ((-not $procPath) -and ($proc.ProcessName -match '^(python|pythonw|uvicorn)$'))
                if (-not $isMsaApi) {
                    Write-Warn "Port $configPort is held by $($proc.Name) (PID $($proc.Id)) outside $AppDir - skipping"
                } else {
                    # Nested try/catch around the kill + verify block so an
                    # unexpected exception (e.g. WaitForExit throwing because
                    # the process handle became invalid) is treated as FATAL,
                    # not downgraded to a Write-Warn by the outer catch. Half-
                    # delete-the-venv-around-a-still-running-python is exactly
                    # the failure mode this whole block exists to prevent;
                    # letting the outer catch swallow it would re-introduce it.
                    try {
                        $killErr = $null
                        try {
                            Stop-Process -Id $proc.Id -Force -ErrorAction Stop
                        } catch {
                            # Could be "no such process" (benign race: died on its own),
                            # "access denied" (AV/elevated), etc. WaitForExit below is
                            # the source of truth - if the process is actually gone the
                            # benign race doesn't matter.
                            $killErr = $_.Exception.Message
                        }
                        # Stop-Process is fire-and-forget; verify the process actually
                        # terminated before continuing so we don't half-delete the venv
                        # around a still-running python.
                        if (-not $proc.WaitForExit(5000)) {
                            $detail = if ($killErr) { "Stop-Process raised: $killErr" } else { "process did not exit within 5s" }
                            Write-Fail "Could not stop PID $($proc.Id) holding port ${configPort} ($detail).`n  Open an Administrator cmd and run:  taskkill /F /PID $($proc.Id)`n  Then re-run the installer."
                        }
                        Write-Ok "Force-killed $($proc.Name) (PID $($proc.Id)) on port $configPort"
                    } catch {
                        # WaitForExit / property access / unexpected throw: we
                        # could NOT verify the kill, which is just as bad as a
                        # confirmed failed kill. Refuse to proceed.
                        Write-Fail "Process-stop verification failed for PID $($proc.Id) ($($_.Exception.Message)). Cannot confirm the API process is gone; refusing to proceed with destructive install steps.`n  Open an Administrator cmd and run:  taskkill /F /PID $($proc.Id)`n  Then re-run the installer."
                    }
                }
            }
        }
    } catch {
        # Stop-Process Write-Fail calls exit 1, which bubbles through this
        # outer catch in PowerShell (terminators are not caught). Other
        # exceptions (e.g. Get-NetTCPConnection oddness) are non-fatal warns.
        Write-Warn "Could not check/kill API port listener: $_"
    }

    # 3. Kill the tray app so the exe file is released before we overwrite it.
    try {
        $trayProcs = Get-Process -Name "MediaSearchAgentTray" -ErrorAction SilentlyContinue |
            Where-Object { $_.Path -like "$AppDir*" }
        foreach ($p in $trayProcs) {
            Stop-Process -Id $p.Id -Force -ErrorAction SilentlyContinue
            Write-Ok "Stopped tray app (PID $($p.Id))"
        }
        if ($trayProcs) {
            # Brief pause so Windows releases the file lock before we overwrite the exe.
            Start-Sleep -Milliseconds 500
        }
    } catch {
        Write-Warn "Could not stop tray process: $_"
    }
}

# -- Bundle download + extract ------------------------------------------------

function Install-Bundle($tag) {
    $tmpExtract = "$env:TEMP\msa-bundle-extract"
    if (Test-Path $tmpExtract) { Remove-Item $tmpExtract -Recurse -Force }

    if ($Bundle) {
        if (-not (Test-Path $Bundle)) {
            Write-Fail "Bundle file not found: $Bundle"
        }
        Write-Info "Using local bundle: $Bundle"
        # Local --Bundle path: skip SHA256 verification. The user has the file
        # locally and is responsible for trusting it; this also makes the
        # local-test workflow keep working without needing a SHA256SUMS.txt
        # alongside the bundle.
        Write-Info "Extracting bundle..."
        Expand-Archive -Path $Bundle -DestinationPath $tmpExtract -Force
    } else {
        $bare = $tag -replace '^v', ''
        $bundleName = "MediaSearchAgent-${bare}-windows-x86_64"
        $bundleBase = "https://github.com/$GithubRepo/releases/download/$tag"
        $bundleUrl  = "$bundleBase/${bundleName}.zip"

        $bundleZip = "$env:TEMP\msa-bundle.zip"
        Get-FileFromUrl $bundleUrl $bundleZip "bundle $tag"
        Test-BundleSha256 $bundleZip "${bundleName}.zip" $bundleBase
        Write-Info "Extracting bundle..."
        Expand-Archive -Path $bundleZip -DestinationPath $tmpExtract -Force
        Remove-Item $bundleZip -Force
    }

    # Find the extracted bundle directory regardless of its name
    $bundleDir = Get-ChildItem $tmpExtract -Directory | Select-Object -First 1 -ExpandProperty FullName
    if (-not $bundleDir -or -not (Test-Path $bundleDir)) {
        Write-Fail "Bundle directory not found after extract"
    }

    # Validate required bundle contents before touching the existing install
    foreach ($item in @("src", "scripts", "pyproject.toml", "requirements.txt")) {
        if (-not (Test-Path (Join-Path $bundleDir $item))) {
            Write-Fail "Bundle is missing required item: $item"
        }
    }
    if (-not (Test-Path (Join-Path $bundleDir "src\msa_apps\ui\dist"))) {
        Write-Fail "Bundle is missing src\msa_apps\ui\dist (UI was not built)"
    }
    if (-not (Test-Path (Join-Path $bundleDir "bin\uv.exe"))) {
        Write-Fail "Bundle is missing bin\uv.exe"
    }
    if (-not (Test-Path (Join-Path $bundleDir "config.yaml.template"))) {
        Write-Fail "Bundle is missing config.yaml.template"
    }
    foreach ($item in @("start.ps1", "stop.ps1")) {
        if (-not (Test-Path (Join-Path $bundleDir $item))) {
            Write-Fail "Bundle is missing $item"
        }
    }
    if (-not (Test-Path (Join-Path $bundleDir "bin\MediaSearchAgentTray.exe"))) {
        Write-Fail "Bundle is missing bin\MediaSearchAgentTray.exe"
    }
    foreach ($tool in @("exiftool.exe")) {
        if (-not (Test-Path (Join-Path $bundleDir "bin\$tool"))) {
            Write-Fail "Bundle is missing bin\$tool"
        }
    }
    if (-not (Test-Path (Join-Path $bundleDir "bin\exiftool_files"))) {
        Write-Fail "Bundle is missing bin\exiftool_files\ (ExifTool's companion Perl-modules directory)"
    }

    # Safe to replace existing install - all required items confirmed above
    if (Test-Path $RepoDir) {
        Write-Info "Removing previous install at $RepoDir..."
        Remove-Item $RepoDir -Recurse -Force
    }
    New-Item -ItemType Directory -Path $RepoDir -Force | Out-Null

    foreach ($item in @("src", "scripts", "pyproject.toml", "requirements.txt",
                        "requirements-windows.txt", "LICENSE", "NOTICE", "uninstall.ps1", "wheels")) {
        $src = Join-Path $bundleDir $item
        if (Test-Path $src) {
            Copy-Item $src (Join-Path $RepoDir $item) -Recurse -Force
        }
    }

    Copy-Item (Join-Path $bundleDir "start.ps1") $StartScript -Force
    Copy-Item (Join-Path $bundleDir "stop.ps1") $StopScript -Force

    if (-not (Test-Path $LauncherDir)) {
        New-Item -ItemType Directory -Path $LauncherDir -Force | Out-Null
    }
    foreach ($tool in @("exiftool.exe")) {
        Copy-Item (Join-Path $bundleDir "bin\$tool") (Join-Path $LauncherDir $tool) -Force
    }
    # ExifTool's Perl runtime + modules ship in a companion directory next to
    # the exe; without it, exiftool.exe runs but fails on most operations.
    $bundledExiftoolFiles   = Join-Path $bundleDir "bin\exiftool_files"
    $installedExiftoolFiles = Join-Path $LauncherDir "exiftool_files"
    if (Test-Path $installedExiftoolFiles) {
        Remove-Item $installedExiftoolFiles -Recurse -Force
    }
    Copy-Item $bundledExiftoolFiles $installedExiftoolFiles -Recurse -Force
    Write-Ok "Bundled tools installed to $LauncherDir"

    # Install uv.exe to AppDir\uv\ so it does not conflict with other installs
    $UvDir = "$AppDir\uv"
    $UvExe = "$UvDir\uv.exe"
    $bundledUv = Join-Path $bundleDir "bin\uv.exe"
    if (Test-Path $bundledUv) {
        if (-not (Test-Path $UvDir)) { New-Item -ItemType Directory -Path $UvDir -Force | Out-Null }
        Copy-Item $bundledUv $UvExe -Force
        Write-Ok "uv installed"
    } else {
        Write-Fail "uv.exe not found in bundle"
    }

    # Save config template path for Initialize-Config
    $script:BundleConfigTemplate = Join-Path $bundleDir "config.yaml.template"

    # Save tray exe path for Install-TrayApp
    $script:BundleTrayExe = Join-Path $bundleDir "bin\MediaSearchAgentTray.exe"

    # Note: do NOT remove $tmpExtract yet - config template and tray exe are
    # still needed. Cleanup happens after all bundle-sourced steps complete.
    $script:BundleTmpDir = $tmpExtract

    Write-Ok "Bundle installed to $RepoDir ($tag)"
}

function Clear-BundleTempDir {
    if ($script:BundleTmpDir -and (Test-Path $script:BundleTmpDir)) {
        Remove-Item $script:BundleTmpDir -Recurse -Force
        $script:BundleTmpDir = $null
    }
}

# -- Python + venv ------------------------------------------------------------

function Initialize-Python($UvExe) {
    Write-Info "Python $PythonVersion..."
    Invoke-Native "uv python install" { & $UvExe python install $PythonVersion --quiet }

    if (Test-Path $VenvDir) {
        Write-Skip "venv already exists at $VenvDir"
    } else {
        Write-Info "Creating venv at $VenvDir..."
        Invoke-Native "uv venv" { & $UvExe venv $VenvDir --python $PythonVersion --quiet }
        Write-Ok "venv created"
    }
}

function Test-NvidiaPresent {
    # Returns $true iff the machine has an NVIDIA discrete GPU.
    #
    # WMI is the right source here: it detects hardware presence without
    # needing nvidia-smi on PATH or the CUDA runtime loaded. Filters out
    # RDP virtual adapters and the Microsoft Basic Display Adapter that
    # Windows uses when no real driver is loaded.
    #
    # On any failure (WMI exception, no adapters returned, etc.) we return
    # $false. CPU-only wheels are the safe default: installing CUDA-enabled
    # wheels on a no-NVIDIA machine crashes subprocess torch imports at the
    # Windows loader (STATUS_DLL_INIT_FAILED / 0xC0000142) the first time
    # any subprocess imports torch. The loader fails before Python's main
    # runs, so msa.log is empty and the user only sees an opaque
    # "Application Error" dialog. CPU wheels install everywhere and the
    # runtime device-select still picks CUDA later if it ever appears.
    try {
        $gpus = Get-CimInstance Win32_VideoController -ErrorAction Stop |
            Where-Object {
                $_.Name -match 'NVIDIA' -and
                $_.Name -notmatch 'Virtual|Remote|Basic'
            }
        return [bool]$gpus
    } catch {
        Write-Warn "GPU detection failed; defaulting to CPU-only torch wheels. ($($_.Exception.Message))"
        return $false
    }
}

function Install-Torch($UvExe) {
    # Wheel selection is a one-time install-time decision. Runtime fallback
    # cannot recover from a wrong choice on Windows - see Test-NvidiaPresent.
    #
    # Install torch + torchvision BEFORE requirements-windows.txt is processed,
    # either way. Without this step, transitive torch deps in requirements
    # (open_clip_torch, transformers, etc.) pull whatever the resolver picks
    # and can replace the wheel we just placed.
    $hasNvidia = Test-NvidiaPresent
    $venvPython = "$VenvDir\Scripts\python.exe"

    if ($hasNvidia) {
        # cu128 wheels support Blackwell sm_120 (RTX 5000) and remain
        # compatible with Ampere/Ada.
        Write-Info "NVIDIA GPU detected - installing CUDA-enabled PyTorch ($TorchIndexUrl)..."
        Write-Host "    Downloading from $TorchIndexUrl - this may take several minutes (~2 GB)." -ForegroundColor DarkGray
        Invoke-Native "uv pip install torch torchvision (cuda)" {
            & $UvExe pip install --python $venvPython `
                torch torchvision --index-url $TorchIndexUrl --quiet
        }
        Write-Ok "PyTorch (CUDA) installed"
    } else {
        # Default PyPI Windows torch is CPU-only, which is exactly what we
        # want here. Skips the ~2 GB CUDA download too.
        Write-Info "No NVIDIA GPU detected - installing CPU-only PyTorch from PyPI..."
        Invoke-Native "uv pip install torch torchvision (cpu)" {
            & $UvExe pip install --python $venvPython `
                torch torchvision --quiet
        }
        Write-Ok "PyTorch (CPU) installed"
    }

    # Non-fatal CUDA smoke check. A False here is expected on CPU-only
    # installs and just means GPU inference will fall back to CPU at runtime.
    $prevEAP = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    $cudaOk = & $venvPython -c 'import torch; print(torch.cuda.is_available())' 2>&1
    if ($cudaOk -match "True") {
        $gpuName = & $venvPython -c 'import torch; print(torch.cuda.get_device_name(0))' 2>&1
        Write-Ok "CUDA available: $gpuName"
    } elseif ($hasNvidia) {
        Write-Warn "NVIDIA GPU detected at install time but torch.cuda.is_available() returned False. The driver may need updating; CPU fallback will be used until then."
    } else {
        Write-Info "Using CPU inference (no NVIDIA GPU detected)."
    }
    $ErrorActionPreference = $prevEAP
}

function Install-AppRuntime($UvExe) {
    $reqs = "$RepoDir\requirements-windows.txt"
    if (-not (Test-Path $reqs)) { $reqs = "$RepoDir\requirements.txt" }

    # Strip any uncommented msa-ranker line so the ranker is installed only by the
    # explicit wheel-or-pin branch below (always --no-deps), never by the bulk -r step.
    $rankerRe  = '^\s*msa[-_]ranker\s*(==|@)'
    $reqLines  = Get-Content $reqs
    $rankerPin = ($reqLines | Where-Object { $_ -notmatch '^\s*#' -and $_ -match $rankerRe } | Select-Object -First 1)
    # Drop a trailing inline comment ( #... preceded by space) - pip rejects it on the
    # command line, unlike in a -r file. The leading-space requirement preserves a
    # "#fragment" inside a URL/wheel spec (no preceding space).
    if ($rankerPin) { $rankerPin = ($rankerPin -replace '\s+#.*$', '').Trim() }
    $tmpReqs = Join-Path $env:TEMP "msa-reqs-$PID.txt"
    # Write UTF-8 *without* a BOM. Set-Content -Encoding UTF8 on Windows PowerShell 5.1
    # emits a BOM (U+FEFF), which lands on the first requirement line and can make
    # uv/pip mis-parse it. UTF8Encoding($false) writes no BOM.
    $kept = @($reqLines | Where-Object { $_ -match '^\s*#' -or $_ -notmatch $rankerRe })
    [System.IO.File]::WriteAllLines($tmpReqs, $kept, (New-Object System.Text.UTF8Encoding($false)))

    try {
        Write-Info "Installing Python packages (this may take several minutes)..."
        Invoke-Native "uv pip install requirements" {
            & $UvExe pip install --python "$VenvDir\Scripts\python.exe" -r $tmpReqs --quiet
        }
        Invoke-Native "uv pip install app" {
            & $UvExe pip install --python "$VenvDir\Scripts\python.exe" --no-deps $RepoDir --quiet
        }
        # Learned-reranker serving library (zero-dependency, installed --no-deps). The venv
        # is reused across upgrades, so ALWAYS clear any prior msa-ranker first, then
        # (re)install from the configured source if one is present. Uninstall-first keeps the
        # venv's ranker state matching the requirements in every case - a deactivated pin, or
        # a pin whose PEP 508 marker is false here (uv installs nothing) - both end heuristic
        # (INV-9; app.py logs whenever msa_ranker imports). No-op on a fresh venv (uv exits 0
        # when the package is absent).
        #
        # uv prints "Using Python ... environment at:" to stderr; under the script-wide
        # $ErrorActionPreference='Stop', PowerShell 5.1 promotes ANY native-command stderr
        # write to a terminating NativeCommandError -> exit 1, and `*> $null` does not
        # suppress it (it redirects the text but the terminating error still fires). This is
        # a best-effort no-op, so drop to 'Continue' around it (the bash twin in install.sh
        # achieves the same with `|| true`). --quiet also keeps the banner off stdout.
        $prevEAP = $ErrorActionPreference
        $ErrorActionPreference = 'Continue'
        try {
            & $UvExe pip uninstall --python "$VenvDir\Scripts\python.exe" --quiet msa-ranker *> $null
        } finally {
            $ErrorActionPreference = $prevEAP
        }
        $rankerWheel = Get-ChildItem (Join-Path $RepoDir "wheels") -Filter "msa_ranker-*.whl" -ErrorAction SilentlyContinue | Select-Object -First 1
        if ($rankerWheel) {
            # Offline path: the vendored wheel ships only with private bundles.
            Invoke-Native "uv pip install msa_ranker" {
                & $UvExe pip install --python "$VenvDir\Scripts\python.exe" --no-deps $rankerWheel.FullName --quiet
            }
            Write-Ok "Learned reranker installed ($($rankerWheel.Name))"
        } elseif ($rankerPin) {
            # Online path (the public mirror ships no vendored wheel): install whichever single
            # msa-ranker spec is uncommented in the requirements - a PyPI ==pin, a GitHub
            # release-asset URL, or a git+ ref. ADR-011 keeps that version == the wheel's. If
            # the spec carries a marker that is false here, uv installs nothing -> stays
            # heuristic (the uninstall above already cleared any stale copy).
            Invoke-Native "uv pip install msa_ranker (pin)" {
                & $UvExe pip install --python "$VenvDir\Scripts\python.exe" --no-deps "$rankerPin" --quiet
            }
            Write-Ok "Learned reranker installed from requirements pin ($rankerPin)"
        }
        Write-Ok "Python packages installed"
    } finally {
        Remove-Item $tmpReqs -ErrorAction SilentlyContinue
    }
}

function Install-FacenetPytorch($UvExe) {
    # Install facenet-pytorch with --no-deps after Install-Torch and Install-AppRuntime
    # have placed the Blackwell-compatible CUDA torch wheel in the venv. Without
    # --no-deps, the resolver pulls facenet-pytorch's transitive torch/torchvision
    # constraints and can replace the sm_120-supporting torch build with a wheel
    # that does not, breaking GPU inference on RTX 5000-series cards.
    Write-Info "Installing facenet-pytorch (face recognition)..."
    Invoke-Native "uv pip install facenet-pytorch" {
        & $UvExe pip install --python "$VenvDir\Scripts\python.exe" `
            "facenet-pytorch>=2.6.0" --no-deps --quiet
    }
    Write-Ok "facenet-pytorch installed"
}

# -- Config -------------------------------------------------------------------

function Initialize-Config {
    if (-not (Test-Path $DataDir)) {
        New-Item -ItemType Directory -Path $DataDir -Force | Out-Null
    }
    if (Test-Path $ConfigPath) {
        Write-Skip "config.yaml already exists at $ConfigPath"
        return
    }
    $template = $script:BundleConfigTemplate
    if ($template -and (Test-Path $template)) {
        Copy-Item $template $ConfigPath
        Write-Ok "config.yaml created at $ConfigPath"
    } else {
        Write-Fail "Config template missing - cannot create $ConfigPath. Re-run the installer."
    }
}

# -- Launcher (added to user PATH, no elevation) ------------------------------

function Install-Launcher($UvExe) {
    if (-not (Test-Path $LauncherDir)) {
        New-Item -ItemType Directory -Path $LauncherDir -Force | Out-Null
    }

    # Wrapper .cmd so `msa` works from any shell without activating the venv.
    # MSA_CONFIG_PATH is exported explicitly so the launcher self-documents the
    # active config file (visible in `set` output) and so installs that passed a
    # non-default -DataDir at install time still resolve to the right config
    # path. For default installs this matches the runtime platform default per
    # ADR-009 section 6 (%USERPROFILE%\MediaSearchAgent\config.yaml).
    # Use set "VAR=value" quoting so paths containing spaces are handled correctly.
    # Written as OEM (system code page) so cmd.exe interprets it without a BOM.
    $launcherContent = @"
@echo off
set "PATH=$LauncherDir;%PATH%"
set "MSA_ROOT=$RepoDir"
set "MSA_APP_DIR=$AppDir"
set "MSA_DATA_DIR=$DataDir"
set "MSA_CACHE_DIR=$CacheDir"
set "MSA_LOG_DIR=$LogDir"
set "MSA_CONFIG_PATH=$ConfigPath"
set "MSA_VENV_DIR=$VenvDir"
if /i "%~1"=="uninstall" (
    powershell -ExecutionPolicy Bypass -File "$RepoDir\uninstall.ps1" -AppDir "$AppDir" -DataDir "$DataDir"
    exit /b %ERRORLEVEL%
)
if /i "%~1"=="tray" (
    start "" "$TrayExe"
    exit /b 0
)
"$VenvDir\Scripts\msa.exe" %*
"@
    [System.IO.File]::WriteAllText($Launcher, $launcherContent, [System.Text.Encoding]::GetEncoding(850))

    $userPath = [Environment]::GetEnvironmentVariable("Path", "User")
    if ($userPath -notlike "*$LauncherDir*") {
        [Environment]::SetEnvironmentVariable("Path", "$userPath;$LauncherDir", "User")
        $env:Path = "$env:Path;$LauncherDir"
        Write-Ok "Added $LauncherDir to user PATH"
    } else {
        Write-Skip "Launcher directory already on PATH"
    }
    Write-Ok "Launcher installed at $Launcher"
}

function New-ShortcutFile {
    param(
        [string] $ShortcutPath,
        [string] $TargetPath,
        [string] $Arguments,
        [string] $WorkingDirectory
    )

    $shell = New-Object -ComObject WScript.Shell
    $shortcut = $shell.CreateShortcut($ShortcutPath)
    $shortcut.TargetPath = $TargetPath
    $shortcut.Arguments = $Arguments
    $shortcut.WorkingDirectory = $WorkingDirectory
    $shortcut.Save()
}

function Install-TrayApp {
    if (-not (Test-Path $LauncherDir)) {
        New-Item -ItemType Directory -Path $LauncherDir -Force | Out-Null
    }

    $src = $script:BundleTrayExe
    if (-not (Test-Path $src)) {
        Write-Fail "Tray exe not found in bundle at $src"
    }
    Copy-Item $src $TrayExe -Force

    # Sidecar: read by the tray exe at startup so it knows the correct DataDir,
    # ConfigPath, and LogDir when launched via a shortcut or Task Scheduler
    # (i.e. not via msa.cmd which sets env vars).
    $sidecarContent = @"
MSA_DATA_DIR=$DataDir
MSA_CONFIG_PATH=$ConfigPath
MSA_LOG_DIR=$LogDir
MSA_CACHE_DIR=$CacheDir
"@
    [System.IO.File]::WriteAllText($TrayPathsEnv, $sidecarContent,
        [System.Text.Encoding]::UTF8)

    Write-Ok "Tray app installed at $TrayExe"
}

function Install-StartMenuShortcuts {
    if (-not (Test-Path $StartMenuDir)) {
        New-Item -ItemType Directory -Path $StartMenuDir -Force | Out-Null
    }

    $powerShellExe = "$env:SystemRoot\System32\WindowsPowerShell\v1.0\powershell.exe"
    # "Media Search Agent" launches the tray app (GUI exe - no console window).
    # If the tray is already running the single-instance mutex prevents a second copy.
    $startShortcut = Join-Path $StartMenuDir "Media Search Agent.lnk"
    $stopShortcut  = Join-Path $StartMenuDir "Stop Media Search Agent.lnk"

    New-ShortcutFile `
        -ShortcutPath $startShortcut `
        -TargetPath $TrayExe `
        -Arguments "" `
        -WorkingDirectory $AppDir

    New-ShortcutFile `
        -ShortcutPath $stopShortcut `
        -TargetPath $powerShellExe `
        -Arguments "-NonInteractive -ExecutionPolicy Bypass -WindowStyle Hidden -File `"$StopScript`" -AppDir `"$AppDir`" -DataDir `"$DataDir`"" `
        -WorkingDirectory $AppDir

    Write-Ok "Start Menu shortcuts installed"
}

# -- Auto-start (Task Scheduler preferred, no elevation) ----------------------

function Get-AutoStartCommand {
    return "`"$TrayExe`""
}

function Install-RunKeyAutoStart {
    param(
        [string] $Name,
        [string] $Command
    )

    $runKeyPath = "HKCU:\Software\Microsoft\Windows\CurrentVersion\Run"
    New-Item -Path $runKeyPath -Force | Out-Null
    New-ItemProperty `
        -Path $runKeyPath `
        -Name $Name `
        -PropertyType String `
        -Value $Command `
        -Force | Out-Null

    Write-Ok "Auto-start enabled via HKCU Run registry key"
}

function Install-TaskScheduler {
    if ($SkipAutoStart) { return }

    $taskName = "MediaSearchAgent"
    $command = Get-AutoStartCommand
    # Always recreate so paths stay in sync when -AppDir or -DataDir changes on reinstall.
    try {
        if (Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue) {
            Write-Info "Updating existing auto-start task..."
            Unregister-ScheduledTask -TaskName $taskName -Confirm:$false -ErrorAction Stop
        }
    } catch {
        Write-Warn "Could not remove existing auto-start task; will try to continue: $($_.Exception.Message)"
    }

    # Launch the tray exe directly - it reads paths from its msa-paths.env sidecar,
    # starts the API if not already running, and opens the browser.
    $action   = New-ScheduledTaskAction `
        -Execute $TrayExe `
        -WorkingDirectory $AppDir
    $trigger  = New-ScheduledTaskTrigger -AtLogOn
    $settings = New-ScheduledTaskSettingsSet -ExecutionTimeLimit 0 -MultipleInstances IgnoreNew

    try {
        Register-ScheduledTask `
            -TaskName $taskName `
            -Action $action `
            -Trigger $trigger `
            -Settings $settings `
            -RunLevel Limited `
            -Force `
            -ErrorAction Stop | Out-Null

        Write-Ok "Auto-start task registered (runs at login, no elevation)"
        # Remove any leftover Run-key fallback from a previous install so only one
        # auto-start mechanism is active.
        $runKeyPath = "HKCU:\Software\Microsoft\Windows\CurrentVersion\Run"
        if (Get-ItemProperty -Path $runKeyPath -Name $taskName -ErrorAction SilentlyContinue) {
            Remove-ItemProperty -Path $runKeyPath -Name $taskName -ErrorAction SilentlyContinue
            Write-Ok "Removed stale Run-key fallback auto-start"
        }
    } catch {
        # Task Scheduler registration can fail on locked-down VMs, Group
        # Policy-restricted boxes, or images where the Schedule service is
        # in a degraded state. The HKCU Run-key fallback below is
        # functionally equivalent for our launch-tray-at-login use case,
        # so we suppress the verbose error message and just announce the
        # fallback. The underlying $_.Exception.Message is intentionally
        # dropped - if you need it for diagnosis, re-run with the older
        # bundle or read the system Task Scheduler event log.
        Write-Info "Falling back to per-user Run registry auto-start..."
        Install-RunKeyAutoStart -Name $taskName -Command $command
    }
}

$script:TranscriptStarted = $false
$script:InstallerLog = $null
$script:InstallerMode = ""
$script:InstallModeReason = ""
$script:InstallModeMarkers = "none"
$script:BundleTrayExe = $null
$installMode = Get-InstallMode

try {
    Initialize-Logging $installMode

    Write-Banner

    # Pre-flight: fail fast on unsupported architecture. Runs after the
    # banner so the user sees what they were trying to install before the
    # error.
    Test-WindowsArchitecture
    Test-SystemRequirements

    $tag = Resolve-MsaVersion
    if ($Bundle) {
        Write-Info "Bundle:  $Bundle"
        if ($tag -ne "(local bundle)") { Write-Info "Version: $tag" }
    } else {
        Write-Info "Version: $tag"
    }

    # Downgrade guard - runs whenever an existing install is detected,
    # whether the markers are complete (upgrade) or partial (repair).
    # Repair-mode installs (1-2 markers) can still have a valid version.txt
    # from a previous successful install; downgrading over partial state
    # still risks SQLite schema corruption if the schema moved forward
    # between versions. Test-VersionDowngrade itself is a silent no-op
    # when version.txt is absent, so fresh installs aren't affected.
    if ($installMode -in @("upgrade", "repair")) {
        Test-VersionDowngrade $tag
    }

    # Derive uv path from AppDir (set during bundle extract)
    $UvExe = "$AppDir\uv\uv.exe"

    if ($installMode -in @("upgrade", "repair")) {
        Write-Step "[0/5] Stopping running services (upgrade)"
        Stop-RunningServices
    }

    Write-Step "[1/5] Bundle"
    Install-Bundle $tag

    Write-Step "[2/5] Python environment"
    Initialize-Python $UvExe
    Install-Torch $UvExe
    Install-AppRuntime $UvExe
    Install-FacenetPytorch $UvExe

    Write-Step "[3/5] Configuration"
    Initialize-Config

    Write-Step "[4/5] Launcher + tray"
    Install-Launcher $UvExe
    Install-TrayApp
    Clear-BundleTempDir
    Install-StartMenuShortcuts

    Write-Step "[5/5] Auto-start"
    Install-TaskScheduler

    # Record the installed version so the next install can guard against
    # downgrades. Written only on success - if anything above failed, the
    # previous version marker stays in place.
    if ($tag -ne "(local bundle)") {
        Set-Content -Path $VersionFile -Value $tag -Encoding UTF8 -Force
    }

    # Two-stage end output (matches install.sh on macOS):
    #   1. "installed" success line (install steps complete)
    #   2. Launch the tray + poll /health with live dots ("Starting the
    #      app........") until it responds
    #   3. "started" success line + browser-opening hint
    # Bridges the silent gap between install completing and the browser
    # actually opening - the tray launches the API in the background and
    # opens the browser only when /health responds, which can be 10-30 s
    # on a fresh first launch. Without the poll, the installer would
    # exit and the user would stare at a returned shell prompt with no
    # idea what was happening.
    Write-Host ""
    Write-Ok "Media Search Agent installed!"
    Write-Host ""

    if ((-not $SkipLaunch) -and (Test-Path $TrayExe)) {
        Start-Process -FilePath $TrayExe -WorkingDirectory $AppDir
        # The tray polls /health itself and opens the browser when ready;
        # our parallel poll just bridges the visible gap so the user has
        # something to watch. Wait-ApiReady never calls Start-Process for
        # the URL itself, so there's no double-tab race between the two
        # polls - the tray is the sole opener.
        Wait-ApiReady -TimeoutSec 90 -Port (Get-ConfiguredApiPort) | Out-Null
    } elseif ($SkipLaunch) {
        Write-Skip "Tray launch skipped"
    }
} finally {
    Stop-InstallerLogging
}
