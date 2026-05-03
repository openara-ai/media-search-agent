#Requires -Version 5.1
<#
.SYNOPSIS
    Media Search Agent - Windows Native stop script.

.DESCRIPTION
    Stops the running Media Search Agent API process by terminating the process
    currently listening on the configured API port.

.PARAMETER AppDir
    App internals directory containing the .venv and logs.
    Default: %LOCALAPPDATA%\MediaSearchAgent

.PARAMETER DataDir
    User data directory containing config.yaml.
    Default: %USERPROFILE%\MediaSearchAgent

.PARAMETER ConfigFile
    Path to config.yaml.
    Default: %USERPROFILE%\MediaSearchAgent\config.yaml

.PARAMETER Port
    API port. Default: 8000

.PARAMETER Help
    Show this help message and exit.

.EXAMPLE
    .\stop.ps1
    Stop the running Media Search Agent on the default port.

.EXAMPLE
    .\stop.ps1 -Port 8080
    Stop an instance running on a non-default port.
#>
[Diagnostics.CodeAnalysis.SuppressMessageAttribute('PSAvoidUsingWriteHost', '',
    Justification='Interactive stop script - colored console output is intentional.')]
[CmdletBinding()]
param(
    [string] $AppDir     = "$env:LOCALAPPDATA\MediaSearchAgent",
    [string] $DataDir    = "$env:USERPROFILE\MediaSearchAgent",
    [string] $ConfigFile = "",
    [int]    $Port       = 8000,
    [switch] $Help
)

if ($Help) {
    Write-Host "Usage: .\stop.ps1 [options]"
    Write-Host ""
    Write-Host "Stops the running Media Search Agent by terminating the process on the API port."
    Write-Host "Qdrant is embedded - no separate process to stop."
    Write-Host ""
    Write-Host "Options:"
    Write-Host "  -AppDir <path>      App internals dir (logs). Default: %LOCALAPPDATA%\MediaSearchAgent"
    Write-Host "  -DataDir <path>     User data dir (config.yaml). Default: %USERPROFILE%\MediaSearchAgent"
    Write-Host "  -ConfigFile <path>  Path to config.yaml. Default: <DataDir>\config.yaml"
    Write-Host "  -Port <int>         API port. Default: 8000"
    Write-Host "  -Help               Show this help message and exit."
    Write-Host ""
    Write-Host "Examples:"
    Write-Host "  .\stop.ps1"
    Write-Host "  .\stop.ps1 -Port 8080"
    exit 0
}

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$LogDir = "$AppDir\logs"
New-Item -ItemType Directory -Force -Path $LogDir | Out-Null
$LogFile = "$LogDir\stop-$(Get-Date -Format 'yyyy-MM-dd_HHmmss').log"

function Write-MsaLog { param([string]$Msg)
    $line = "$(Get-Date -Format 'HH:mm:ss') $Msg"
    Write-Host $line
    Add-Content -Path $LogFile -Value $line
}

function Write-MsaDone {
    Write-MsaLog "----- end of log -----"
}

if (-not $ConfigFile) {
    $ConfigFile = "$DataDir\config.yaml"
}

$script:WinFormsAvailable = $false
try {
    Add-Type -AssemblyName System.Windows.Forms -ErrorAction Stop
    $script:WinFormsAvailable = $true
} catch {
    # System.Windows.Forms may be unavailable on Server Core / headless environments.
    # Toast notifications are optional; the stop script continues without them.
    Write-Verbose "System.Windows.Forms not available; toast notifications disabled. $_"
}

function Show-Toast {
    param([string]$Title, [string]$Text)
    if (-not $script:WinFormsAvailable) { return }
    try {
        $notify = New-Object System.Windows.Forms.NotifyIcon
        $notify.Icon    = [System.Drawing.SystemIcons]::Information
        $notify.Visible = $true
        $notify.ShowBalloonTip(4000, $Title, $Text, [System.Windows.Forms.ToolTipIcon]::Info)
        Start-Sleep -Seconds 1
        $notify.Dispose()
    } catch {
        Write-Verbose "Toast notification failed: $_"
    }
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
        Write-Verbose "Could not parse API port from $Path : $_"
    }

    return $DefaultPort
}

$Port = Get-ConfiguredPort -Path $ConfigFile -DefaultPort $Port
$startedAt = Get-Date

Write-MsaLog "Media Search Agent stopping. Log: $LogFile"
Write-MsaLog "Checking for API on port $Port..."

try {
    $conn = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue |
        Select-Object -First 1
} catch {
    $conn = $null
}

if (-not $conn) {
    Write-MsaLog "App is not running on port $Port."
    Write-MsaDone
    Show-Toast "Media Search Agent" "Not running."
    exit 0
}

$proc = Get-Process -Id $conn.OwningProcess -ErrorAction SilentlyContinue
if (-not $proc) {
    Write-MsaLog "Found port $Port in use, but could not resolve the owning process."
    Write-MsaDone
    Show-Toast "Media Search Agent" "Could not identify the running process."
    exit 1
}

$procPath = ''
try {
    $procPath = [string]$proc.Path
} catch {
    $procPath = ''
}

if ($procPath -and ($procPath -notlike "$AppDir*") -and ($proc.ProcessName -notmatch '^(python|uvicorn)(\.exe)?$')) {
    Write-MsaLog "Refusing to stop unexpected process on port ${Port}: $($proc.ProcessName) ($procPath)"
    Write-MsaDone
    Show-Toast "Media Search Agent" "Unexpected process is using port $Port."
    exit 1
}

try {
    Write-MsaLog "Stopping Media Search Agent (PID $($proc.Id), process $($proc.ProcessName))..."
    Stop-Process -Id $proc.Id -Force -ErrorAction Stop
    $elapsed = [int][Math]::Round(((Get-Date) - $startedAt).TotalSeconds)
    Write-MsaLog "Stopped Media Search Agent ($($proc.ProcessName), PID $($proc.Id)) in ${elapsed}s."
    Write-MsaLog "All services stopped."
    Write-MsaDone
    Show-Toast "Media Search Agent" "Stopped."
    exit 0
} catch {
    Write-MsaLog "Failed to stop Media Search Agent: $_"
    Write-MsaDone
    Show-Toast "Media Search Agent" "Failed to stop."
    exit 1
}
