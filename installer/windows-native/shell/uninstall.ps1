#Requires -Version 5.1
<#
.SYNOPSIS
    Media Search Agent - Shell Installer Uninstaller (Windows)

.DESCRIPTION
    Implements the three-tier uninstall policy (ADR-005).

    Tier 1 - Always (no prompt):
        App code items: repo\src, repo\scripts, repo\bin, repo\pyproject.toml,
        repo\requirements*.txt, repo\LICENSE, repo\NOTICE, repo\uninstall.ps1
        Python venv (.venv\)
        Launcher (msa.cmd + LauncherDir PATH entry removal)
        Task Scheduler auto-start task

    Tier 2 - Prompt user, default KEEP:
        index\  (SQLite + FAISS + Qdrant - can represent hours of indexing)
        config.yaml  (media source paths)
        logs\
        App-private model cache under Cache\models\

    Tier 3 - Never removed:
        ~/.local/bin/uv or AppDir\uv\  (may be used by other projects)
        User media library - never modified by this app

.PARAMETER AppDir
    App internals directory (venv, repo, logs, cache).
    Default: %LOCALAPPDATA%\MediaSearchAgent

.PARAMETER DataDir
    User data directory (index\, config.yaml).
    Default: %USERPROFILE%\MediaSearchAgent

.PARAMETER Unattended
    Skip all Tier 2 prompts and keep user data. Safe for scripted uninstall.
#>
[Diagnostics.CodeAnalysis.SuppressMessageAttribute('PSAvoidUsingWriteHost', '',
    Justification='Interactive uninstall script - colored console output is intentional.')]
[Diagnostics.CodeAnalysis.SuppressMessageAttribute('PSAvoidUsingEmptyCatchBlock', '',
    Justification='Transcript shutdown in Stop-UninstallLogging is best-effort and should not mask the original uninstall result.')]
[Diagnostics.CodeAnalysis.SuppressMessageAttribute('PSUseShouldProcessForStateChangingFunctions', '',
    Justification='Internal uninstall helpers mutate only installer-owned state during scripted cleanup.')]
