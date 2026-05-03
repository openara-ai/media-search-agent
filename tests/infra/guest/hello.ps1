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

$OutputDir = Join-Path $WorkDir 'output'
$ScaffoldDir = Join-Path $OutputDir 'scaffold'
New-Item -ItemType Directory -Force -Path $ScaffoldDir | Out-Null

$helloText = @(
    'hello from Media Search Agent E2E scaffold'
    ('computer: ' + $env:COMPUTERNAME)
    ('user: ' + [System.Security.Principal.WindowsIdentity]::GetCurrent().Name)
    ('time_utc: ' + (Get-Date).ToUniversalTime().ToString('o'))
) -join [Environment]::NewLine

Write-Utf8NoBomFile -Path (Join-Path $ScaffoldDir 'hello.txt') -Content $helloText

$status = [pscustomobject]@{
    scenario      = 'scaffold'
    status        = 'passed'
    computer_name = $env:COMPUTERNAME
    generated_at  = (Get-Date).ToUniversalTime().ToString('o')
}

Write-Utf8NoBomFile -Path (Join-Path $OutputDir 'hello.json') -Content ($status | ConvertTo-Json -Depth 4)
