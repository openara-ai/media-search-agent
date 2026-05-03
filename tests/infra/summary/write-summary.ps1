#Requires -Version 5.1
[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)][string] $RunDirectory,
    [Parameter(Mandatory = $true)][string] $HostStatusPath
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

$summaryPath = Join-Path $RunDirectory 'summary.md'
$summaryJsonPath = Join-Path $RunDirectory 'summary.json'
$hostStatus = Get-Content -LiteralPath $HostStatusPath -Raw | ConvertFrom-Json

$guestJsonFiles = Get-ChildItem -Path (Join-Path $RunDirectory 'guest-output') -Filter '*.json' -ErrorAction SilentlyContinue
$guestResults = @()
foreach ($file in $guestJsonFiles) {
    try {
        $guestResults += [pscustomobject]@{
            file = $file.Name
            data = (Get-Content -LiteralPath $file.FullName -Raw | ConvertFrom-Json)
        }
    } catch {
        $guestResults += [pscustomobject]@{
            file = $file.Name
            data = [pscustomobject]@{ status = 'parse_failed'; message = $_.Exception.Message }
        }
    }
}

$lines = New-Object System.Collections.Generic.List[string]
$lines.Add('# Local E2E Summary')
$lines.Add('')
$lines.Add('| Field | Value |')
$lines.Add('|---|---|')
$lines.Add('| Scenario | ' + $hostStatus.scenario + ' |')
$lines.Add('| VM | ' + $hostStatus.vm_name + ' |')
$lines.Add('| Checkpoint | ' + $hostStatus.checkpoint + ' |')
$lines.Add('| Result | ' + ($(if ($hostStatus.succeeded) { 'PASS' } else { 'FAIL' })) + ' |')
if ($hostStatus.installer) {
    $lines.Add('| Installer | ' + $hostStatus.installer + ' |')
}
$lines.Add('')
$lines.Add('## Steps')
$lines.Add('')
$lines.Add('| Step | Status | Detail |')
$lines.Add('|---|---|---|')
foreach ($step in $hostStatus.steps) {
    $lines.Add('| ' + $step.name + ' | ' + $step.status + ' | ' + ($step.detail -replace '\|', '/') + ' |')
}

if ($guestResults.Count -gt 0) {
    $lines.Add('')
    $lines.Add('## Guest Results')
    $lines.Add('')
    $lines.Add('| File | Status |')
    $lines.Add('|---|---|')
    foreach ($result in $guestResults) {
        $status = ''
        if ($result.data.PSObject.Properties.Name -contains 'status') {
            $status = [string]$result.data.status
        }
        $lines.Add('| ' + $result.file + ' | ' + $status + ' |')
    }
}

if ($hostStatus.error_message) {
    $lines.Add('')
    $lines.Add('## Error')
    $lines.Add('')
    $lines.Add($hostStatus.error_message)
}

Write-Utf8NoBomFile -Path $summaryPath -Content ($lines -join [Environment]::NewLine)

$summaryJson = [pscustomobject]@{
    host  = $hostStatus
    guest = $guestResults
}
Write-Utf8NoBomFile -Path $summaryJsonPath -Content ($summaryJson | ConvertTo-Json -Depth 8)
