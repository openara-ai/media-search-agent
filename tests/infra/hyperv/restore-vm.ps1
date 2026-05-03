#Requires -Version 5.1
[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)][string] $VmName,
    [Parameter(Mandatory = $true)][string] $CheckpointName,
    [switch] $StartVm
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$vm = Get-VM -Name $VmName -ErrorAction Stop
$snapshot = Get-VMSnapshot -VMName $VmName -Name $CheckpointName -ErrorAction Stop

if ($vm.State -ne 'Off') {
    Stop-VM -Name $VmName -Force -TurnOff -ErrorAction Stop | Out-Null
}

Restore-VMSnapshot -VMName $VmName -Name $CheckpointName -Confirm:$false -ErrorAction Stop | Out-Null

if ($StartVm) {
    Start-VM -Name $VmName -ErrorAction Stop | Out-Null
}

[void]$snapshot
