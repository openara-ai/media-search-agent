#Requires -Version 5.1
[Diagnostics.CodeAnalysis.SuppressMessageAttribute('PSAvoidUsingPlainTextForPassword', 'GuestPassword', Justification = 'Local throwaway VM convenience parameter for WSL-driven Hyper-V tests.')]
[Diagnostics.CodeAnalysis.SuppressMessageAttribute('PSAvoidUsingUsernameAndPasswordParams', '', Justification = 'This helper supports the local-only guest username/password flow used by the WSL wrapper.')]
[Diagnostics.CodeAnalysis.SuppressMessageAttribute('PSAvoidUsingConvertToSecureStringWithPlainText', '', Justification = 'A runtime-supplied local test password is converted into PSCredential for PowerShell Direct only.')]
[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string] $VmName,

    [Parameter(Mandatory = $true)]
    [string] $CheckpointName,

    [ValidateSet('scaffold', 'installer')]
    [string] $Scenario = 'installer',

    [string] $InstallerPath = '',
    [string] $GuestWorkDir = 'C:\E2E',
    [string] $ArtifactsRoot = '',
    [pscredential] $GuestCredential,
    [string] $GuestUsername = '',
    [string] $GuestPassword = '',
    [switch] $RunPlaywright,
    [switch] $SkipCheckpointRestore,
    [switch] $KeepVmRunning
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$ScriptRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$RepoRoot = Split-Path -Parent (Split-Path -Parent $ScriptRoot)
$HyperVDir = Join-Path $ScriptRoot 'hyperv'
$GuestDir = Join-Path $ScriptRoot 'guest'
$PlaywrightDir = Join-Path (Split-Path -Parent $ScriptRoot) 'e2e'
$SummaryDir = Join-Path $ScriptRoot 'summary'

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

function Resolve-GuestCredential {
    param(
        [pscredential] $Credential,
        [string] $Username,
        [securestring] $Password
    )

    if ($Credential) {
        return $Credential
    }

    if (-not $Password -and $env:MSA_E2E_GUEST_PASSWORD) {
        $Password = ConvertTo-SecureString $env:MSA_E2E_GUEST_PASSWORD -AsPlainText -Force
    }

    if ($Username -and $Password) {
        return New-Object System.Management.Automation.PSCredential($Username, $Password)
    }

    if ($Username) {
        $secure = Read-Host -Prompt "Password for $Username" -AsSecureString
        return New-Object System.Management.Automation.PSCredential($Username, $secure)
    }

    return Get-Credential -Message 'Enter the guest VM credential'
}

function Get-RedactedArgumentString {
    param([string[]] $ArgumentValues)

    $result = New-Object System.Collections.Generic.List[string]
    for ($i = 0; $i -lt $ArgumentValues.Count; $i++) {
        $value = $ArgumentValues[$i]
        $result.Add($value)
        if ($value -eq '-GuestPassword' -and ($i + 1) -lt $ArgumentValues.Count) {
            $result.Add('<redacted>')
            $i += 1
        }
    }

    return ($result -join ' ')
}

function Resolve-InstallerPath {
    param([string] $PathHint)

    if ($PathHint) {
        return (Resolve-Path -Path $PathHint).Path
    }

    $defaultDir = Join-Path $RepoRoot 'dist\shell'
    if (-not (Test-Path $defaultDir)) {
        throw "Installer path not provided and default directory does not exist: $defaultDir"
    }

    $candidate = Get-ChildItem -Path $defaultDir -Filter 'MediaSearchAgent-*-windows-x86_64.zip' |
        Sort-Object LastWriteTimeUtc -Descending |
        Select-Object -First 1

    if (-not $candidate) {
        throw "No bundle found in $defaultDir matching MediaSearchAgent-*-windows-x86_64.zip"
    }

    return $candidate.FullName
}

function Add-StepResult {
    param(
        [string] $Name,
        [string] $Status,
        [string] $Detail
    )

    [void]$script:StepResults.Add([pscustomobject]@{
            name   = $Name
            status = $Status
            detail = $Detail
        })
}

function Write-StampedLine {
    param([Parameter(Mandatory = $true)][string] $Message)

    Write-Output ((Get-Date -Format 'HH:mm:ss') + ' ' + $Message)
}

if (-not $ArtifactsRoot) {
    $ArtifactsRoot = Join-Path $RepoRoot '.artifacts\local-e2e'
}

