#Requires -Version 5.1
[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)][string] $VmName,
    [Parameter(Mandatory = $true)][pscredential] $Credential,
    [int] $TimeoutSeconds = 300,
    [int] $PollSeconds = 5,
    [int] $MaxAuthFailures = 2
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$deadline = (Get-Date).AddSeconds($TimeoutSeconds)
$lastError = ''
$authFailures = 0

function Test-IsCredentialFailure {
    param([string] $Message)

    $patterns = @(
        'password is incorrect',
        'user name or password is incorrect',
        'logon failure',
        'account is currently locked out',
        'locked out',
        'unknown user name or bad password'
    )

    foreach ($pattern in $patterns) {
        if ($Message -match $pattern) {
            return $true
        }
    }

    return $false
}

while ((Get-Date) -lt $deadline) {
    try {
        $result = Invoke-Command -VMName $VmName -Credential $Credential -ScriptBlock {
            [pscustomobject]@{
                computer_name = $env:COMPUTERNAME
                user_name     = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name
            }
        } -ErrorAction Stop

        if ($result) {
            return $result
        }
    } catch {
        $lastError = $_.Exception.Message
        if (Test-IsCredentialFailure -Message $lastError) {
            $authFailures += 1
            if ($authFailures -ge $MaxAuthFailures) {
                throw "Credential validation failed for $VmName after $authFailures attempts. Last error: $lastError"
            }
        }
    }

    Start-Sleep -Seconds $PollSeconds
}

throw "Timed out waiting for PowerShell Direct on $VmName. Last error: $lastError"
