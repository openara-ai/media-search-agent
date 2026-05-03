#Requires -Version 5.1
<#
.SYNOPSIS
    Media Search Agent - Windows Native CLI launcher.

.DESCRIPTION
    Prepares the installed Media Search Agent environment for interactive CLI use,
    then leaves the PowerShell session open so commands like `msa` work
    without typing the full venv path.

    Intended for Start Menu usage via:
      powershell.exe -NoExit -ExecutionPolicy Bypass -File cli.ps1

.PARAMETER AppDir
    App internals directory containing the .venv and logs.
    Default: %LOCALAPPDATA%\MediaSearchAgent

.PARAMETER DataDir
    User data directory containing config.yaml.
    Default: %USERPROFILE%\MediaSearchAgent

.PARAMETER ConfigFile
    Path to config.yaml.
    Default: %USERPROFILE%\MediaSearchAgent\config.yaml
#>
[Diagnostics.CodeAnalysis.SuppressMessageAttribute('PSAvoidUsingWriteHost', '',
    Justification='Interactive CLI launcher - console output is intentional.')]
[CmdletBinding()]
param(
    [string] $AppDir     = "$env:LOCALAPPDATA\MediaSearchAgent",
    [string] $DataDir    = "$env:USERPROFILE\MediaSearchAgent",
    [string] $ConfigFile = ""
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

if (-not $ConfigFile) {
    $ConfigFile = "$DataDir\config.yaml"
}

$VenvDir      = "$AppDir\.venv"
$ScriptsDir   = "$VenvDir\Scripts"
$MsaExe       = "$ScriptsDir\msa.exe"
$ActivatePs1  = "$ScriptsDir\Activate.ps1"
$LogDir       = "$AppDir\logs"

function Write-MsaCliLine {
    param([string] $Text, [ConsoleColor] $Color = [ConsoleColor]::Gray)
    Write-Host $Text -ForegroundColor $Color
}

if (-not (Test-Path $MsaExe)) {
    Write-Host "ERROR: Media Search Agent CLI not found at $MsaExe" -ForegroundColor Red
    Write-Host "Re-run the installer to repair the installation." -ForegroundColor Red
    exit 1
}

New-Item -ItemType Directory -Force -Path $LogDir | Out-Null
New-Item -ItemType Directory -Force -Path $DataDir | Out-Null

# Ensure the installed environment is the active default for this shell session.
$env:MSA_CONFIG_PATH = $ConfigFile
$env:MSA_DATA_DIR    = $DataDir
$env:MSA_LOG_DIR     = $LogDir
$env:VIRTUAL_ENV     = $VenvDir

if ($env:PATH -notlike "*$ScriptsDir*") {
    $env:PATH = "$ScriptsDir;$env:PATH"
}

if (Test-Path $ActivatePs1) {
    . $ActivatePs1
}

# Replace the default venv prompt label with a product-oriented shell prompt.
function global:prompt {
    Write-Host "(MSA) " -NoNewline -ForegroundColor Yellow
    "PS $(Get-Location)> "
}

Set-Location $DataDir

Write-MsaCliLine ""
Write-MsaCliLine "Media Search Agent CLI" Cyan
Write-MsaCliLine "  AppDir:    $AppDir"
Write-MsaCliLine "  DataDir:   $DataDir"
Write-MsaCliLine "  Config:    $ConfigFile"
Write-MsaCliLine ""
Write-MsaCliLine "Ready. Try one of these commands:" Yellow
Write-MsaCliLine "  msa --help"
Write-MsaCliLine "  msa api status"
Write-MsaCliLine "  msa api start"
Write-MsaCliLine "  msa api stop"
Write-MsaCliLine "  msa index run --dry-run"
Write-MsaCliLine "  msa index run"
Write-MsaCliLine "  msa index backup"
Write-MsaCliLine ""
