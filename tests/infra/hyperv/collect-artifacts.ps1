#Requires -Version 5.1
[Diagnostics.CodeAnalysis.SuppressMessageAttribute('PSUseUsingScopeModifierInNewRunspaces', '', Justification = 'The remoting script block receives values via param/ArgumentList, not closure capture.')]
[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)][string] $VmName,
    [Parameter(Mandatory = $true)][pscredential] $Credential,
    [Parameter(Mandatory = $true)][string] $GuestArtifactsPath,
    [Parameter(Mandatory = $true)][string] $HostDestinationPath
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
New-Item -ItemType Directory -Force -Path $HostDestinationPath | Out-Null
$session = New-PSSession -VMName $VmName -Credential $Credential -ErrorAction Stop

try {
    $exists = Invoke-Command -Session $session -ScriptBlock {
        param($ArtifactPath)
        Test-Path -LiteralPath $ArtifactPath
    } -ArgumentList $GuestArtifactsPath -ErrorAction Stop

    if (-not $exists) {
        throw "Guest artifacts path not found: $GuestArtifactsPath"
    }

    Get-ChildItem -Path $HostDestinationPath -Force -ErrorAction SilentlyContinue |
        Remove-Item -Recurse -Force -ErrorAction SilentlyContinue

    Copy-Item -FromSession $session -Path (Join-Path $GuestArtifactsPath '*') -Destination $HostDestinationPath -Recurse -Force -ErrorAction Stop
} finally {
    if ($session) {
        Remove-PSSession -Session $session -ErrorAction SilentlyContinue
    }
}
