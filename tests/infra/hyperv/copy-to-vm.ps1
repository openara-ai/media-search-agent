#Requires -Version 5.1
[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)][string] $VmName,
    [Parameter(Mandatory = $true)][pscredential] $Credential,
    [Parameter(Mandatory = $true)][string] $SourcePath,
    [Parameter(Mandatory = $true)][string] $DestinationPath
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

function Assert-GuestServiceInterfaceEnabled {
    param([string] $TargetVmName)

    $service = Get-VMIntegrationService -VMName $TargetVmName -Name 'Guest Service Interface' -ErrorAction Stop
    if (-not $service.Enabled) {
        throw "Hyper-V Guest Service Interface is disabled for $TargetVmName. Enable it before running E2E copy steps."
    }
}

function New-HostZipPayload {
    [CmdletBinding(SupportsShouldProcess = $true)]
    param([string] $InputPath)

    $resolved = (Resolve-Path -Path $InputPath).Path
    $sourceItem = Get-Item -LiteralPath $resolved -ErrorAction Stop
    $stagingDir = Join-Path ([System.IO.Path]::GetTempPath()) ("msa-e2e-stage-" + [System.Guid]::NewGuid().ToString('N'))
    $zipPath = Join-Path ([System.IO.Path]::GetTempPath()) ("msa-e2e-payload-" + [System.Guid]::NewGuid().ToString('N') + '.zip')

    New-Item -ItemType Directory -Force -Path $stagingDir | Out-Null

    if ($sourceItem.PSIsContainer) {
        Copy-Item -LiteralPath $resolved -Destination $stagingDir -Recurse -Force
        $payloadName = $sourceItem.Name
    } else {
        Copy-Item -LiteralPath $resolved -Destination (Join-Path $stagingDir $sourceItem.Name) -Force
        $payloadName = $sourceItem.Name
    }

    Compress-Archive -Path (Join-Path $stagingDir '*') -DestinationPath $zipPath -Force

    return [pscustomobject]@{
        zip_path     = $zipPath
        staging_dir  = $stagingDir
        payload_name = $payloadName
        is_dir       = $sourceItem.PSIsContainer
    }
}

Assert-GuestServiceInterfaceEnabled -TargetVmName $VmName

$destinationParent = Split-Path -Parent $DestinationPath
$destinationLeaf = Split-Path -Leaf $DestinationPath
$guestZipPath = $DestinationPath + '.zip'
$payload = $null

try {
    $payload = New-HostZipPayload -InputPath $SourcePath
    Copy-VMFile -Name $VmName -SourcePath $payload.zip_path -DestinationPath $guestZipPath -CreateFullPath -FileSource Host -ErrorAction Stop

    Invoke-Command -VMName $VmName -Credential $Credential -ScriptBlock {
        param($ExpandZipPath, $ExpandDestinationParent, $ExpandDestinationLeaf, $PayloadName)

        if ($ExpandDestinationParent) {
            New-Item -ItemType Directory -Force -Path $ExpandDestinationParent | Out-Null
        }

        Expand-Archive -Path $ExpandZipPath -DestinationPath $ExpandDestinationParent -Force
        Remove-Item -LiteralPath $ExpandZipPath -Force -ErrorAction SilentlyContinue

        $extractedPath = Join-Path $ExpandDestinationParent $PayloadName
        $finalPath = Join-Path $ExpandDestinationParent $ExpandDestinationLeaf

        if ($extractedPath -ne $finalPath) {
            if (Test-Path -LiteralPath $finalPath) {
                Remove-Item -LiteralPath $finalPath -Recurse -Force
            }
            Move-Item -LiteralPath $extractedPath -Destination $finalPath -Force
        }
    } -ArgumentList $guestZipPath, $destinationParent, $destinationLeaf, $payload.payload_name -ErrorAction Stop
} finally {
    if ($payload) {
        Remove-Item -LiteralPath $payload.zip_path -Force -ErrorAction SilentlyContinue
        Remove-Item -LiteralPath $payload.staging_dir -Recurse -Force -ErrorAction SilentlyContinue
    }
}
