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
[Diagnostics.CodeAnalysis.SuppressMessageAttribute('PSUseSingularNouns', '',
    Justification = 'Installer helper names prioritise readability and match multi-target side effects.')]
[Diagnostics.CodeAnalysis.SuppressMessageAttribute('PSUseShouldProcessForStateChangingFunctions', '',
    Justification = 'Internal installer helpers only mutate installer-owned temp directories and install state.')]
[CmdletBinding()]
param(
    [string] $Version      = "",
    [string] $Bundle       = "",
    [string] $AppDir       = "$env:LOCALAPPDATA\MediaSearchAgent",
    [string] $DataDir      = "$env:USERPROFILE\MediaSearchAgent",
    [switch] $SkipAutoStart,
    [switch] $SkipLaunch,
    [switch] $Help
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

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

$PythonVersion = "3.12"
$GithubRepo    = "kumraj/media-search-agent"

# PyTorch CUDA index - cu128 wheels build for Blackwell sm_120 (RTX 5000) and
# remain compatible with Ampere/Ada. Installing torch from this index *before*
# requirements-windows.txt is processed is what keeps GPU inference enabled;
# the default PyPI Windows torch wheel is CPU-only.
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
    Write-Host ""
    Write-Host "  Media Search Agent Installer" -ForegroundColor Cyan
    Write-Host "  Local-first semantic search for your photos and videos" -ForegroundColor Gray
    Write-Host ""
    Write-Info "AppDir:  $AppDir"
    Write-Info "DataDir: $DataDir"
    Write-Host ""
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
    param([string]$Mode)

    if (-not (Test-Path $LogDir)) {
        New-Item -ItemType Directory -Path $LogDir -Force | Out-Null
    }

    $timestamp = Get-Date -Format "yyyy-MM-dd_HHmmss"
    $script:InstallerLog = Join-Path $LogDir "$Mode-$timestamp.log"
    Start-Transcript -Path $script:InstallerLog -Force | Out-Null
    $script:TranscriptStarted = $true

    Write-Info "Log:     $script:InstallerLog"
    Write-Info "Mode:    $Mode"
    Write-Info "Markers: $script:InstallModeMarkers"
    Write-Info "Reason:  $script:InstallModeReason"
}

# -- Download helper ----------------------------------------------------------

function Get-FileFromUrl($url, $dest, $desc) {
    Write-Info "Downloading $desc..."
    $dir = Split-Path $dest -Parent
    if (-not (Test-Path $dir)) { New-Item -ItemType Directory -Path $dir -Force | Out-Null }
    Invoke-WebRequest -Uri $url -OutFile $dest -UseBasicParsing
}

# -- Version resolution -------------------------------------------------------

