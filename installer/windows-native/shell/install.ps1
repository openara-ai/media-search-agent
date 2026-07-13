#Requires -Version 5.1
<#
.SYNOPSIS
    Media Search Agent - thin one-line Windows bootstrap (M-7/S-3).

.DESCRIPTION
    Fetches the per-user Tauri desktop installer (setup.exe) from GitHub Releases,
    verifies its SHA-256 against SHA256SUMS.txt (HARD FAIL on mismatch), unblocks the
    downloaded file (clears the Mark-of-the-Web SmartScreen zone), runs it silently
    (/S, current-user, no UAC), and launches the app. Nothing heavy happens here - the
    ~2 GB Python/ML provisioning is done by the app itself on first launch.

    One-liner:
      powershell -c "irm https://github.com/openara-ai/media-search-agent/releases/latest/download/install.ps1 | iex"

    With parameters (scriptblock form when piped):
      powershell -c "& ([scriptblock]::Create((irm https://github.com/openara-ai/media-search-agent/releases/latest/download/install.ps1))) -Version v0.4.0"

    Headless (no GUI session - SSH / CI): provisions the installed bundle inline (the same
    uv -> CPython -> venv -> ensure_dependencies a GUI first run would run), installs the
    `msa` CLI launcher, and leaves the box ready for `msa api start` (browser mode):
      powershell -c "& ([scriptblock]::Create((irm .../install.ps1))) -Headless"

.PARAMETER Version
    Tagged release to install (e.g. v0.4.0). Default: latest published GitHub release.
    Also settable via env: $env:MSA_VERSION

.PARAMETER Setup
    Path to a pre-downloaded setup.exe. Skips the GitHub download + SHA-256 verify
    (the caller is responsible for trusting the file). Useful for local testing.

.PARAMETER Headless
    Provision inline and install the `msa` CLI instead of launching the GUI. Leaves the
    machine ready for `msa api start` (FastAPI serves the built SPA at the config port).

.PARAMETER SkipLaunch
    Skip launching the app after a GUI install.

.PARAMETER Help
    Show this help and exit.

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File install.ps1
.EXAMPLE
    powershell -ExecutionPolicy Bypass -File install.ps1 -Version v0.4.0
.EXAMPLE
    powershell -ExecutionPolicy Bypass -File install.ps1 -Headless
#>
[Diagnostics.CodeAnalysis.SuppressMessageAttribute('PSAvoidUsingWriteHost', '',
    Justification = 'Interactive bootstrap - coloured console output is intentional.')]
[Diagnostics.CodeAnalysis.SuppressMessageAttribute('PSReviewUnusedParameter', 'Version',
    Justification = 'Used indirectly by Resolve-MsaVersion; PSScriptAnalyzer does not follow that flow.')]
[Diagnostics.CodeAnalysis.SuppressMessageAttribute('PSReviewUnusedParameter', 'Setup',
    Justification = 'Used inside Get-SetupExe (local pre-downloaded setup.exe path); PSScriptAnalyzer does not follow script-param -> nested-function usage.')]
[Diagnostics.CodeAnalysis.SuppressMessageAttribute('PSUseShouldProcessForStateChangingFunctions', '',
    Justification = 'Internal bootstrap helpers only mutate installer-owned temp + install state.')]
[Diagnostics.CodeAnalysis.SuppressMessageAttribute('PSUseSingularNouns', '',
    Justification = 'Helper names prioritise readability and match multi-target side effects.')]
