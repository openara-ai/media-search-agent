#Requires -Version 5.1
[CmdletBinding()]
param(
    [string] $WorkDir = 'C:\E2E',
    [string] $BundlePath = '',
    [string] $BootstrapPath = '',
    # Legacy alias retained so older callers passing -InstallerPath still work
    # (forwarded to -BundlePath when -BundlePath is not set).
    [string] $InstallerPath = ''
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

$InputDir = Join-Path $WorkDir 'input'
$OutputDir = Join-Path $WorkDir 'output'
$InstallerLogsDir = Join-Path $OutputDir 'installer-logs'
$ActionLog = Join-Path $InstallerLogsDir 'install-app.log'
$BootstrapLog = Join-Path $InstallerLogsDir 'install-bootstrap.log'

New-Item -ItemType Directory -Force -Path $InstallerLogsDir | Out-Null

# Legacy compat: callers that still pass -InstallerPath should be forwarded
# to -BundlePath unless -BundlePath was set explicitly.
if (-not $BundlePath -and $InstallerPath) {
    $BundlePath = $InstallerPath
}

if (-not $BundlePath) {
    $candidate = Get-ChildItem -Path $InputDir -Filter 'MediaSearchAgent-*-windows-x86_64.zip' |
        Sort-Object LastWriteTimeUtc -Descending |
        Select-Object -First 1

    if (-not $candidate) {
        throw "No bundle .zip found in $InputDir matching MediaSearchAgent-*-windows-x86_64.zip"
    }

    $BundlePath = $candidate.FullName
}

if (-not (Test-Path -LiteralPath $BundlePath)) {
    throw "Bundle not found: $BundlePath"
}

if (-not $BootstrapPath) {
    $BootstrapPath = Join-Path $InputDir 'install.ps1'
}

if (-not (Test-Path -LiteralPath $BootstrapPath)) {
    throw "Bootstrap install.ps1 not found: $BootstrapPath"
}

$AppBinDir = Join-Path $env:LOCALAPPDATA 'Programs\MediaSearchAgent'
$StartScript = Join-Path $AppBinDir 'start.ps1'

Write-LogLine -Path $ActionLog -Message ("Bundle:    " + $BundlePath)
Write-LogLine -Path $ActionLog -Message ("Bootstrap: " + $BootstrapPath)
Write-LogLine -Path $ActionLog -Message 'Running shell-bundle install (no UAC, current user only)'

# shell/install.ps1 -Bundle <zip> performs the offline install: extracts the
# bundle, sets up uv + venv + dependencies, writes config.yaml from template.
$proc = Start-Process -FilePath 'powershell.exe' `
    -ArgumentList @(
        '-NoProfile',
        '-ExecutionPolicy', 'Bypass',
        '-File', $BootstrapPath,
        '-Bundle', $BundlePath,
        '-SkipAutoStart'
    ) `
    -RedirectStandardOutput $BootstrapLog `
    -RedirectStandardError ($BootstrapLog + '.err') `
    -PassThru

if (-not $proc.WaitForExit(600000)) {
    try {
        $proc.Kill()
    } catch {
        Write-LogLine -Path $ActionLog -Message ("Installer kill attempt failed: " + $_.Exception.Message)
    }
    throw 'Installer timed out after 10 minutes'
}

Write-LogLine -Path $ActionLog -Message ("Installer exit code: " + $proc.ExitCode)

if ($proc.ExitCode -ne 0) {
    throw "Installer exited with code $($proc.ExitCode)"
}

if (-not (Test-Path -LiteralPath $StartScript)) {
    throw "Installed start script not found: $StartScript"
}

Write-LogLine -Path $ActionLog -Message 'Installer completed; launch is handled by launch-app.ps1'

$status = [pscustomobject]@{
    scenario       = 'installer'
    status         = 'passed'
    bundle_path    = $BundlePath
    bootstrap_path = $BootstrapPath
    app_bin_dir    = $AppBinDir
    completed_at   = (Get-Date).ToUniversalTime().ToString('o')
}

Write-Utf8NoBomFile -Path (Join-Path $OutputDir 'install.json') -Content ($status | ConvertTo-Json -Depth 5)