function Resolve-MsaVersion {
    if ($Version) { return $Version }
    if ($env:MSA_VERSION) { return $env:MSA_VERSION }
    # If a local bundle is supplied the version is only used for display; skip API call.
    if ($Bundle) { return "(local bundle)" }
    try {
        $rel = Invoke-RestMethod "https://api.github.com/repos/$GithubRepo/releases/latest"
        return $rel.tag_name
    } catch {
        Write-Fail "Could not resolve latest version from GitHub. Use -Version v0.2.0 to specify one."
    }
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
        $configPort = 8000
        if (Test-Path $ConfigPath) {
            $inApi = $false
            foreach ($line in Get-Content $ConfigPath -ErrorAction SilentlyContinue) {
                if ($line -match '^[^#]*\bapi\s*:\s*$') { $inApi = $true; continue }
                if ($inApi -and $line -match '^[^ \t]') { break }
                if ($inApi -and $line -match '^\s*port\s*:\s*([0-9]+)') {
                    $configPort = [int]$Matches[1]; break
                }
            }
        }
        $conn = Get-NetTCPConnection -LocalPort $configPort -State Listen `
            -ErrorAction SilentlyContinue | Select-Object -First 1
        if ($conn) {
            $proc = Get-Process -Id $conn.OwningProcess -ErrorAction SilentlyContinue
            if ($proc) {
                $procPath = try { $proc.Path } catch { $null }
                if ($procPath -like "$AppDir\*") {
                    Stop-Process -Id $proc.Id -Force -ErrorAction SilentlyContinue
                    Write-Ok "Force-killed $($proc.Name) (PID $($proc.Id)) on port $configPort"
                } else {
                    Write-Warn "Port $configPort is held by $($proc.Name) (PID $($proc.Id)) outside $AppDir - skipping"
                }
            }
        }
    } catch {
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
        Write-Info "Extracting bundle..."
        Expand-Archive -Path $Bundle -DestinationPath $tmpExtract -Force
    } else {
        $bare = $tag -replace '^v', ''
        $bundleName = "MediaSearchAgent-${bare}-windows-x86_64"
        $bundleUrl  = "https://github.com/$GithubRepo/releases/download/$tag/${bundleName}.zip"

        $bundleZip = "$env:TEMP\msa-bundle.zip"
        Get-FileFromUrl $bundleUrl $bundleZip "bundle $tag"
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
                        "requirements-windows.txt", "LICENSE", "NOTICE", "uninstall.ps1")) {
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

function Install-Torch($UvExe) {
    # Install torch + torchvision from the CUDA index BEFORE requirements-windows.txt
    # is processed. The default PyPI Windows torch wheel is CPU-only, so without
    # this step, transitive torch deps in requirements (open_clip_torch, transformers,
    # etc.) pull the CPU build and torch.cuda.is_available() returns False on
    # CUDA-capable machines. cu128 wheels support Blackwell sm_120 and remain
    # compatible with Ampere/Ada.
    Write-Info "Installing PyTorch + torchvision ($TorchIndexUrl)..."
    Write-Host "    Downloading from $TorchIndexUrl - this may take several minutes (~2 GB)." -ForegroundColor DarkGray
    Invoke-Native "uv pip install torch torchvision" {
        & $UvExe pip install --python "$VenvDir\Scripts\python.exe" `
            torch torchvision --index-url $TorchIndexUrl --quiet
    }
    Write-Ok "PyTorch installed"

    # Non-fatal CUDA smoke check. A False here is expected on CPU-only machines
    # and just means GPU inference will fall back to CPU at runtime.
    $prevEAP = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    $venvPython = "$VenvDir\Scripts\python.exe"
    $cudaOk = & $venvPython -c 'import torch; print(torch.cuda.is_available())' 2>&1
    if ($cudaOk -match "True") {
        $gpuName = & $venvPython -c 'import torch; print(torch.cuda.get_device_name(0))' 2>&1
        Write-Ok "CUDA available: $gpuName"
    } else {
        Write-Warn "CUDA not available - GPU inference will fall back to CPU. Check your NVIDIA driver if this machine has a GPU."
    }
    $ErrorActionPreference = $prevEAP
}

function Install-AppRuntime($UvExe) {
    $reqs = "$RepoDir\requirements-windows.txt"
    if (-not (Test-Path $reqs)) { $reqs = "$RepoDir\requirements.txt" }

    Write-Info "Installing Python packages (this may take several minutes)..."
    Invoke-Native "uv pip install requirements" {
        & $UvExe pip install --python "$VenvDir\Scripts\python.exe" -r $reqs --quiet
    }
    Invoke-Native "uv pip install app" {
        & $UvExe pip install --python "$VenvDir\Scripts\python.exe" --no-deps $RepoDir --quiet
    }
    Write-Ok "Python packages installed"
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
        Write-Warn "Task Scheduler registration failed: $($_.Exception.Message)"
        Write-Info "Falling back to per-user Run registry auto-start..."
        Install-RunKeyAutoStart -Name $taskName -Command $command
    }
}

$script:TranscriptStarted = $false
$script:InstallerLog = $null
$script:InstallModeReason = ""
$script:InstallModeMarkers = "none"
$script:BundleTrayExe = $null
$installMode = Get-InstallMode

try {
    Initialize-Logging $installMode

    Write-Banner

    $tag = Resolve-MsaVersion
    if ($Bundle) {
        Write-Info "Bundle:  $Bundle"
        if ($tag -ne "(local bundle)") { Write-Info "Version: $tag" }
    } else {
        Write-Info "Version: $tag"
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

    # Launch the tray immediately so the user doesn't have to log off and back on.
    # Start-Process detaches it so the installer exits cleanly.
    if ((-not $SkipLaunch) -and (Test-Path $TrayExe)) {
        Start-Process -FilePath $TrayExe -WorkingDirectory $AppDir
        Write-Ok "Tray app launched"
    } elseif ($SkipLaunch) {
        Write-Skip "Tray launch skipped"
    }

    Write-Host ""
    Write-Ok "Media Search Agent installed!"
    Write-Host ""
    Write-Host "  Starting:  The tray icon appears in the system tray at login" -ForegroundColor White
    Write-Host "  Relaunch:  Start Menu > Media Search Agent, or: msa tray" -ForegroundColor White
    Write-Host "  Open:      http://localhost:8000" -ForegroundColor White
    Write-Host "  Config:    $ConfigPath" -ForegroundColor Gray
    Write-Host "  Logs:      $LogDir" -ForegroundColor Gray
    Write-Host "  Uninstall: msa uninstall" -ForegroundColor Gray
    Write-Host ""
    Write-Host "  Edit config.yaml to add your media source paths, then run: msa tray" -ForegroundColor Gray
    Write-Host ""
} finally {
    Stop-InstallerLogging
}
