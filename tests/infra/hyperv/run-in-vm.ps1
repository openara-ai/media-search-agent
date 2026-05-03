#Requires -Version 5.1
[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)][string] $VmName,
    [Parameter(Mandatory = $true)][pscredential] $Credential,
    [Parameter(Mandatory = $true)][string] $ScriptPath,
    [string[]] $ArgumentList = @()
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

function Write-StampedLine {
    param([Parameter(Mandatory = $true)][string] $Message)

    Write-Output ((Get-Date -Format 'HH:mm:ss') + ' ' + $Message)
}

Write-StampedLine -Message ("run-in-vm: starting " + $ScriptPath)

Invoke-Command -VMName $VmName -Credential $Credential -ScriptBlock {
    param($RemoteScriptPath, $RemoteArgs)

    if (-not (Test-Path -LiteralPath $RemoteScriptPath)) {
        throw "Guest script not found: $RemoteScriptPath"
    }

    & powershell.exe -NoProfile -NonInteractive -ExecutionPolicy Bypass -File $RemoteScriptPath @RemoteArgs
    if ($LASTEXITCODE -ne 0) {
        throw "Guest script failed with exit code ${LASTEXITCODE}: $RemoteScriptPath"
    }
} -ArgumentList $ScriptPath, $ArgumentList -ErrorAction Stop

Write-StampedLine -Message ("run-in-vm: completed " + $ScriptPath)