$timestamp = Get-Date -Format 'yyyyMMdd-HHmmss'
$RunDir = Join-Path $ArtifactsRoot $timestamp
$HostLogDir = Join-Path $RunDir 'host-logs'
$GuestOutputDir = Join-Path $RunDir 'guest-output'
$InstallerLogsDir = Join-Path $RunDir 'installer-logs'
$LaunchLogsDir = Join-Path $RunDir 'launch-logs'
$UninstallLogsDir = Join-Path $RunDir 'uninstall-logs'
$PlaywrightReportDir = Join-Path $RunDir 'playwright-report'
$PlaywrightResultsDir = Join-Path $RunDir 'playwright-test-results'
$HostStatusPath = Join-Path $RunDir 'host-status.json'
$HostTranscript = Join-Path $HostLogDir 'run-local.log'

New-Item -ItemType Directory -Force -Path $HostLogDir, $GuestOutputDir, $InstallerLogsDir, $LaunchLogsDir, $UninstallLogsDir, $PlaywrightReportDir, $PlaywrightResultsDir | Out-Null
Start-Transcript -Path $HostTranscript -Append | Out-Null
$invocationArgs = @()
if ($MyInvocation -and $MyInvocation.UnboundArguments) {
    $invocationArgs = @($MyInvocation.UnboundArguments)
}
Write-StampedLine -Message ('Invocation: ' + (Get-RedactedArgumentString -ArgumentValues $invocationArgs))

$StepResults = New-Object System.Collections.ArrayList
$runFailed = $false
$errorMessage = ''
$installAttempted = $false
$resolvedInstallerPath = ''
$GuestPasswordSecure = $null
if ($GuestPassword) {
    $GuestPasswordSecure = ConvertTo-SecureString $GuestPassword -AsPlainText -Force
}
$GuestCredential = Resolve-GuestCredential -Credential $GuestCredential -Username $GuestUsername -Password $GuestPasswordSecure

