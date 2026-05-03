#Requires -Version 5.1
[CmdletBinding()]
param(
    [string] $WorkDir = 'C:\E2E',
    [string] $BaseUrl = 'http://127.0.0.1:8000',
    [int] $TimeoutSeconds = 180,
    [int] $PollSeconds = 2
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
$LaunchLogsDir = Join-Path $OutputDir 'launch-logs'
$SmokeDir = Join-Path $OutputDir 'smoke'
$SmokeLog = Join-Path $SmokeDir 'run-smoke-test.log'
New-Item -ItemType Directory -Force -Path $LaunchLogsDir, $SmokeDir | Out-Null
Write-LogLine -Path $SmokeLog -Message ("Smoke test start. BaseUrl=" + $BaseUrl)

$deadline = (Get-Date).AddSeconds($TimeoutSeconds)
$healthUri = $BaseUrl.TrimEnd('/') + '/health'
$rootUri = $BaseUrl.TrimEnd('/')
$healthResponse = $null
$lastError = ''

while ((Get-Date) -lt $deadline) {
    try {
        Write-LogLine -Path $SmokeLog -Message ("Requesting " + $healthUri)
        $healthResponse = Invoke-WebRequest -Uri $healthUri -UseBasicParsing -TimeoutSec 5 -ErrorAction Stop
        if ($healthResponse.StatusCode -eq 200) {
            Write-LogLine -Path $SmokeLog -Message ("Health ready with status " + $healthResponse.StatusCode)
            break
        }
    } catch {
        $lastError = $_.Exception.Message
        Write-LogLine -Path $SmokeLog -Message ("Health request failed: " + $lastError)
    }

    Start-Sleep -Seconds $PollSeconds
}

if (-not $healthResponse -or $healthResponse.StatusCode -ne 200) {
    throw "Health check did not succeed within $TimeoutSeconds seconds. Last error: $lastError"
}

$AppendRoot = "Requesting " + $rootUri
Write-LogLine -Path $SmokeLog -Message $AppendRoot
$rootResponse = Invoke-WebRequest -Uri $rootUri -UseBasicParsing -TimeoutSec 10 -ErrorAction Stop
if ($rootResponse.StatusCode -ne 200) {
    throw "Root page returned status $($rootResponse.StatusCode)"
}

$rootBody = [string]$rootResponse.Content
if ($rootBody -notmatch 'id="root"') {
    throw 'Root page did not contain the SPA mount point (id="root")'
}

$appLogDir = Join-Path $env:LOCALAPPDATA 'MediaSearchAgent\logs'
Copy-DirectoryContent -SourceDir $appLogDir -DestinationDir $LaunchLogsDir
Write-LogLine -Path $SmokeLog -Message "Smoke test completed successfully"

$status = [pscustomobject]@{
    status          = 'passed'
    base_url        = $BaseUrl
    health_uri      = $healthUri
    root_uri        = $rootUri
    health_code     = [int]$healthResponse.StatusCode
    root_code       = [int]$rootResponse.StatusCode
    completed_at    = (Get-Date).ToUniversalTime().ToString('o')
}

Write-Utf8NoBomFile -Path (Join-Path $SmokeDir 'health.txt') -Content ([string]$healthResponse.Content)
Write-Utf8NoBomFile -Path (Join-Path $SmokeDir 'root.html') -Content $rootBody
Write-Utf8NoBomFile -Path (Join-Path $OutputDir 'smoke.json') -Content ($status | ConvertTo-Json -Depth 4)
