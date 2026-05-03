#Requires -Version 5.1
<#
.SYNOPSIS
    Media Search Agent  - Windows Native start script.

.DESCRIPTION
    Activates the MSA venv and starts the FastAPI backend. Opens the browser
    once the health endpoint becomes ready.

    Safe to run multiple times  - detects if the app is already running.

.PARAMETER AppDir
    App internals directory containing the .venv and logs.
    Default: %LOCALAPPDATA%\MediaSearchAgent

.PARAMETER DataDir
    User data directory containing config.yaml.
    Default: %USERPROFILE%\MediaSearchAgent

.PARAMETER ConfigFile
    Path to config.yaml.
    Default: %USERPROFILE%\MediaSearchAgent\config.yaml

.PARAMETER Port
    API port. Default: 8000

.PARAMETER BindHost
    Host address uvicorn binds to. Default: 127.0.0.1 (loopback only).
    Set to 0.0.0.0 to accept connections from other machines on the network
    (e.g. a VM or another device). Requires a Windows Firewall inbound rule
    for TCP port 8000 when binding to 0.0.0.0.

.PARAMETER NoBrowser
    Do not open the browser automatically.

.PARAMETER NoPrewarm
    Skip model pre-warming (faster startup, slower first search).
    Reserved for future use  - passed through to the API process.

.PARAMETER Help
    Show this help message and exit.

.EXAMPLE
    .\start.ps1
    Start normally, binding to localhost only and opening the browser.

.EXAMPLE
    .\start.ps1 -BindHost 0.0.0.0
    Bind to all interfaces so other machines (e.g. a VM) can reach port 8000.
    Requires a Windows Firewall inbound rule for TCP 8000.

.EXAMPLE
    .\start.ps1 -NoBrowser -Port 8080
    Start on port 8080 without opening the browser.
#>
[Diagnostics.CodeAnalysis.SuppressMessageAttribute('PSAvoidUsingWriteHost', '',
    Justification='Interactive start script  - colored console output is intentional.')]
[Diagnostics.CodeAnalysis.SuppressMessageAttribute('PSReviewUnusedParameter', 'NoPrewarm',
    Justification='Reserved for future use; will be forwarded to uvicorn/API in a later phase.')]
[Diagnostics.CodeAnalysis.SuppressMessageAttribute('PSUseShouldProcessForStateChangingFunctions', '',
    Justification='Launcher helper functions update transient UI state only; no external state is modified.')]
[CmdletBinding()]
param(
    [string] $AppDir     = "$env:LOCALAPPDATA\MediaSearchAgent",
    [string] $DataDir    = "$env:USERPROFILE\MediaSearchAgent",
    [string] $ConfigFile = "",
    [int]    $Port       = 8000,
    [string] $BindHost   = "127.0.0.1",
    [switch] $NoBrowser,
    [switch] $NoPrewarm,
    [switch] $Help
)

if ($Help) {
    Write-Host "Usage: .\start.ps1 [options]"
    Write-Host ""
    Write-Host "Starts the Media Search Agent API and opens the browser when ready."
    Write-Host ""
    Write-Host "Options:"
    Write-Host "  -AppDir <path>      App internals dir (.venv, logs). Default: %LOCALAPPDATA%\MediaSearchAgent"
    Write-Host "  -DataDir <path>     User data dir (config.yaml). Default: %USERPROFILE%\MediaSearchAgent"
    Write-Host "  -ConfigFile <path>  Path to config.yaml. Default: <DataDir>\config.yaml"
    Write-Host "  -Port <int>         API port. Default: 8000"
    Write-Host "  -BindHost <addr>    Host uvicorn binds to. Default: 127.0.0.1 (localhost only)."
    Write-Host "                      Use 0.0.0.0 to accept connections from other machines on"
    Write-Host "                      the network (e.g. a VM). Requires a Windows Firewall rule"
    Write-Host "                      allowing inbound TCP on the chosen port."
    Write-Host "  -NoBrowser          Do not open the browser automatically."
    Write-Host "  -NoPrewarm          Skip ML model pre-warming (faster start; first search slower)."
    Write-Host "  -Help               Show this help message and exit."
    Write-Host ""
    Write-Host "Examples:"
    Write-Host "  .\start.ps1"
    Write-Host "  .\start.ps1 -BindHost 0.0.0.0"
    Write-Host "  .\start.ps1 -NoBrowser -Port 8080"
    exit 0
}

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$VenvDir    = "$AppDir\.venv"
$LogDir     = "$AppDir\logs"
$UvicornExe = "$VenvDir\Scripts\uvicorn.exe"
$AppUrl     = "http://127.0.0.1:$Port"
$LaunchUrl  = "$AppUrl/search?launch=1"
$HealthUrl  = "$AppUrl/health"
$MaxWaitSec = 180
$PollSec    = 2

