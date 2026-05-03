#Requires -Version 5.1
[CmdletBinding()]
param(
    [string] $WorkDir = 'C:\E2E',
    [string] $BaseUrl = 'http://127.0.0.1:8000'
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

function Write-Utf8NoBomFile {
    param(
        [Parameter(Mandatory = $true)][string] $Path,
        [AllowEmptyString()][string] $Content = ''
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

function Initialize-CommandPath {
    $pathSegments = New-Object System.Collections.Generic.List[string]

    foreach ($scope in @('Process', 'Machine', 'User')) {
        $raw = [Environment]::GetEnvironmentVariable('Path', $scope)
        if (-not $raw) {
            continue
        }

        foreach ($segment in ($raw -split ';')) {
            if (-not $segment) {
                continue
            }
            if (-not $pathSegments.Contains($segment)) {
                [void]$pathSegments.Add($segment)
            }
        }
    }

    $env:Path = ($pathSegments -join ';')
}

function Resolve-CommandPath {
    param([Parameter(Mandatory = $true)][string] $Name)

    $command = Get-Command $Name -ErrorAction SilentlyContinue
    if ($command -and $command.Source) {
        return $command.Source
    }

    throw "Required command not found on PATH: $Name"
}

function Invoke-LoggedProcess {
    param(
        [Parameter(Mandatory = $true)][string] $FilePath,
        [Parameter(Mandatory = $true)][string[]] $ArgumentList,
        [Parameter(Mandatory = $true)][string] $WorkingDirectory,
        [Parameter(Mandatory = $true)][string] $StdoutPath,
        [Parameter(Mandatory = $true)][string] $StderrPath,
        [Parameter(Mandatory = $true)][int] $TimeoutMilliseconds,
        [Parameter(Mandatory = $true)][string] $ActionName,
        [Parameter(Mandatory = $true)][string] $ActionLog
    )

    Write-LogLine -Path $ActionLog -Message ("Starting " + $ActionName)
    $escapedArgs = @()
    foreach ($arg in $ArgumentList) {
        $escapedArgs += ('"' + ($arg -replace '"', '""') + '"')
    }

    $commandLine = '"' + ('"' + $FilePath + '" ' + ($escapedArgs -join ' ')) + '"'

    # Redirect stdout/stderr directly to files via Start-Process.  Using
    # ProcessStartInfo with RedirectStandardOutput=true creates OS pipes whose
    # buffers (~4-64 KB) fill when npm ci or playwright test emit large output;
    # WaitForExit() blocks while the child blocks on the full pipe -- deadlock.
    # File-based redirection sidesteps pipe buffers entirely.
    $proc = Start-Process `
        -FilePath 'cmd.exe' `
        -ArgumentList ('/d /c ' + $commandLine) `
        -WorkingDirectory $WorkingDirectory `
        -RedirectStandardOutput $StdoutPath `
        -RedirectStandardError $StderrPath `
        -NoNewWindow `
        -PassThru

    if (-not $proc.WaitForExit($TimeoutMilliseconds)) {
        Write-LogLine -Path $ActionLog -Message ($ActionName + ' timed out; sending Kill()')
        try {
            $proc.Kill()
        } catch {
            Write-LogLine -Path $ActionLog -Message ($ActionName + ' kill attempt failed: ' + $_.Exception.Message)
        }
        throw ($ActionName + ' timed out')
    }

    $exitCode = $proc.ExitCode
    Write-LogLine -Path $ActionLog -Message ($ActionName + ' exit code: ' + $exitCode)
    return [int]$exitCode
}

$OutputDir = Join-Path $WorkDir 'output'
$PlaywrightDir = Join-Path $WorkDir 'playwright'
$PlaywrightOutputDir = Join-Path $OutputDir 'playwright'
$ActionLog = Join-Path $PlaywrightOutputDir 'run-playwright.log'
$HtmlReportDir = Join-Path $OutputDir 'playwright-report'
$ResultsDir = Join-Path $OutputDir 'playwright-test-results'
$JsonReportPath = Join-Path $OutputDir 'playwright-results.json'
$JUnitReportPath = Join-Path $OutputDir 'playwright-junit.xml'

New-Item -ItemType Directory -Force -Path $PlaywrightOutputDir, $HtmlReportDir, $ResultsDir | Out-Null

if (-not (Test-Path -LiteralPath (Join-Path $PlaywrightDir 'package.json'))) {
    throw "Playwright package not found: $PlaywrightDir"
}

if (-not (Test-Path -LiteralPath (Join-Path $PlaywrightDir 'package-lock.json'))) {
    throw "Playwright package-lock.json not found: $PlaywrightDir"
}

$env:E2E_BASE_URL = $BaseUrl
$env:PLAYWRIGHT_HTML_REPORT = $HtmlReportDir
$env:PLAYWRIGHT_TEST_RESULTS_DIR = $ResultsDir
$env:PLAYWRIGHT_JSON_REPORT = $JsonReportPath
$env:PLAYWRIGHT_JUNIT_REPORT = $JUnitReportPath

Initialize-CommandPath
$npmCmd = Resolve-CommandPath -Name 'npm.cmd'
$npxCmd = Resolve-CommandPath -Name 'npx.cmd'

$status = [ordered]@{
    status          = 'failed'
    base_url        = $BaseUrl
    package_dir     = $PlaywrightDir
    html_report_dir = $HtmlReportDir
    results_dir     = $ResultsDir
    json_report     = $JsonReportPath
    junit_report    = $JUnitReportPath
    completed_at    = $null
}

try {
    $npmStdout = Join-Path $PlaywrightOutputDir 'npm-ci.stdout.log'
    $npmStderr = Join-Path $PlaywrightOutputDir 'npm-ci.stderr.log'
    Write-LogLine -Path $ActionLog -Message ('Resolved npm.cmd: ' + $npmCmd)
    $npmExit = Invoke-LoggedProcess `
        -FilePath $npmCmd `
        -ArgumentList @('ci', '--no-audit', '--no-fund') `
        -WorkingDirectory $PlaywrightDir `
        -StdoutPath $npmStdout `
        -StderrPath $npmStderr `
        -TimeoutMilliseconds 600000 `
        -ActionName 'npm ci' `
        -ActionLog $ActionLog

    if ($npmExit -ne 0) {
        throw "npm ci failed with exit code $npmExit"
    }

    $pwStdout = Join-Path $PlaywrightOutputDir 'playwright.stdout.log'
    $pwStderr = Join-Path $PlaywrightOutputDir 'playwright.stderr.log'
    Write-LogLine -Path $ActionLog -Message ('Resolved npx.cmd: ' + $npxCmd)
    $pwExit = Invoke-LoggedProcess `
        -FilePath $npxCmd `
        -ArgumentList @('playwright', 'test') `
        -WorkingDirectory $PlaywrightDir `
        -StdoutPath $pwStdout `
        -StderrPath $pwStderr `
        -TimeoutMilliseconds 600000 `
        -ActionName 'playwright test' `
        -ActionLog $ActionLog

    if ($pwExit -ne 0) {
        throw "playwright test failed with exit code $pwExit"
    }

    $status.status = 'passed'
} catch {
    $status.error_message = $_.Exception.Message
    throw
} finally {
    $status.completed_at = (Get-Date).ToUniversalTime().ToString('o')
    Write-Utf8NoBomFile -Path (Join-Path $OutputDir 'playwright.json') -Content (($status | ConvertTo-Json -Depth 4))
}