[CmdletBinding()]
param(
    [string] $AppDir   = "$env:LOCALAPPDATA\MediaSearchAgent",
    [string] $DataDir  = "$env:USERPROFILE\MediaSearchAgent",
    [switch] $Unattended
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Continue'

# Strip trailing backslashes on the user-overridable paths so subsequent
# `-like "$AppDir\*"` checks (the Tier-1 stop-API ownership check) work
# regardless of whether the caller passed `-AppDir C:\MSA` or `C:\MSA\`.
# Without this, the trailing slash produces a `C:\MSA\\*` pattern that
# never matches real process paths and uninstall would skip terminating
# its own API process. Symmetric fix to install.ps1; caught by Codex on PR #132.
#
# Drive-root guard: leave `C:\`, `D:\`, etc. as-is. Trimming would turn
# `C:\` into `C:` and the subsequent `-like "C:\*"` ownership check
# would spuriously match anything on the drive. Caught by Copilot on PR #133.
if ($AppDir -notmatch '^[A-Za-z]:\\$') { $AppDir = $AppDir.TrimEnd('\') }
if ($DataDir -notmatch '^[A-Za-z]:\\$') { $DataDir = $DataDir.TrimEnd('\') }

# -- Logging ------------------------------------------------------------------

function Write-Step { param([string]$Msg) Write-Host "`n==> $Msg" -ForegroundColor Cyan }
function Write-Ok   { param([string]$Msg) Write-Host "    + $Msg" -ForegroundColor Green }
function Write-Skip { param([string]$Msg) Write-Host "    - $Msg" -ForegroundColor DarkGray }
function Write-Warn { param([string]$Msg) Write-Host "    ! $Msg" -ForegroundColor Yellow }
function Write-Fail {
    # Hard abort: refuse to proceed with destructive uninstall steps when the
    # API kill failed. Half-deleting the venv around a live python is what
    # produced the "port held outside AppDir - skipping" deadlock on
    # re-install, so failing fast here is the safe path.
    param([string]$Msg)
    Write-Host "    x $Msg" -ForegroundColor Red
    exit 1
}

function Stop-UninstallLogging {
    if ($script:TranscriptStarted) {
        try {
            Stop-Transcript | Out-Null
        } catch {
            # Best-effort only.
        }
        $script:TranscriptStarted = $false
    }
}

function Initialize-UninstallLogging {
    $timestamp = Get-Date -Format "yyyy-MM-dd_HHmmss"
    $script:UninstallLogBaseName = "uninstall-$timestamp.log"
    $script:UninstallLog = Join-Path $env:TEMP "msa-$($script:UninstallLogBaseName)"
    Start-Transcript -Path $script:UninstallLog -Force | Out-Null
    $script:TranscriptStarted = $true

    Write-Host "    Log (temp): $script:UninstallLog" -ForegroundColor DarkGray
}

function Read-KeepChoice {
    param([string]$Question)
    if ($Unattended) { return $false }
    $ans = Read-Host "  $Question [y/N]"
    return ($ans -match '^[Yy]$')
}

function Get-ConfiguredPort {
    param(
        [string] $Path,
        [int] $DefaultPort
    )

    if (-not (Test-Path $Path)) {
        return $DefaultPort
    }

    try {
        $inApi = $false
        foreach ($line in Get-Content -Path $Path -ErrorAction Stop) {
            if ($line -match '^[^#]*\bapi\s*:\s*$') {
                $inApi = $true
                continue
            }
            if ($inApi -and $line -match '^[^ \t]') {
                break
            }
            if ($inApi -and $line -match '^\s*port\s*:\s*([0-9]+)\s*$') {
                return [int]$Matches[1]
            }
        }
    } catch {
        Write-Warn "Could not parse API port from $Path; defaulting to $DefaultPort"
    }

    return $DefaultPort
}

# -- Banner -------------------------------------------------------------------

Write-Host ""
Write-Host "  Media Search Agent - Uninstaller" -ForegroundColor White
Write-Host "  Shell install layout (ADR-005 removal tiers)" -ForegroundColor DarkGray
Write-Host ""
Write-Host "    App:    $AppDir" -ForegroundColor DarkGray
Write-Host "    Data:   $DataDir" -ForegroundColor DarkGray
Write-Host ""

if (-not $Unattended) {
    $ans = Read-Host "  Uninstall Media Search Agent? [y/N]"
    if ($ans -notmatch '^[Yy]$') {
        Write-Host "  Uninstall cancelled." -ForegroundColor Yellow
        exit 0
    }
}

$RepoDir     = Join-Path $AppDir "repo"
$VenvDir     = Join-Path $AppDir ".venv"
$LogDir      = Join-Path $AppDir "logs"
$LauncherDir = Join-Path $AppDir "bin"
$Launcher    = Join-Path $LauncherDir "msa.cmd"
$TrayExe     = Join-Path $LauncherDir "MediaSearchAgentTray.exe"
$TrayPathsEnv = Join-Path $LauncherDir "msa-paths.env"
$StartScript = Join-Path $AppDir "start.ps1"
$StopScript  = Join-Path $AppDir "stop.ps1"
$StartMenuDir = Join-Path $env:APPDATA "Microsoft\Windows\Start Menu\Programs\Media Search Agent"
$ConfigPath  = Join-Path $DataDir "config.yaml"
$IndexDir    = Join-Path $DataDir "index"
$ModelCacheDir = Join-Path $AppDir "Cache\models"
$taskName    = "MediaSearchAgent"
$runKeyPath  = "HKCU:\Software\Microsoft\Windows\CurrentVersion\Run"
$script:TranscriptStarted = $false
$script:UninstallLog = $null
$script:UninstallLogBaseName = $null
$script:ArchiveUninstallLog = $true

try {
Initialize-UninstallLogging

# -- Tier 1: Stop the running app ---------------------------------------------

Write-Step "Tier 1 - Stopping app"

try {
    $port = Get-ConfiguredPort -Path $ConfigPath -DefaultPort 8000
    $conn = Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction SilentlyContinue |
        Select-Object -First 1
    if ($conn) {
        $proc = Get-Process -Id $conn.OwningProcess -ErrorAction SilentlyContinue
        if ($proc) {
            $procPath = ''
            try {
                $procPath = [string]$proc.Path
            } catch {
                $procPath = ''
            }

            # Kill if EITHER:
            #   (a) the exe path is resolvable AND lives under $AppDir
            #       (strong signal: this is our process); OR
            #   (b) the exe path is unresolvable AND the process name is
            #       python/pythonw/uvicorn (recovery case for half-deleted
            #       venv orphans where $proc.Path comes back null because
            #       the file was removed under a still-running process).
            #
            # The previous code OR'd the name check unconditionally, which
            # would terminate UNRELATED dev python on port 8000 (Django /
            # FastAPI dev / Jupyter on alternate port). Guarding the name
            # fallback behind "$procPath is empty" protects external dev
            # processes whose paths are fully resolvable while keeping the
            # orphan-recovery path intact. `$AppDir\*` (with the backslash)
            # matches "<AppDir>\subdir\..." but not "<AppDir>Neighbor\..." -
            # the bare `$AppDir*` (no backslash) the old code used would
            # spuriously match sibling directories.
            $isMsaApi = ($procPath -and ($procPath -like "$AppDir\*")) -or
                        ((-not $procPath) -and ($proc.ProcessName -match '^(python|pythonw|uvicorn)(\.exe)?$'))
            if (-not $isMsaApi) {
                Write-Warn "Refusing to stop unexpected process on port ${port}: $($proc.Name) ($procPath)"
            } else {
                # Nested try/catch so an unexpected exception during kill
                # verification (e.g. WaitForExit throwing) is FATAL via
                # Write-Fail rather than downgraded to Write-Warn by the
                # outer catch. The outer catch is meant for environmental
                # oddities (Get-NetTCPConnection / Get-Process quirks);
                # failure to verify a kill we initiated is a different beast
                # - it can leave us deleting the venv around a still-running
                # python, which is exactly what this guard exists to prevent.
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
                    # Stop-Process is fire-and-forget. Without WaitForExit it can
                    # return cleanly while the process is still alive (anti-virus
                    # blocking termination, kernel-mode handle, etc.); we then
                    # delete the venv around it and ship a deadlock to the next
                    # install run. Verify before continuing.
                    if (-not $proc.WaitForExit(5000)) {
                        $detail = if ($killErr) { "Stop-Process raised: $killErr" } else { "process did not exit within 5s" }
                        Write-Fail "Could not stop PID $($proc.Id) holding port ${port} ($detail).`n      Open an Administrator cmd and run:  taskkill /F /PID $($proc.Id)`n      Then re-run uninstall."
                    }
                    Write-Ok "Stopped $($proc.Name) (PID $($proc.Id)) on port $port"
                } catch {
                    Write-Fail "Process-stop verification failed for PID $($proc.Id) ($($_.Exception.Message)). Cannot confirm the API process is gone; refusing to proceed with destructive uninstall steps.`n      Open an Administrator cmd and run:  taskkill /F /PID $($proc.Id)`n      Then re-run uninstall."
                }
            }
        } else {
            Write-Warn "Found listener on port $port, but could not resolve the owning process"
        }
    } else {
        Write-Skip "App is not running on port $port"
    }
} catch {
    Write-Warn "Could not check running state: $_"
}

# -- Tier 1: Stop tray app ----------------------------------------------------

try {
    $trayProcs = Get-Process -Name "MediaSearchAgentTray" -ErrorAction SilentlyContinue |
        Where-Object { $_.Path -like "$AppDir*" }
    foreach ($p in $trayProcs) {
        Stop-Process -Id $p.Id -Force -ErrorAction SilentlyContinue
        Write-Ok "Stopped tray app (PID $($p.Id))"
    }
    if (-not $trayProcs) { Write-Skip "Tray app is not running" }
} catch {
    Write-Warn "Could not check tray process: $_"
}

# -- Tier 1: Remove Task Scheduler auto-start ---------------------------------

Write-Step "Tier 1 - Removing auto-start task"

if (Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue) {
    Unregister-ScheduledTask -TaskName $taskName -Confirm:$false -ErrorAction SilentlyContinue
    Write-Ok "Task Scheduler task '$taskName' removed"
} else {
    Write-Skip "Auto-start task not found"
}

if (Get-ItemProperty -Path $runKeyPath -Name $taskName -ErrorAction SilentlyContinue) {
    Remove-ItemProperty -Path $runKeyPath -Name $taskName -ErrorAction SilentlyContinue
    Write-Ok "Run registry auto-start '$taskName' removed"
} else {
    Write-Skip "Run registry auto-start not found"
}

# -- Tier 1: Remove launcher + PATH entry -------------------------------------

Write-Step "Tier 1 - Removing launcher"

if (Test-Path $Launcher) {
    # `msa uninstall` invokes the launcher (msa.cmd), which runs `powershell
    # -File uninstall.ps1`. cmd.exe streams batch files and seeks ahead at
    # block boundaries - when PowerShell returns it still needs to read the
    # next lines of msa.cmd ("exit /b %ERRORLEVEL%", the closing ")", etc.).
    # Deleting msa.cmd inline produces "The batch file cannot be found"
    # output (typically twice, once per cmd.exe read attempt) at the very
    # end of an otherwise-successful uninstall run.
    #
    # Defer the delete via a detached cmd that waits a few seconds, so the
    # parent cmd.exe finishes reading the script before the file disappears.
    # `timeout /nobreak > nul` is the standard Windows sleep that survives
    # being detached (Start-Sleep would die with our PowerShell session).
    $deferDelete = "timeout /t 3 /nobreak > nul & del /q `"$Launcher`""
    Start-Process -FilePath "cmd.exe" `
        -ArgumentList "/c", $deferDelete `
        -WindowStyle Hidden -ErrorAction SilentlyContinue | Out-Null
    Write-Ok "Launcher scheduled for removal: $Launcher (deferred a few seconds)"
}
if (Test-Path $LauncherDir) {
    # Remove LauncherDir from user PATH if it is there
    $userPath = [Environment]::GetEnvironmentVariable("Path", "User")
    if ($userPath -like "*$LauncherDir*") {
        $newPath = ($userPath -split ';' | Where-Object { $_ -ne $LauncherDir }) -join ';'
        [Environment]::SetEnvironmentVariable("Path", $newPath, "User")
        Write-Ok "Removed $LauncherDir from user PATH"
    }
}

foreach ($item in @(
    $TrayExe, $TrayPathsEnv,
    (Join-Path $LauncherDir "exiftool.exe"),
    # Legacy bundled tools (removed in commit bdcb543); cleaned up here so
    # upgrades from older installs don't leave the launcher dir behind.
    (Join-Path $LauncherDir "ffmpeg.exe"),
    (Join-Path $LauncherDir "ffprobe.exe")
)) {
    if (Test-Path $item) {
        Remove-Item $item -Force -ErrorAction SilentlyContinue
        Write-Ok "Removed $(Split-Path $item -Leaf)"
    }
}

# ExifTool ships with a companion exiftool_files\ directory of Perl modules.
# Removed here as a directory so the launcher dir can be deleted cleanly below.
$exiftoolFilesDir = Join-Path $LauncherDir "exiftool_files"
if (Test-Path $exiftoolFilesDir) {
    Remove-Item $exiftoolFilesDir -Recurse -Force -ErrorAction SilentlyContinue
    Write-Ok "Removed exiftool_files\"
}

if (Test-Path $LauncherDir) {
    # Remove launcher dir if empty (may also contain uv.exe - Tier 3, keep it)
    $remaining = Get-ChildItem $LauncherDir -ErrorAction SilentlyContinue
    if (-not $remaining) {
        Remove-Item $LauncherDir -Force -ErrorAction SilentlyContinue
        Write-Ok "Removed empty launcher directory"
    } else {
        Write-Skip "Launcher directory kept (contains: $(($remaining | Select-Object -ExpandProperty Name) -join ', '))"
    }
}

foreach ($scriptPath in @($StartScript, $StopScript)) {
    if (Test-Path $scriptPath) {
        Remove-Item $scriptPath -Force -ErrorAction SilentlyContinue
        Write-Ok "Removed $(Split-Path $scriptPath -Leaf)"
    }
}

if (Test-Path $StartMenuDir) {
    Remove-Item $StartMenuDir -Recurse -Force -ErrorAction SilentlyContinue
    Write-Ok "Removed Start Menu shortcuts"
} else {
    Write-Skip "Start Menu shortcuts not found"
}

# -- Tier 1: Remove Python venv -----------------------------------------------

Write-Step "Tier 1 - Removing Python venv"

if (Test-Path $VenvDir) {
    Remove-Item $VenvDir -Recurse -Force -ErrorAction SilentlyContinue
    Write-Ok "Venv removed: $VenvDir"
} else {
    Write-Skip "Venv not found"
}

# -- Tier 1: Remove app code items --------------------------------------------
# Remove only the specific code items installed by install.ps1 - not the whole
# AppDir - so the ML model cache (Cache\) and any user files are not touched.

Write-Step "Tier 1 - Removing app code"

$codeItems = @('src', 'scripts', 'bin', 'pyproject.toml', 'requirements.txt',
               'requirements-windows.txt', 'LICENSE', 'NOTICE', 'uninstall.ps1',
               'config.yaml.template',
               # Pip/setuptools byproducts of `uv pip install $RepoDir` during
               # install. Not copied by the installer, so they were previously
               # left behind and tripped the empty-directory check below.
               'build', '__pycache__')
$anyCode = $false
foreach ($item in $codeItems) {
    $target = Join-Path $RepoDir $item
    if (Test-Path $target) {
        Remove-Item $target -Recurse -Force -ErrorAction SilentlyContinue
        Write-Ok "Removed repo\$item"
        $anyCode = $true
    }
}
# Glob-named byproducts (e.g. media_search_agent.egg-info/) need wildcard expansion.
foreach ($pattern in @('*.egg-info')) {
    Get-ChildItem -Path $RepoDir -Filter $pattern -Force -ErrorAction SilentlyContinue |
        ForEach-Object {
            Remove-Item $_.FullName -Recurse -Force -ErrorAction SilentlyContinue
            Write-Ok "Removed repo\$($_.Name)"
            $script:anyCode = $true
        }
}
if (-not $anyCode) { Write-Skip "No app code found at $RepoDir" }

# Remove RepoDir if empty
if (Test-Path $RepoDir) {
    $rem = Get-ChildItem $RepoDir -ErrorAction SilentlyContinue
    if (-not $rem) {
        Remove-Item $RepoDir -Force -ErrorAction SilentlyContinue
        Write-Ok "Removed empty repo directory"
    } else {
        Write-Skip "Repo directory kept (contains: $(($rem | Select-Object -ExpandProperty Name) -join ', '))"
    }
}

# -- Tier 2: Index + data (prompt, default KEEP) ------------------------------

Write-Step "Tier 2 - User data (kept by default)"
Write-Host "  The media index represents your indexing work and cannot be regenerated quickly." -ForegroundColor DarkGray
Write-Host ""

if (Test-Path $IndexDir) {
    [long]$bytes = 0
    Get-ChildItem $IndexDir -Recurse -File -ErrorAction SilentlyContinue |
        ForEach-Object { $bytes += $_.Length }
    $sizeStr = if ($bytes -gt 1GB) { '{0:N1} GB' -f ($bytes/1GB) }
               elseif ($bytes -gt 1MB) { '{0:N0} MB' -f ($bytes/1MB) }
               else { '{0:N0} KB' -f ($bytes/1KB) }

    if (Read-KeepChoice "Delete media index at '$IndexDir' ($sizeStr)? This cannot be undone.") {
        Remove-Item $IndexDir -Recurse -Force -ErrorAction SilentlyContinue
        Write-Ok "Index removed"
    } else {
        Write-Skip "Keeping index at $IndexDir"
    }
} else {
    Write-Skip "No index found"
}

# -- Tier 2: Config (prompt, default KEEP) ------------------------------------

if (Test-Path $ConfigPath) {
    if (Read-KeepChoice "Delete config file at '$ConfigPath'?") {
        Remove-Item $ConfigPath -Force -ErrorAction SilentlyContinue
        Write-Ok "Config removed"
    } else {
        Write-Skip "Keeping config at $ConfigPath"
    }
}

# -- Tier 2: Logs (prompt, default KEEP) --------------------------------------

if (Test-Path $LogDir) {
    if (Read-KeepChoice "Delete log files at '$LogDir'?") {
        $script:ArchiveUninstallLog = $false
        Remove-Item $LogDir -Recurse -Force -ErrorAction SilentlyContinue
        Write-Ok "Logs removed"
    } else {
        Write-Skip "Keeping logs at $LogDir"
    }
}

# -- Tier 2: App-private model cache (prompt, default KEEP) -------------------

if (Test-Path $ModelCacheDir) {
    if (Read-KeepChoice "Delete app-private model cache at '$ModelCacheDir'? Re-download will be required later.") {
        Remove-Item $ModelCacheDir -Recurse -Force -ErrorAction SilentlyContinue
        Write-Ok "Model cache removed"
    } else {
        Write-Skip "Keeping model cache at $ModelCacheDir"
    }
}

# -- Clean up empty top-level directories -------------------------------------

foreach ($dir in @($AppDir, $DataDir)) {
    if (Test-Path $dir) {
        $rem = Get-ChildItem $dir -ErrorAction SilentlyContinue
        if (-not $rem) {
            Remove-Item $dir -Force -ErrorAction SilentlyContinue
            Write-Ok "Removed empty directory: $dir"
        } else {
            Write-Skip "Directory kept (not empty): $dir"
        }
    }
}

# -- Tier 3: Never removed ----------------------------------------------------

Write-Host ""
Write-Host "  Not removed (Tier 3 - never touched):" -ForegroundColor DarkGray
Write-Host "    uv binary - may be used by other projects" -ForegroundColor DarkGray
Write-Host "    Your media library - never modified by this app" -ForegroundColor DarkGray
Write-Host ""
Write-Host "  + Media Search Agent uninstalled." -ForegroundColor Green
Write-Host ""
} finally {
    Stop-UninstallLogging
    if ($script:UninstallLog) {
        if ($script:ArchiveUninstallLog) {
            if (-not (Test-Path $LogDir)) {
                New-Item -ItemType Directory -Path $LogDir -Force | Out-Null
            }
            $finalLog = Join-Path $LogDir $script:UninstallLogBaseName
            Move-Item $script:UninstallLog $finalLog -Force
            Write-Host "  Uninstall log saved to: $finalLog" -ForegroundColor DarkGray
        } else {
            Write-Host "  Uninstall log kept in temp: $($script:UninstallLog)" -ForegroundColor DarkGray
        }
    }
}