if (-not $ConfigFile) { $ConfigFile = "$DataDir\config.yaml" }

New-Item -ItemType Directory -Force -Path $LogDir | Out-Null
$LogFile = "$LogDir\launch-$(Get-Date -Format 'yyyy-MM-dd_HHmmss').log"

function Write-MsaLog { param([string]$Msg)
    $line = "$(Get-Date -Format 'HH:mm:ss') $Msg"
    Write-Host $line
    Add-Content -Path $LogFile -Value $line
}

$script:WinFormsAvailable = $false
if ($host.Name -notmatch 'ServerRemoteHost') {
    try {
        Add-Type -AssemblyName System.Drawing -ErrorAction Stop
    } catch {
        Write-Verbose "System.Drawing not available; continuing without startup window enhancements. $_"
    }
}
try {
    Add-Type -AssemblyName System.Windows.Forms -ErrorAction Stop
    $script:WinFormsAvailable = $true
} catch {
    # System.Windows.Forms may be unavailable on Server Core / headless environments.
    # Toast notifications are optional; the launcher continues without them.
    Write-Verbose "System.Windows.Forms not available; toast notifications disabled. $_"
}

function Show-Toast { param([string]$Title, [string]$Text)
    if (-not $script:WinFormsAvailable) { return }
    try {
        $notify = New-Object System.Windows.Forms.NotifyIcon
        $notify.Icon    = [System.Drawing.SystemIcons]::Information
        $notify.Visible = $true
        $notify.ShowBalloonTip(5000, $Title, $Text, [System.Windows.Forms.ToolTipIcon]::Info)
        Start-Sleep -Seconds 1
        $notify.Dispose()
    } catch {
        # Toast notifications are optional; ignore failures (e.g. headless environments)
        Write-Verbose "Toast notification failed: $_"
    }
}

$script:StartupForm        = $null
$script:StartupStatusLabel = $null
$script:StartupDetailLabel = $null
$script:StartupElapsedLabel = $null
$script:StartupProgressBar = $null
$script:StartupLogsButton  = $null