[CmdletBinding()]
param(
    [string] $Version    = "",
    [string] $Setup      = "",
    [switch] $Headless,
    [switch] $SkipLaunch,
    [switch] $Help
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$GithubRepo    = "openara-ai/media-search-agent"
$ProductName   = "MediaSearchAgent"
$AppId         = "ai.openara.mediasearchagent"
$PythonVersion = "3.12"
# Where the per-user Tauri NSIS installer places the app (productName, currentUser mode).
$InstallDir    = Join-Path $env:LOCALAPPDATA $ProductName
# The identifier-keyed app-private runtime dir the supervisor provisions the venv into.
$AppPrivateDir = Join-Path $env:LOCALAPPDATA $AppId

# -- Logging ------------------------------------------------------------------

function Write-Ok($msg)   { Write-Host "  + $msg" -ForegroundColor Green }
function Write-Info($msg) { Write-Host "  * $msg" -ForegroundColor Gray }
function Write-Skip($msg) { Write-Host "  - $msg" -ForegroundColor DarkGray }
function Write-Warn($msg) { Write-Host "  ! $msg" -ForegroundColor Yellow }
function Write-Fail($msg) { Write-Host "  x $msg" -ForegroundColor Red; exit 1 }
function Write-Step($msg) { Write-Host "`n$msg" -ForegroundColor White }

function Write-Banner {
    Write-Host ""
    Write-Host "  Media Search Agent Installer" -ForegroundColor Cyan
    Write-Host "  Local-first semantic search for your photos and videos" -ForegroundColor Gray
    Write-Host ""
    Write-Info "Scope:   current user only (other Windows accounts need their own install)"
    Write-Host ""
}

if ($Help) {
    Write-Banner
    Write-Host "USAGE" -ForegroundColor White
    Write-Host '  powershell -c "irm https://github.com/openara-ai/media-search-agent/releases/latest/download/install.ps1 | iex"'
    Write-Host ""
    Write-Host "OPTIONS" -ForegroundColor White
    Write-Host "  -Version <tag>   Release tag (e.g. v0.4.0). Default: latest. Env: MSA_VERSION"
    Write-Host "  -Setup <path>    Install a pre-downloaded setup.exe (skips download + verify)"
    Write-Host "  -Headless        Provision inline and install the msa CLI; do not launch the GUI"
    Write-Host "  -SkipLaunch      Do not launch the app after install"
    Write-Host "  -Help            Show this help and exit"
    Write-Host ""
    exit 0
}

# -- Architecture pre-flight (x86_64 only) ------------------------------------

function Test-WindowsArchitecture {
    $arch = if ($env:PROCESSOR_ARCHITEW6432) { $env:PROCESSOR_ARCHITEW6432 } else { $env:PROCESSOR_ARCHITECTURE }
    $is64 = [Environment]::Is64BitOperatingSystem
    if (-not $is64 -or ($arch -notmatch '^(AMD64|x86_64)$')) {
        Write-Fail "Media Search Agent currently supports Windows x86_64 only. Detected arch=$arch, Is64BitOS=$is64. ARM64 Windows is not yet supported - check https://github.com/$GithubRepo/releases for updates."
    }
}

# -- Version resolution -------------------------------------------------------

function Resolve-MsaVersion {
    if ($Version)        { return $Version }
    if ($env:MSA_VERSION) { return $env:MSA_VERSION }
    if ($Setup)          { return "(local setup)" }
    $releasesUrl = "https://api.github.com/repos/$GithubRepo/releases/latest"
    Write-Info "Resolving latest version from GitHub..."
    Write-Info "  from $releasesUrl"
    try {
        return (Invoke-RestMethod $releasesUrl).tag_name
    } catch {
        Write-Fail "Could not resolve latest version from GitHub. Use -Version v0.4.0 to specify one."
    }
}

# -- Download + SHA-256 verify ------------------------------------------------

function Get-FileFromUrl($url, $dest, $desc) {
    Write-Info "Downloading $desc..."
    Write-Info "  from $url"
    $dir = Split-Path $dest -Parent
    if (-not (Test-Path $dir)) { New-Item -ItemType Directory -Path $dir -Force | Out-Null }
    Invoke-WebRequest -Uri $url -OutFile $dest -UseBasicParsing
}

function Test-SetupSha256 {
    # Verify the downloaded setup.exe against SHA256SUMS.txt published alongside the release.
    # HARD FAIL on mismatch OR on ANY fetch failure, including HTTP 404: a release that ships no
    # SHA256SUMS.txt is refused, not trusted (the 404 branch below Write-Fails too). An unverified
    # unsigned installer is exactly the supply-chain hole this closes.
    param(
        [string] $SetupFile,
        [string] $SetupName,
        [string] $ReleaseBaseUrl
    )
    $sumsUrl = "$ReleaseBaseUrl/SHA256SUMS.txt"
    Write-Info "Verifying installer integrity..."
    Write-Info "  from $sumsUrl"
    $sumsText = $null
    try {
        $sumsText = Invoke-WebRequest -Uri $sumsUrl -UseBasicParsing -ErrorAction Stop |
            Select-Object -ExpandProperty Content
    } catch {
        $status = $null
        try { if ($_.Exception.Response) { $status = [int]$_.Exception.Response.StatusCode } } catch { $status = $null }
        if ($status -eq 404) {
            Write-Fail "SHA256SUMS.txt not found (HTTP 404) at $sumsUrl. Refusing to install an unverified installer - pass -Setup <local-path> for a copy you trust, or install a release that ships checksums."
        }
        $statusDesc = if ($status) { "HTTP $status" } else { "transport failure" }
        Write-Fail "Could not fetch SHA256SUMS.txt from $sumsUrl ($statusDesc): $($_.Exception.Message). Refusing to install an unverified installer - retry once the network is healthy, or pass -Setup <local-path>."
    }

    $expected = $null
    foreach ($line in $sumsText -split "`n") {
        $line = $line.Trim()
        if (-not $line -or $line.StartsWith("#")) { continue }
        $parts = $line -split '\s+', 2
        if ($parts.Length -lt 2) { continue }
        $name = $parts[1].TrimStart('*', '.', '/').Trim()
        if ($name -eq $SetupName) { $expected = $parts[0].ToLower(); break }
    }
    if (-not $expected) {
        Write-Fail "$SetupName not listed in SHA256SUMS.txt. Refusing to install an installer with no published checksum."
    }
    $actual = (Get-FileHash $SetupFile -Algorithm SHA256).Hash.ToLower()
    if ($actual -ne $expected) {
        Write-Fail "Installer SHA256 mismatch for ${SetupName}: expected $expected, got $actual. The download may be corrupted or tampered with - aborting before running it."
    }
    Write-Ok "Installer SHA256 verified ($($expected.Substring(0,12))...)"
}

function Get-SetupExe($tag) {
    # Returns the local path to a verified, unblocked setup.exe.
    if ($Setup) {
        if (-not (Test-Path $Setup)) { Write-Fail "Setup file not found: $Setup" }
        Write-Info "Using local setup: $Setup"
        Unblock-File -Path $Setup -ErrorAction SilentlyContinue
        return (Resolve-Path $Setup).Path
    }
    $bare      = $tag -replace '^v', ''
    $setupName = "${ProductName}_${bare}_x64-setup.exe"
    $baseUrl   = "https://github.com/$GithubRepo/releases/download/$tag"
    $setupUrl  = "$baseUrl/$setupName"
    $dest      = Join-Path $env:TEMP $setupName

    Get-FileFromUrl $setupUrl $dest "installer $tag"
    Test-SetupSha256 $dest $setupName $baseUrl
    # Clear the Mark-of-the-Web zone so SmartScreen does not block the silent run.
    Unblock-File -Path $dest -ErrorAction SilentlyContinue
    return $dest
}

# -- Silent install -----------------------------------------------------------

function Invoke-SilentInstall($setupExe) {
    Write-Info "Running the installer silently (/S, current-user, no UAC)..."
    $p = Start-Process -FilePath $setupExe -ArgumentList "/S" -PassThru -Wait
    if ($p.ExitCode -ne 0) {
        Write-Fail "Installer exited with code $($p.ExitCode). See %LOCALAPPDATA%\$ProductName for a partial install."
    }
    Write-Ok "Installed to $InstallDir"
}

# -- Headless provisioning (no GUI) -------------------------------------------

function Invoke-HeadlessProvision {
    # Provision the installed bundle inline so a headless box ends in the same state a GUI first run
    # would: mirror the supervisor (extract uv -> uv python install -> uv venv into the app-private
    # dir with the SAME UV_* pins), then run the shim's `python -m app.provision` for the pip
    # installs + config + version stamp. All path logic lives in the shim; this only locates the
    # installed bundle. Finally install the `msa` CLI launcher and print the start instructions.
    $stagedUv = Join-Path $InstallDir "bin\uv.exe"
    $backend  = Join-Path $InstallDir "backend"
    if (-not (Test-Path $stagedUv)) { Write-Fail "Bundled uv not found at $stagedUv - is the app installed?" }
    if (-not (Test-Path (Join-Path $backend "app"))) { Write-Fail "Shim not found at $backend\app - is the app installed?" }

    $uvForRuntime = Join-Path $AppPrivateDir "bin\uv.exe"
    New-Item -ItemType Directory -Path (Split-Path $uvForRuntime -Parent) -Force | Out-Null
    Copy-Item $stagedUv $uvForRuntime -Force   # the shim's ensure_dependencies looks for <root>\bin\uv.exe

    $venvDir    = Join-Path $AppPrivateDir ".venv"
    $venvPython = Join-Path $venvDir "Scripts\python.exe"

    # Same UV_* app-private pins the vendored supervisor uses (run_uv in main.rs).
    $env:UV_CACHE_DIR         = Join-Path $AppPrivateDir "uv-cache"
    $env:UV_PYTHON_INSTALL_DIR = Join-Path $AppPrivateDir "python"
    $env:UV_NO_CONFIG          = "1"
    $env:UV_PYTHON_INSTALL_BIN = "0"

    Write-Info "Installing CPython $PythonVersion (app-private)..."
    & $uvForRuntime python install $PythonVersion
    if ($LASTEXITCODE -ne 0) { Write-Fail "uv python install failed ($LASTEXITCODE)" }
    if (-not (Test-Path $venvPython)) {
        Write-Info "Creating the app-private venv..."
        & $uvForRuntime venv $venvDir --python $PythonVersion
        if ($LASTEXITCODE -ne 0) { Write-Fail "uv venv failed ($LASTEXITCODE)" }
    }

    Write-Step "Provisioning dependencies (one-time; ~2 GB on NVIDIA systems)"
    $env:PYTHONPATH = $backend
    & $venvPython -m app.provision
    if ($LASTEXITCODE -ne 0) { Write-Fail "Provisioning failed ($LASTEXITCODE). See %LOCALAPPDATA%\$ProductName\logs." }

    Install-MsaLauncher $venvDir
    Write-Host ""
    Write-Ok "Media Search Agent provisioned (headless)."
    Write-Host "  Start the backend + SPA (browser mode) with:" -ForegroundColor DarkGray
    Write-Host "    msa api start" -ForegroundColor White
    Write-Host ""
}

function Install-MsaLauncher($venvDir) {
    # `msa` CLI launcher targeting the app-private venv (S-5 item 3, pulled forward for headless).
    $launcherDir = Join-Path $AppPrivateDir "bin"
    $launcher    = Join-Path $launcherDir "msa.cmd"
    New-Item -ItemType Directory -Path $launcherDir -Force | Out-Null
    # `msa uninstall` delegates to the Tauri NSIS uninstaller (silent); everything else runs the
    # app-private venv msa. The uninstaller removes the app-private runtime via the POSTUNINSTALL hook.
    $content = @"
@echo off
if /i "%~1"=="uninstall" (
    if exist "$InstallDir\uninstall.exe" (
        "$InstallDir\uninstall.exe" /S
    ) else (
        echo Uninstall Media Search Agent via Settings ^> Apps ^> MediaSearchAgent.
    )
    exit /b %ERRORLEVEL%
)
"$venvDir\Scripts\msa.exe" %*
"@
    [System.IO.File]::WriteAllText($launcher, $content, [System.Text.Encoding]::GetEncoding(850))

    # Append $launcherDir to the persistent user PATH, but only if it is not already present.
    # Compare entry-by-entry (split on ';', case-insensitive, trailing '\' trimmed) rather than a
    # whole-PATH wildcard substring test: a substring match can false-positive on an unrelated
    # entry (e.g. `...\bin2` contains `...\bin`) and skip a needed install. Also handle an empty /
    # unset user PATH so we never write a leading ';' (which yields a bogus empty PATH entry).
    $userPath = [Environment]::GetEnvironmentVariable("Path", "User")
    $target   = $launcherDir.TrimEnd('\')
    $present  = $false
    if ($userPath) {
        foreach ($entry in ($userPath -split ';')) {
            $trimmed = $entry.Trim()
            if ($trimmed -and ($trimmed.TrimEnd('\') -ieq $target)) { $present = $true; break }
        }
    }
    if (-not $present) {
        $newUserPath = if ($userPath) { "$userPath;$launcherDir" } else { $launcherDir }
        [Environment]::SetEnvironmentVariable("Path", $newUserPath, "User")
        $env:Path = if ($env:Path) { "$env:Path;$launcherDir" } else { $launcherDir }
        Write-Ok "Installed msa launcher and added it to your PATH"
    } else {
        Write-Ok "Installed msa launcher"
    }
}

# -- Main ---------------------------------------------------------------------

Write-Banner
Test-WindowsArchitecture

$tag = Resolve-MsaVersion
if ($tag -ne "(local setup)") { Write-Info "Version: $tag" }

Write-Step "[1/2] Installer"
$setupExe = Get-SetupExe $tag
Invoke-SilentInstall $setupExe

if ($Headless) {
    Write-Step "[2/2] Headless provisioning"
    Invoke-HeadlessProvision
    exit 0
}

Write-Step "[2/2] Launch"
Write-Host ""
Write-Ok "Media Search Agent installed!"
Write-Host ""
if ($SkipLaunch) {
    Write-Skip "Launch skipped (-SkipLaunch). Start it from the Start Menu."
} else {
    $appExe = Join-Path $InstallDir "$ProductName.exe"
    if (Test-Path $appExe) {
        Start-Process -FilePath $appExe
        Write-Ok "Launched. The window opens and finishes first-run setup itself."
    } else {
        Write-Warn "Installed, but $appExe was not found. Launch it from the Start Menu."
    }
}
