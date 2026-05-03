#Requires -Version 5.1
[CmdletBinding()]
param(
    [string] $WorkDir = 'C:\E2E'
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

function Write-Utf8NoBomFile {
    param(
        [Parameter(Mandatory = $true)][string] $Path,
        [Parameter(Mandatory = $true)][string] $Content
    )

    $parent = Split-Path -Parent $Path
    if ($parent) {
        New-Item -ItemType Directory -Force -Path $parent | Out-Null
    }

    $utf8NoBom = New-Object System.Text.UTF8Encoding($false)
    [System.IO.File]::WriteAllText($Path, $Content, $utf8NoBom)
}

function Write-LogLine {
    param(
        [Parameter(Mandatory = $true)][string] $Path,
        [Parameter(Mandatory = $true)][string] $Message
    )

    $parent = Split-Path -Parent $Path
    if ($parent) {
        New-Item -ItemType Directory -Force -Path $parent | Out-Null
    }

    $utf8NoBom = New-Object System.Text.UTF8Encoding($false)
    $line = (Get-Date -Format 'HH:mm:ss') + ' ' + $Message + [Environment]::NewLine
    [System.IO.File]::AppendAllText($Path, $line, $utf8NoBom)
}

function Copy-DirectoryContent {
    param(
        [Parameter(Mandatory = $true)][string] $SourceDir,
        [Parameter(Mandatory = $true)][string] $DestinationDir
    )

    if (-not (Test-Path -LiteralPath $SourceDir)) {
        return
    }

    New-Item -ItemType Directory -Force -Path $DestinationDir | Out-Null
    Copy-Item -Path (Join-Path $SourceDir '*') -Destination $DestinationDir -Recurse -Force -ErrorAction SilentlyContinue
}

$OutputDir = Join-Path $WorkDir 'output'
$UninstallLogsDir = Join-Path $OutputDir 'uninstall-logs'
$ActionLog = Join-Path $UninstallLogsDir 'uninstall-app.log'
New-Item -ItemType Directory -Force -Path $UninstallLogsDir | Out-Null

$AppBinDir = Join-Path $env:LOCALAPPDATA 'Programs\MediaSearchAgent'
$AppLogDir = Join-Path $env:LOCALAPPDATA 'MediaSearchAgent\logs'
$Uninstaller = Join-Path $AppBinDir 'unins000.exe'

Copy-DirectoryContent -SourceDir $AppLogDir -DestinationDir (Join-Path $UninstallLogsDir 'pre-uninstall-app-logs')

if (-not (Test-Path -LiteralPath $Uninstaller)) {
    throw "Uninstaller not found: $Uninstaller"
}

Write-LogLine -Path $ActionLog -Message ("Running: " + $Uninstaller)

$proc = Start-Process -FilePath $Uninstaller `
    -ArgumentList @('/VERYSILENT', '/SUPPRESSMSGBOXES', '/NORESTART') `
    -PassThru

if (-not $proc.WaitForExit(300000)) {
    Write-LogLine -Path $ActionLog -Message 'Uninstaller timed out after 5 minutes; sending Kill()'
    try {
        $proc.Kill()
    } catch {
        Write-LogLine -Path $ActionLog -Message ("Uninstaller kill attempt failed: " + $_.Exception.Message)
    }
    throw 'Uninstaller timed out after 5 minutes'
}

Write-LogLine -Path $ActionLog -Message ("Uninstaller exit code: " + $proc.ExitCode)

if ($proc.ExitCode -ne 0) {
    throw "Uninstaller exited with code $($proc.ExitCode)"
}

Start-Sleep -Seconds 3
$listening = Get-NetTCPConnection -LocalPort 8000 -State Listen -ErrorAction SilentlyContinue
if (Test-Path -LiteralPath $AppBinDir) {
    throw "Uninstall incomplete: install directory still exists: $AppBinDir"
}
if ($listening) {
    throw "Uninstall incomplete: app is still listening on port 8000"
}

$status = [pscustomobject]@{
    status               = 'passed'
    uninstaller          = $Uninstaller
    install_dir_exists   = $false
    listener_on_8000     = $false
    completed_at         = (Get-Date).ToUniversalTime().ToString('o')
}

Write-Utf8NoBomFile -Path (Join-Path $OutputDir 'uninstall.json') -Content ($status | ConvertTo-Json -Depth 4)