function Open-LogFile {
    param([string]$Path)

    if (-not (Test-Path $Path)) { return }
    try {
        Start-Process "explorer.exe" -ArgumentList "/select,`"$Path`""
    } catch {
        Write-Verbose "Could not open log file in Explorer: $_"
    }
}

function Show-StartupWindow {
    param(
        [string] $Status,
        [string] $Detail
    )

    if (-not $script:WinFormsAvailable) { return }
    if ($script:StartupForm) { return }

    try {
        $form = New-Object System.Windows.Forms.Form
        $form.Text = "Media Search Agent"
        $form.StartPosition = [System.Windows.Forms.FormStartPosition]::CenterScreen
        $form.Size = New-Object System.Drawing.Size(460, 285)
        $form.MinimumSize = New-Object System.Drawing.Size(460, 285)
        $form.MaximumSize = New-Object System.Drawing.Size(460, 285)
        $form.FormBorderStyle = [System.Windows.Forms.FormBorderStyle]::FixedDialog
        $form.MaximizeBox = $false
        $form.MinimizeBox = $false
        $form.TopMost = $true

        $titleLabel = New-Object System.Windows.Forms.Label
        $titleLabel.Location = New-Object System.Drawing.Point(20, 18)
        $titleLabel.Size = New-Object System.Drawing.Size(400, 24)
        $titleLabel.Font = New-Object System.Drawing.Font("Segoe UI", 12, [System.Drawing.FontStyle]::Bold)
        $titleLabel.Text = "Starting Media Search Agent..."

        $statusLabel = New-Object System.Windows.Forms.Label
        $statusLabel.Location = New-Object System.Drawing.Point(20, 56)
        $statusLabel.Size = New-Object System.Drawing.Size(400, 24)
        $statusLabel.Font = New-Object System.Drawing.Font("Segoe UI", 10, [System.Drawing.FontStyle]::Regular)
        $statusLabel.Text = $Status

        $detailLabel = New-Object System.Windows.Forms.Label
        $detailLabel.Location = New-Object System.Drawing.Point(20, 84)
        $detailLabel.Size = New-Object System.Drawing.Size(400, 48)
        $detailLabel.Font = New-Object System.Drawing.Font("Segoe UI", 9, [System.Drawing.FontStyle]::Regular)
        $detailLabel.Text = $Detail

        $progressBar = New-Object System.Windows.Forms.ProgressBar
        $progressBar.Location = New-Object System.Drawing.Point(20, 138)
        $progressBar.Size = New-Object System.Drawing.Size(400, 18)
        $progressBar.Style = [System.Windows.Forms.ProgressBarStyle]::Marquee
        $progressBar.MarqueeAnimationSpeed = 25

        $elapsedLabel = New-Object System.Windows.Forms.Label
        $elapsedLabel.Location = New-Object System.Drawing.Point(20, 166)
        $elapsedLabel.Size = New-Object System.Drawing.Size(220, 20)
        $elapsedLabel.Font = New-Object System.Drawing.Font("Segoe UI", 9, [System.Drawing.FontStyle]::Regular)
        $elapsedLabel.Text = "Elapsed: 0s"

        $logsButton = New-Object System.Windows.Forms.Button
        $logsButton.Location = New-Object System.Drawing.Point(20, 198)
        $logsButton.Size = New-Object System.Drawing.Size(92, 28)
        $logsButton.Text = "Open Logs"
        $logsButton.Add_Click({
            Open-LogFile -Path $LogFile
        })

        $hintLabel = New-Object System.Windows.Forms.Label
        $hintLabel.Location = New-Object System.Drawing.Point(128, 196)
        $hintLabel.Size = New-Object System.Drawing.Size(292, 42)
        $hintLabel.Font = New-Object System.Drawing.Font("Segoe UI", 8, [System.Drawing.FontStyle]::Regular)
        $hintLabel.Text = "This app runs locally on your computer.`r`nYour browser will open automatically."

        [void]$form.Controls.Add($titleLabel)
        [void]$form.Controls.Add($statusLabel)
        [void]$form.Controls.Add($detailLabel)
        [void]$form.Controls.Add($progressBar)
        [void]$form.Controls.Add($elapsedLabel)
        [void]$form.Controls.Add($logsButton)
        [void]$form.Controls.Add($hintLabel)

        $form.Add_Shown({ $this.Activate() })
        [void]$form.Show()
        [System.Windows.Forms.Application]::DoEvents()

        $script:StartupForm = $form
        $script:StartupStatusLabel = $statusLabel
        $script:StartupDetailLabel = $detailLabel
        $script:StartupElapsedLabel = $elapsedLabel
        $script:StartupProgressBar = $progressBar
        $script:StartupLogsButton = $logsButton
    } catch {
        Write-Verbose "Could not show startup window: $_"
        $script:StartupForm = $null
    }
}

function Update-StartupWindow {
    param(
        [string] $Status,
        [string] $Detail,
        [int]    $ElapsedSeconds = -1
    )

    if (-not $script:StartupForm) { return }

    try {
        if ($Status) { $script:StartupStatusLabel.Text = $Status }
        if ($Detail) { $script:StartupDetailLabel.Text = $Detail }
        if ($ElapsedSeconds -ge 0) {
            $script:StartupElapsedLabel.Text = "Elapsed: ${ElapsedSeconds}s"
        }
        [System.Windows.Forms.Application]::DoEvents()
    } catch {
        Write-Verbose "Could not update startup window: $_"
    }
}

function Close-StartupWindow {
    if (-not $script:StartupForm) { return }

    try {
        $script:StartupForm.Close()
        $script:StartupForm.Dispose()
        [System.Windows.Forms.Application]::DoEvents()
    } catch {
        Write-Verbose "Could not close startup window: $_"
    } finally {
        $script:StartupForm = $null
        $script:StartupStatusLabel = $null
        $script:StartupDetailLabel = $null
        $script:StartupElapsedLabel = $null
        $script:StartupProgressBar = $null
        $script:StartupLogsButton = $null
    }
}

function Wait-WithUiPump {
    param(
        [int] $Milliseconds
    )

    if ($Milliseconds -le 0) { return }

    $interval = 100
    $remaining = $Milliseconds

    while ($remaining -gt 0) {
        $sleepMs = [Math]::Min($interval, $remaining)
        Start-Sleep -Milliseconds $sleepMs
        if ($script:StartupForm) {
            try {
                [System.Windows.Forms.Application]::DoEvents()
            } catch {
                Write-Verbose "Could not pump startup window events: $_"
            }
        }
        $remaining -= $sleepMs
    }
}

function Test-AppRunning {
    try {
        $req = [System.Net.WebRequest]::Create($HealthUrl)
        $req.Method  = "GET"
        $req.Timeout = 2000
        $resp = $req.GetResponse()
        $ok = ([int]$resp.StatusCode -eq 200)
        $resp.Close()
        return $ok
    } catch { return $false }
}

function Open-Browser {
    try { Start-Process $LaunchUrl } catch {
        Write-Verbose "Could not open browser: $_"
    }
}

# -- Validate setup ------------------------------------------------------------

if (-not (Test-Path $UvicornExe)) {
    Write-Host "ERROR: Venv not found at $VenvDir" -ForegroundColor Red
    Write-Host "Re-run the installer to repair." -ForegroundColor Red
    exit 1
}

if (-not (Test-Path $ConfigFile)) {
    Write-Host "ERROR: Config not found at $ConfigFile" -ForegroundColor Red
    Write-Host "Edit $DataDir\config.yaml to add your media sources." -ForegroundColor Red
    exit 1
}

# -- Already running? ----------------------------------------------------------

Write-MsaLog "Media Search Agent starting. Log: $LogFile"

if (Test-AppRunning) {
    Write-MsaLog "Already running  - opening browser."
    Show-Toast "Media Search Agent" "Already running  - opening browser."
    if (-not $NoBrowser) { Open-Browser }
    exit 0
}

# -- Start the API -------------------------------------------------------------

$env:MSA_CONFIG_PATH = $ConfigFile
$env:MSA_DATA_DIR    = $DataDir
$env:MSA_LOG_DIR     = $LogDir
$env:MSA_CACHE_DIR   = "$AppDir\Cache"

$uvicornArgs = @(
    "msa_apps.search_api.app:app",
    "--host", $BindHost,
    "--port", "$Port",
    "--log-level", "info"
)

$UvicornLog = "$LogDir\uvicorn.log"
$UvicornErr = "$LogDir\uvicorn-err.log"

Write-MsaLog "Starting uvicorn on port $Port..."
Write-MsaLog "Uvicorn log: $UvicornLog"
Show-StartupWindow `
    -Status "Starting local services..." `
    -Detail "First launch may take 30-60 seconds. Media Search Agent will open in your browser automatically when ready."
Show-Toast "Media Search Agent" "Starting... this may take 30-60 seconds on first launch."

# Start uvicorn as a detached hidden process, capturing stdout+stderr so
# any startup crash is visible in the log rather than silently lost.
$proc = Start-Process `
    -FilePath $UvicornExe `
    -ArgumentList $uvicornArgs `
    -WorkingDirectory $AppDir `
    -WindowStyle Hidden `
    -RedirectStandardOutput $UvicornLog `
    -RedirectStandardError  $UvicornErr `
    -PassThru

Write-MsaLog "uvicorn started (PID $($proc.Id))"
Update-StartupWindow `
    -Status "Waiting for the app to become ready..." `
    -Detail "The server is starting on $AppUrl. You do not need to do anything."

# -- Poll health endpoint ------------------------------------------------------

$elapsed     = 0
$browserDone = $false

while ($elapsed -lt $MaxWaitSec) {
    Wait-WithUiPump -Milliseconds ($PollSec * 1000)
    $elapsed += $PollSec
    if ($elapsed -ge 30) {
        Update-StartupWindow `
            -Status "Still starting..." `
            -Detail "This app runs locally on your computer. First launch can take longer while models load from disk." `
            -ElapsedSeconds $elapsed
    } else {
        Update-StartupWindow `
            -Status "Waiting for the app to become ready..." `
            -Detail "The server is starting on $AppUrl. Your browser will open automatically when it is ready." `
            -ElapsedSeconds $elapsed
    }

    # Detect early crash - if the process exited before the health check passes,
    # report the error log so the user knows what went wrong.
    if ($proc.HasExited) {
        $exitCode = $proc.ExitCode
        Write-MsaLog "ERROR: uvicorn exited early (exit code $exitCode)."
        Write-MsaLog "Check logs for details:"
        Write-MsaLog "  $UvicornLog"
        Write-MsaLog "  $UvicornErr"
        # Print the last few lines of stderr to the launch log for immediate context
        if (Test-Path $UvicornErr) {
            $tail = Get-Content $UvicornErr -Tail 20 -ErrorAction SilentlyContinue
            if ($tail) {
                Write-MsaLog "--- uvicorn stderr (last 20 lines) ---"
                $tail | ForEach-Object { Write-MsaLog "  $_" }
                Write-MsaLog "--------------------------------------"
            }
        }
        Update-StartupWindow `
            -Status "Startup failed" `
            -Detail "Media Search Agent could not finish starting. Use Open Logs for details." `
            -ElapsedSeconds $elapsed
        Show-Toast "Media Search Agent" "Startup failed (exit $exitCode). Check $UvicornErr"
        if ($script:StartupForm) {
            [void][System.Windows.Forms.MessageBox]::Show(
                "Media Search Agent could not finish starting.`r`n`r`nOpen Logs to review $UvicornErr",
                "Media Search Agent",
                [System.Windows.Forms.MessageBoxButtons]::OK,
                [System.Windows.Forms.MessageBoxIcon]::Error
            )
        }
        Close-StartupWindow
        exit 1
    }

    if (Test-AppRunning) {
        Write-MsaLog "App ready after ${elapsed}s."
        Update-StartupWindow `
            -Status "Ready" `
            -Detail "Media Search Agent is ready. Opening your browser..." `
            -ElapsedSeconds $elapsed
        Show-Toast "Media Search Agent" "Ready! Opening browser..."
        Start-Sleep -Milliseconds 300
        Close-StartupWindow
        if (-not $NoBrowser -and -not $browserDone) {
            Open-Browser
            $browserDone = $true
        }
        exit 0
    }
}

Write-MsaLog "Timed out after ${MaxWaitSec}s waiting for health check."
Write-MsaLog "Uvicorn may still be starting, or may have crashed. Check: $UvicornErr"
Update-StartupWindow `
    -Status "Still starting..." `
    -Detail "Startup is taking longer than expected. The browser can still open while the app finishes loading." `
    -ElapsedSeconds $MaxWaitSec
Show-Toast "Media Search Agent" "Taking longer than expected. Check $UvicornErr if it fails to open."
if (-not $NoBrowser -and -not $browserDone) { Open-Browser }
Close-StartupWindow