try {
    Add-StepResult -Name 'prepare' -Status 'passed' -Detail "Scenario: $Scenario"

    if (-not $SkipCheckpointRestore) {
        Write-StampedLine -Message "Step: restore-vm"
        & (Join-Path $HyperVDir 'restore-vm.ps1') -VmName $VmName -CheckpointName $CheckpointName -StartVm
        Add-StepResult -Name 'restore-vm' -Status 'passed' -Detail "Restored checkpoint $CheckpointName"
    } else {
        Write-StampedLine -Message "Step: restore-vm (skipped)"
        Add-StepResult -Name 'restore-vm' -Status 'skipped' -Detail 'Checkpoint restore skipped by caller'
    }

    Write-StampedLine -Message "Step: wait-vm"
    & (Join-Path $HyperVDir 'wait-vm.ps1') -VmName $VmName -Credential $GuestCredential
    Add-StepResult -Name 'wait-vm' -Status 'passed' -Detail 'PowerShell Direct ready'

    Write-StampedLine -Message "Step: copy-guest-payload"
    & (Join-Path $HyperVDir 'copy-to-vm.ps1') `
        -VmName $VmName `
        -Credential $GuestCredential `
        -SourcePath $GuestDir `
        -DestinationPath (Join-Path $GuestWorkDir 'guest')
    Add-StepResult -Name 'copy-guest-payload' -Status 'passed' -Detail 'Guest scripts copied'

    if ($Scenario -eq 'installer') {
        if ($RunPlaywright) {
            Write-StampedLine -Message "Step: copy-playwright-payload"
            & (Join-Path $HyperVDir 'copy-to-vm.ps1') `
                -VmName $VmName `
                -Credential $GuestCredential `
                -SourcePath $PlaywrightDir `
                -DestinationPath (Join-Path $GuestWorkDir 'playwright')
            Add-StepResult -Name 'copy-playwright-payload' -Status 'passed' -Detail 'Playwright package copied'
        }

        $resolvedInstallerPath = Resolve-InstallerPath -PathHint $InstallerPath
        $guestInstallerPath = Join-Path (Join-Path $GuestWorkDir 'input') (Split-Path -Leaf $resolvedInstallerPath)
        Write-StampedLine -Message ("Step: copy-installer -> " + (Split-Path -Leaf $resolvedInstallerPath))
        & (Join-Path $HyperVDir 'copy-to-vm.ps1') `
            -VmName $VmName `
            -Credential $GuestCredential `
            -SourcePath $resolvedInstallerPath `
            -DestinationPath $guestInstallerPath
        Add-StepResult -Name 'copy-installer' -Status 'passed' -Detail (Split-Path -Leaf $resolvedInstallerPath)

        # Shell-bundle installs run via installer/windows-native/shell/install.ps1
        # against the bundle .zip. Copy the bootstrap script alongside the bundle.
        $bootstrapSrc = Join-Path $RepoRoot 'installer\windows-native\shell\install.ps1'
        $guestBootstrapPath = Join-Path (Join-Path $GuestWorkDir 'input') 'install.ps1'
        Write-StampedLine -Message "Step: copy-bootstrap -> install.ps1"
        & (Join-Path $HyperVDir 'copy-to-vm.ps1') `
            -VmName $VmName `
            -Credential $GuestCredential `
            -SourcePath $bootstrapSrc `
            -DestinationPath $guestBootstrapPath
        Add-StepResult -Name 'copy-bootstrap' -Status 'passed' -Detail 'shell/install.ps1 copied'

        $installAttempted = $true
        Write-StampedLine -Message "Step: install-app"
        & (Join-Path $HyperVDir 'run-in-vm.ps1') `
            -VmName $VmName `
            -Credential $GuestCredential `
            -ScriptPath (Join-Path $GuestWorkDir 'guest\install-app.ps1') `
            -ArgumentList @(
                '-WorkDir', $GuestWorkDir,
                '-BundlePath', $guestInstallerPath,
                '-BootstrapPath', $guestBootstrapPath
            )
        Add-StepResult -Name 'install-app' -Status 'passed' -Detail 'Installer run completed'

        Write-StampedLine -Message "Step: launch-app"
        & (Join-Path $HyperVDir 'run-in-vm.ps1') `
            -VmName $VmName `
            -Credential $GuestCredential `
            -ScriptPath (Join-Path $GuestWorkDir 'guest\launch-app.ps1') `
            -ArgumentList @('-WorkDir', $GuestWorkDir)
        Add-StepResult -Name 'launch-app' -Status 'passed' -Detail 'Installed app launch completed'

        Write-StampedLine -Message "Step: smoke-test"
        & (Join-Path $HyperVDir 'run-in-vm.ps1') `
            -VmName $VmName `
            -Credential $GuestCredential `
            -ScriptPath (Join-Path $GuestWorkDir 'guest\run-smoke-test.ps1') `
            -ArgumentList @('-WorkDir', $GuestWorkDir, '-BaseUrl', 'http://127.0.0.1:8000')
        Add-StepResult -Name 'smoke-test' -Status 'passed' -Detail 'Health and root page checks passed'

        if ($RunPlaywright) {
            Write-StampedLine -Message "Step: playwright"
            & (Join-Path $HyperVDir 'run-in-vm.ps1') `
                -VmName $VmName `
                -Credential $GuestCredential `
                -ScriptPath (Join-Path $GuestWorkDir 'guest\run-playwright.ps1') `
                -ArgumentList @('-WorkDir', $GuestWorkDir, '-BaseUrl', 'http://127.0.0.1:8000')
            Add-StepResult -Name 'playwright' -Status 'passed' -Detail 'Playwright app-shell checks passed'
        }
    } else {
        Write-StampedLine -Message "Step: hello-world"
        & (Join-Path $HyperVDir 'run-in-vm.ps1') `
            -VmName $VmName `
            -Credential $GuestCredential `
            -ScriptPath (Join-Path $GuestWorkDir 'guest\hello.ps1') `
            -ArgumentList @('-WorkDir', $GuestWorkDir)
        Add-StepResult -Name 'hello-world' -Status 'passed' -Detail 'Scaffold run completed'
    }
} catch {
    $runFailed = $true
    $errorMessage = $_.Exception.Message
    Add-StepResult -Name 'run' -Status 'failed' -Detail $errorMessage
} finally {
    if ($Scenario -eq 'installer' -and $installAttempted) {
        try {
            Write-StampedLine -Message "Step: uninstall-app"
            & (Join-Path $HyperVDir 'run-in-vm.ps1') `
                -VmName $VmName `
                -Credential $GuestCredential `
                -ScriptPath (Join-Path $GuestWorkDir 'guest\uninstall-app.ps1') `
                -ArgumentList @('-WorkDir', $GuestWorkDir)
            Add-StepResult -Name 'uninstall-app' -Status 'passed' -Detail 'Silent uninstall completed'
        } catch {
            Add-StepResult -Name 'uninstall-app' -Status 'failed' -Detail $_.Exception.Message
            if (-not $runFailed) {
                $runFailed = $true
                $errorMessage = $_.Exception.Message
            }
        }
    }

    try {
        Write-StampedLine -Message "Step: collect-artifacts"
        & (Join-Path $HyperVDir 'collect-artifacts.ps1') `
            -VmName $VmName `
            -Credential $GuestCredential `
            -GuestArtifactsPath (Join-Path $GuestWorkDir 'output') `
            -HostDestinationPath $GuestOutputDir

        Get-ChildItem -Path $GuestOutputDir -Directory -ErrorAction SilentlyContinue | ForEach-Object {
            if ($_.Name -eq 'installer-logs') {
                Copy-Item -Path (Join-Path $_.FullName '*') -Destination $InstallerLogsDir -Recurse -Force -ErrorAction SilentlyContinue
            } elseif ($_.Name -eq 'launch-logs') {
                Copy-Item -Path (Join-Path $_.FullName '*') -Destination $LaunchLogsDir -Recurse -Force -ErrorAction SilentlyContinue
            } elseif ($_.Name -eq 'playwright-report') {
                Copy-Item -Path (Join-Path $_.FullName '*') -Destination $PlaywrightReportDir -Recurse -Force -ErrorAction SilentlyContinue
            } elseif ($_.Name -eq 'playwright-test-results') {
                Copy-Item -Path (Join-Path $_.FullName '*') -Destination $PlaywrightResultsDir -Recurse -Force -ErrorAction SilentlyContinue
            } elseif ($_.Name -eq 'uninstall-logs') {
                Copy-Item -Path (Join-Path $_.FullName '*') -Destination $UninstallLogsDir -Recurse -Force -ErrorAction SilentlyContinue
            }
        }

        Add-StepResult -Name 'collect-artifacts' -Status 'passed' -Detail $GuestOutputDir
    } catch {
        Add-StepResult -Name 'collect-artifacts' -Status 'failed' -Detail $_.Exception.Message
        if (-not $runFailed) {
            $runFailed = $true
            $errorMessage = $_.Exception.Message
        }
    }

    try {
        Write-StampedLine -Message "Step: validate-artifacts"
        $requiredArtifacts = @()
        if ($Scenario -eq 'installer') {
            $requiredArtifacts = @(
                (Join-Path $GuestOutputDir 'install.json'),
                (Join-Path $GuestOutputDir 'launch.json'),
                (Join-Path $GuestOutputDir 'smoke.json'),
                (Join-Path $GuestOutputDir 'uninstall.json')
            )
            if ($RunPlaywright) {
                $requiredArtifacts += (Join-Path $GuestOutputDir 'playwright.json')
            }
        } else {
            $requiredArtifacts = @(
                (Join-Path $GuestOutputDir 'hello.json')
            )
        }

        $missingArtifacts = @($requiredArtifacts | Where-Object { -not (Test-Path -LiteralPath $_) })
        if ($missingArtifacts.Count -gt 0) {
            $message = 'Missing required guest artifacts: ' + ($missingArtifacts -join ', ')
            Add-StepResult -Name 'validate-artifacts' -Status 'failed' -Detail $message
            if (-not $runFailed) {
                $runFailed = $true
                $errorMessage = $message
            }
        } else {
            Add-StepResult -Name 'validate-artifacts' -Status 'passed' -Detail 'Required guest artifacts present'
        }
    } catch {
        Add-StepResult -Name 'validate-artifacts' -Status 'failed' -Detail $_.Exception.Message
        if (-not $runFailed) {
            $runFailed = $true
            $errorMessage = $_.Exception.Message
        }
    }

    if (-not $KeepVmRunning) {
        try {
            Write-StampedLine -Message "Step: stop-vm"
            Stop-VM -Name $VmName -Force -TurnOff -ErrorAction Stop | Out-Null
            Add-StepResult -Name 'stop-vm' -Status 'passed' -Detail 'VM stopped after run'
        } catch {
            Add-StepResult -Name 'stop-vm' -Status 'failed' -Detail $_.Exception.Message
        }
    } else {
        Add-StepResult -Name 'stop-vm' -Status 'skipped' -Detail 'VM left running by caller'
    }

    $hostStatus = [pscustomobject]@{
        scenario      = $Scenario
        vm_name       = $VmName
        checkpoint    = $CheckpointName
        installer     = $resolvedInstallerPath
        run_playwright = [bool]$RunPlaywright
        run_dir       = $RunDir
        started_at    = $timestamp
        succeeded     = (-not $runFailed)
        error_message = $errorMessage
        steps         = $StepResults
    }
    Write-Utf8NoBomFile -Path $HostStatusPath -Content ($hostStatus | ConvertTo-Json -Depth 6)

    & (Join-Path $SummaryDir 'write-summary.ps1') `
        -RunDirectory $RunDir `
        -HostStatusPath $HostStatusPath

    Stop-Transcript | Out-Null
}

if ($runFailed) {
    Write-Error "Local E2E run failed. Summary: $(Join-Path $RunDir 'summary.md')"
    exit 1
}

Write-StampedLine -Message "Local E2E run completed. Summary: $(Join-Path $RunDir 'summary.md')"
