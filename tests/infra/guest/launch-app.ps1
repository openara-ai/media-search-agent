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

$OutputDir = Join-Path $WorkDir 'output'
$LaunchDir = Join-Path $OutputDir 'launch'
$LaunchLog = Join-Path $LaunchDir 'launch-app.log'
New-Item -ItemType Directory -Force -Path $LaunchDir | Out-Null

$AppBinDir = Join-Path $env:LOCALAPPDATA 'Programs\MediaSearchAgent'
$AppDir = Join-Path $env:LOCALAPPDATA 'MediaSearchAgent'
$DataDir = Join-Path $env:USERPROFILE 'MediaSearchAgent'
$StartScript = Join-Path $AppBinDir 'start.ps1'

if (-not (Test-Path -LiteralPath $StartScript)) {
    throw "Installed start script not found: $StartScript"
}

Write-LogLine -Path $LaunchLog -Message 'Launching installed app with -NoBrowser'

$startProc = Start-Process -FilePath 'powershell.exe' `
    -ArgumentList @(
        '-NoProfile',
        '-NonInteractive',
        '-ExecutionPolicy', 'Bypass',
        '-File', $StartScript,
        '-AppDir', $AppDir,
        '-DataDir', $DataDir,
        '-NoBrowser'
    ) `
    -PassThru

Write-LogLine -Path $LaunchLog -Message ("start.ps1 launched with PID " + $startProc.Id)
Start-Sleep -Seconds 8
if ($startProc.HasExited) {
    Write-LogLine -Path $LaunchLog -Message ("start.ps1 exited early with code " + $startProc.ExitCode)
    if ($startProc.ExitCode -ne 0) {
        throw "Installed start.ps1 exited early with code $($startProc.ExitCode)"
    }
}
Write-LogLine -Path $LaunchLog -Message 'Installed app launch step dispatched'

$status = [pscustomobject]@{
    status         = 'passed'
    app_bin_dir    = $AppBinDir
    app_dir        = $AppDir
    data_dir       = $DataDir
    launcher_pid   = $startProc.Id
    completed_at   = (Get-Date).ToUniversalTime().ToString('o')
}

Write-Utf8NoBomFile -Path (Join-Path $OutputDir 'launch.json') -Content ($status | ConvertTo-Json -Depth 4)
