#Requires -Version 5.1
<#
.SYNOPSIS
    Real-host validation of the Windows Tauri DESKTOP bundle (M-7/S-3 item 3).

.DESCRIPTION
    Drives the REAL shippable desktop app the same way the macOS harness
    (validate-installed-desktop-macos.sh) does: install the artifact, LAUNCH THE APP
    (not `msa api start`), discover the app's ephemeral backend port from the
    sidecar's published-port file (logs\sidecar-port; issue #172 resolved), run the
    real-media assertions against that backend, then quit the owned process and assert
    NO orphaned sidecar survives.

    Two modes (both UI-launch the app - the CLI-serve asymmetry with macOS is gone):

    * Default (real host): run on the 4D.2 Hyper-V harness (NVIDIA VM and non-NVIDIA VM).
      Adds the webview CORS preflight (only a real host can catch it) and a smoke
      search on top of the launch/port/quit/orphan flow.

    * -Ci: the CI-safe path for a GitHub-hosted windows-latest runner. Same
      UI-launch + port-discovery + quit/orphan flow, and runs the FULL real-media runtime
      pytest suite (same test bodies as the macOS desktop BVT). SKIPS the real-host-only
      CORS preflight. This is the go-forward replacement for the retiring
      build-windows-shell-bundle real-media job.

    Steps (mirrors the macOS harness AND the real user flow - the app itself performs
    the cold in-process provisioning; see BUGS_AND_GOTCHAS PTH-001 for the bug class
    that the previous headless-provision-first flow could never catch):
      1. Silent-install the Tauri setup.exe (setup.exe /S - the user's NSIS payload).
      2. Launch #1 - COLD first launch: the app provisions the venv in-process exactly
         like a user's first double-click; wait for /health ready, then quit.
      3. Assert the torch wheel variant matches the box: cu128 on NVIDIA, CPU otherwise.
      4. Stage the real-media fixtures and index them (venv `msa` CLI, app down -> no
         Qdrant lock contention).
      5. Launch #2 - relaunch to serve; discover the ephemeral backend port from the
         sidecar-port file, and assert /health ready.
      6. Runtime validation against the app's backend: the full real-media pytest suite
         (-Ci), or a CORS preflight + smoke search (real host).
      7. Quit the owned app process, then reap + report any surviving orphan python.exe
         (the SUPERVISOR_PID watchdog should reap the socket-holding sidecar child once the
         Tauri supervisor is gone). Orphan-on-quit is tracked as issue #171 and reproduces
         on BOTH platforms today, so this is a NON-FATAL warning (mirrors the macOS harness,
         which exits on the runtime result) - flip to a hard gate once #171 is fixed.
      8. Silent uninstall -> app-private runtime removed.

    Documented deviations from the raw user flow: /S silent install (same NSIS payload,
    no UI); MSA_DISABLE_UPDATER=1 (validate THIS build, not an updated one); hard process
    kill on quit (a user clicks close). Headless-provision coverage (install.ps1
    -Headless) left this harness when Launch #1 took over cold provisioning - re-cover
    that path separately if the headless install regresses.
      7. Silent-uninstall and assert the app-private runtime dir is gone.

.PARAMETER Setup
    Path to the built installer: MediaSearchAgent_<version>_x64-setup.exe

.PARAMETER Ci
    Run the CI-safe subset (see above) instead of the full real-host validation. (Both modes
    UI-launch the app; -Ci only selects which checks run - it is NOT a headless/CLI-serve mode.)
#>
[Diagnostics.CodeAnalysis.SuppressMessageAttribute('PSAvoidUsingWriteHost', '',
    Justification = 'Validation script - explicit console output is intentional.')]
[Diagnostics.CodeAnalysis.SuppressMessageAttribute('PSAvoidUsingEmptyCatchBlock', '',
    Justification = 'Best-effort polling/cleanup should not mask the primary validation failure.')]
[Diagnostics.CodeAnalysis.SuppressMessageAttribute('PSUseShouldProcessForStateChangingFunctions', '',
    Justification = 'Validation helpers mutate only test-run state on a throwaway VM.')]
param(
    [Parameter(Mandatory = $true)]
    [string] $Setup,

    [switch] $Ci
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$RepoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..\..\..")).Path
$ProductName   = "MediaSearchAgent"
$AppId         = "ai.openara.mediasearchagent"
$InstallDir    = Join-Path $env:LOCALAPPDATA $ProductName
$AppPrivateDir = Join-Path $env:LOCALAPPDATA $AppId
$VenvPython    = Join-Path $AppPrivateDir ".venv\Scripts\python.exe"
$MsaExe        = Join-Path $AppPrivateDir ".venv\Scripts\msa.exe"   # venv entry point (the app's own provisioning creates it)
$Uninstaller   = Join-Path $InstallDir "uninstall.exe"
$AppExe        = Join-Path $InstallDir "$ProductName.exe"           # renamed via tauri.conf mainBinaryName (#163)
# ADR-009 platform log dir (provision.platform_dirs): %LOCALAPPDATA%\MediaSearchAgent\logs.
# The desktop shim + uvicorn write msa-desktop.log here; the sidecar publishes its bound
# ephemeral port to sidecar-port (issue #172) - the authoritative port-discovery source.
$LogDir        = Join-Path $env:LOCALAPPDATA "$ProductName\logs"
$DesktopLog    = Join-Path $LogDir "msa-desktop.log"
$PortFile      = Join-Path $LogDir "sidecar-port"

$runRoot     = if ($env:RUNNER_TEMP) { $env:RUNNER_TEMP } else { $env:TEMP }
$testRoot    = Join-Path $runRoot "msa-desktop-tests"
$fixtureRoot = Join-Path $testRoot "fixtures"
$configPath  = Join-Path $env:USERPROFILE "MediaSearchAgent\config.yaml"   # ADR-009 DataDir
$ledgerDir   = Join-Path $runRoot "ranker-ledger"

function Fail($msg) { Write-Host "FAIL: $msg" -ForegroundColor Red; exit 1 }
function Ok($msg)   { Write-Host "  OK: $msg" -ForegroundColor Green }
function Step($msg) { Write-Host "`n==> $msg" -ForegroundColor Cyan }

# Read a file that another process holds open for append (the rotating msa-desktop.log): open with
# FileShare.ReadWrite so the read never trips a Windows sharing violation. Returns '' if unreadable.
function Read-SharedText([string] $Path) {
    if (-not (Test-Path $Path)) { return '' }
    try {
        $fs = [System.IO.File]::Open($Path, [System.IO.FileMode]::Open, [System.IO.FileAccess]::Read, [System.IO.FileShare]::ReadWrite)
        $sr = New-Object System.IO.StreamReader($fs)
        try { return $sr.ReadToEnd() } finally { $sr.Dispose(); $fs.Dispose() }
    } catch { return '' }
}

# Launch the desktop app exactly as a user double-click would (fresh log + port file so we
# observe THIS launch only). Returns the process object.
function Start-DesktopApp {
    if (Test-Path $DesktopLog) { Clear-Content -Path $DesktopLog -ErrorAction SilentlyContinue }
    if (Test-Path $PortFile)   { Remove-Item  -Path $PortFile   -ErrorAction SilentlyContinue }
    if (-not (Test-Path $AppExe)) { Fail "app exe missing at $AppExe" }
    return Start-Process -FilePath $AppExe -PassThru
}

# Wait for the backend to publish its port (logs\sidecar-port, issue #172) and answer
# /health "ready". Returns the port; Fails (with the desktop log dumped) on app exit or timeout.
function Wait-BackendReady([object] $Gui, [int] $TimeoutSec) {
    $port = $null
    for ($i = 0; $i -lt $TimeoutSec; $i++) {
        if ($Gui.HasExited) {
            Write-Host (Read-SharedText $DesktopLog)
            Fail "desktop app exited during startup (exit $($Gui.ExitCode)); see $DesktopLog"
        }
        if (-not $port) {
            # Written atomically by the sidecar right after it binds (never held open, so plain
            # Get-Content is safe); the /health ready gate below confirms liveness, so a stale
            # value can never green-light.
            if (Test-Path $PortFile) {
                $rawPort = (Get-Content -Path $PortFile -Raw -ErrorAction SilentlyContinue)
                if ($rawPort) { $rawPort = ($rawPort -replace '\D', '') }
                if ($rawPort) {
                    $port = $rawPort
                    Write-Host "  discovered ephemeral backend port $port (sidecar-port file)"
                }
            }
        }
        if ($port) {
            try {
                $resp = Invoke-WebRequest -Uri "http://127.0.0.1:$port/health" -UseBasicParsing -TimeoutSec 2
                if ($resp.StatusCode -eq 200 -and $resp.Content -match '"status":"ready"') { return $port }
            } catch { }
        }
        Start-Sleep -Seconds 1
    }
    Write-Host (Read-SharedText $DesktopLog)
    Fail "backend did not become ready within ${TimeoutSec}s (port='$port'); see $DesktopLog"
}

# Quit the supervisor and reap any app-private survivors. Hard-killing the Tauri supervisor can
# orphan BOTH the python sidecar (python.exe running from the app-private venv; issue #171 - the
# SUPERVISOR_PID watchdog should reap it) AND the app's WebView2 host (msedgewebview2.exe whose
# EBWebView --user-data-dir lives under the app-private dir; a survivor keeps EBWebView locked and
# blocks the uninstaller's RMDir). Waits for full exit so venv-DLL / EBWebView handles release.
# Returns $true if any orphan had to be reaped (NON-FATAL; matches the macOS harness).
function Stop-AppAndReapOrphans([object] $Gui) {
    if ($Gui -and -not $Gui.HasExited) {
        try { Stop-Process -Id $Gui.Id -Force -ErrorAction SilentlyContinue } catch { }
        try { Wait-Process -Id $Gui.Id -Timeout 10 -ErrorAction SilentlyContinue } catch { }
    }
    Start-Sleep -Seconds 8   # give the SUPERVISOR_PID watchdog time to reap the sidecar child
    $pyOrphans = @(Get-CimInstance Win32_Process -Filter "Name='python.exe'" -ErrorAction SilentlyContinue |
        Where-Object { $_.ExecutablePath -and $_.ExecutablePath -like "$AppPrivateDir*" })
    $wvOrphans = @(Get-CimInstance Win32_Process -Filter "Name='msedgewebview2.exe'" -ErrorAction SilentlyContinue |
        Where-Object { $_.CommandLine -and $_.CommandLine -like "*$AppPrivateDir*" })
    $orphans = @($pyOrphans + $wvOrphans)
    if ($orphans.Count -gt 0) {
        $orphans | ForEach-Object { Write-Host "  orphan PID $($_.ProcessId) ($($_.Name)): $($_.ExecutablePath)" }
        $orphans | ForEach-Object { try { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue } catch { } }
        $orphans | ForEach-Object { try { Wait-Process -Id $_.ProcessId -Timeout 15 -ErrorAction SilentlyContinue } catch { } }
        return $true
    }
    Ok "no orphan python.exe / msedgewebview2.exe after app quit"
    return $false
}

if (-not (Test-Path $Setup)) { Fail "Setup installer not found: $Setup" }

# The desktop app self-updates on launch (main.rs check_for_updates). Disable it for the BVT so
# we validate THIS freshly-built setup.exe, not a version the updater swaps in mid-run: a
# branch-dispatch build stamps 0.0.0 (no reachable tag) and would otherwise pull the latest
# public release. Set before any launch so both Launch #1 (cold) and #2 inherit it.
$env:MSA_DISABLE_UPDATER = '1'

# -- Step 1: silent install (the user's NSIS payload; NO pre-provisioning) ---------------
# setup.exe /S runs the exact installer payload a user runs (minus the UI). Provisioning is
# deliberately NOT done here: the app itself must do it on first launch (step 2), because
# that is what real users hit - the previous install.ps1 -Headless flow provisioned in a
# separate process and permanently masked the in-process cold-launch bug class (PTH-001).
Step "[1/8] Silent install (setup.exe /S)"
Start-Process -FilePath $Setup -ArgumentList '/S' -Wait
for ($i = 0; $i -lt 120; $i++) { if (Test-Path $AppExe) { break }; Start-Sleep -Seconds 1 }  # NSIS /S may detach
if (-not (Test-Path $AppExe)) { Fail "app exe missing at $AppExe after silent install" }
if (Test-Path $VenvPython) { Fail "venv already provisioned before first launch - step 2 would not be a COLD launch (delete $AppPrivateDir and re-run)" }
Ok "installed to $InstallDir (unprovisioned - cold)"

$OrphanSeen = $false

# -- Step 2: Launch #1 - COLD first launch: the app provisions in-process (user flow) ----
# The exact path a real user hits on first double-click: the shim provisions the venv INSIDE
# the interpreter that then imports the app - .pth re-scan, pre-import, responder->uvicorn
# handoff, port publish, all under test. Generous budget: CPU torch + app deps, cold runner.
Step "[2/8] Launch #1 - cold first launch (in-process provisioning, the user flow)"
$gui1 = Start-DesktopApp
$port1 = Wait-BackendReady -Gui $gui1 -TimeoutSec 900
Ok "cold first launch reached ready on port $port1 (provisioned in-process)"
if (Stop-AppAndReapOrphans -Gui $gui1) { $OrphanSeen = $true }   # app down -> indexer can take the Qdrant lock
if (-not (Test-Path $VenvPython)) { Fail "app-private venv python missing at $VenvPython after cold launch" }
if (-not (Test-Path $MsaExe))     { Fail "venv msa entry point missing at $MsaExe after cold launch" }

# -- Step 3: torch wheel variant matches the box -----------------------------------------
Step "[3/8] Torch wheel variant (cu128 on NVIDIA, CPU otherwise)"
$hasNvidia = $false
try {
    $gpus = Get-CimInstance Win32_VideoController -ErrorAction Stop |
        Where-Object { $_.Name -match 'NVIDIA' -and $_.Name -notmatch 'Virtual|Remote|Basic' }
    $hasNvidia = [bool]$gpus
} catch { $hasNvidia = $false }

$torchCuda = (& $VenvPython -c "import torch; print(torch.version.cuda)") 2>&1 | Select-Object -Last 1
if ($hasNvidia) {
    if ("$torchCuda" -notmatch '^12\.8') {
        Fail "NVIDIA box but torch.version.cuda='$torchCuda' (expected 12.8 / cu128 wheels)"
    }
    Ok "NVIDIA box -> cu128 torch ($torchCuda)"
} else {
    if ("$torchCuda" -ne "None") {
        Fail "non-NVIDIA box but torch.version.cuda='$torchCuda' (expected None / CPU wheels)"
    }
    Ok "non-NVIDIA box -> CPU torch"
}

# -- Step 4: stage fixtures, patch config, index -----------------------------------------
Step "[4/8] Index sample media (app down - no Qdrant lock contention)"
New-Item -ItemType Directory -Force -Path $testRoot | Out-Null
Copy-Item (Join-Path $RepoRoot "tests\real_media\*") $testRoot -Recurse -Force
Get-ChildItem $testRoot -Directory -Filter "__pycache__" -Recurse | Remove-Item -Recurse -Force

# NB: no api.host/api.port patch - the desktop app binds an OS-assigned EPHEMERAL port
# (src-tauri free_port()), ignoring config.api.port. The port is discovered at launch (step 5).
$patchScript = Join-Path $runRoot "patch_desktop_config.py"
@"
from pathlib import Path
import yaml
config_path = Path(r"$configPath")
data = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
data["media_sources"] = [{"name": "Real Media Fixtures", "path": r"$fixtureRoot", "read_only": True}]
# Force object detection: on a non-NVIDIA runner (windows-latest) the template
# default `auto` resolves to skip on CPU, but the runtime suite asserts video
# keyframe object tags. Mirrors validate-installed-desktop-macos.sh.
data["enable_object_detection"] = True
data["enable_video_object_detection"] = True
r = data.get("ranker") or {}
r["event_logging"] = True
r["ledger_dir"] = r"$ledgerDir"
data["ranker"] = r
config_path.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
"@ | Set-Content -Path $patchScript -Encoding UTF8
& $VenvPython $patchScript
if ($LASTEXITCODE -ne 0) { Fail "config patch failed" }

# Expose the artifact's bundled exiftool/mediainfo so indexing + the runtime suite
# validate the PACKAGED tools, not any host copy (the GoPro-GPS assertion needs
# exiftool, absent on a stock windows-latest). Mirrors the macOS harness PATH export.
$toolsDir = $null
foreach ($cand in @((Join-Path $InstallDir "bin"), (Join-Path $InstallDir "resources\bin"), (Join-Path $AppPrivateDir "bin"))) {
    if (Test-Path (Join-Path $cand "exiftool.exe")) { $toolsDir = $cand; break }
}
if (-not $toolsDir) { Fail "bundled exiftool.exe not found under the install / app-private bin dirs - cannot validate the packaged tools" }
$env:MSA_TOOLS_DIR = $toolsDir
$env:PATH = "$toolsDir;$($env:PATH)"
Ok "bundled media tools on PATH: $toolsDir"

& $MsaExe index run --config $configPath --media-source-override $fixtureRoot --export-to-qdrant
if ($LASTEXITCODE -ne 0) { Fail "msa index run exited $LASTEXITCODE" }
Ok "indexed the fixture set"

# -- Step 5: Launch #2 - relaunch to serve (warm); discover the ephemeral port -----------
# Mirror of validate-installed-desktop-macos.sh Launch #2. The Tauri supervisor spawns the
# sidecar off-thread BEFORE building the window (src-tauri/src/main.rs), so the backend
# binds + serves regardless of webview rendering on a CI runner.
Step "[5/8] Launch #2 - relaunch to serve -> ephemeral port -> /health ready"
$gui = Start-DesktopApp
try {
    $port = Wait-BackendReady -Gui $gui -TimeoutSec 300   # warm launch: seconds; generous for CI
    $baseUrl = "http://127.0.0.1:$port"
    Ok "backend ready on ephemeral port $port"

    # -- Step 6: runtime validation against the app's backend ----------------------------
    Step "[6/8] Runtime validation against the app's backend"
    if (-not $Ci) {
        # CORS preflight from the webview origin (CI can't catch this; only a real host).
        $headers = @{ "Origin" = "tauri://localhost"; "Access-Control-Request-Method" = "POST" }
        $pf = Invoke-WebRequest -Uri "$baseUrl/search" -Method Options -Headers $headers -UseBasicParsing -TimeoutSec 5
        $allow = $pf.Headers["Access-Control-Allow-Origin"]
        if (-not $allow) { Fail "CORS preflight from tauri://localhost returned no Access-Control-Allow-Origin" }
        Ok "CORS preflight from tauri://localhost -> $allow"
    }

    # Warm the CLIP encoder (a cold first /search can exceed pytest's urlopen budget).
    Invoke-RestMethod -Uri "$baseUrl/search" -Method POST -Body '{"q":"warmup"}' `
        -ContentType "application/json" -TimeoutSec 180 | Out-Null

    if ($Ci) {
        # Full real-media assertion suite against the app's backend - the same test bodies as the
        # macOS desktop BVT. pytest is installed to a scratch dir (via the stdlib ensurepip) so the
        # artifact venv stays pristine.
        & $VenvPython -m ensurepip --upgrade 2>&1 | Out-Null
        $pytestDir = Join-Path $runRoot "pytest-libs"
        & $VenvPython -m pip install --disable-pip-version-check --target $pytestDir pytest 2>&1 | Out-Null
        if ($LASTEXITCODE -ne 0) { Fail "pytest install failed" }
        $dataDir = Split-Path $configPath -Parent
        $env:PYTHONPATH = $pytestDir
        $env:MSA_REALDATA_WORKSPACE = $dataDir
        $env:MSA_REALDATA_SQLITE_PATH = Join-Path $dataDir "index\media.sqlite"
        $env:MSA_REALDATA_FIXTURE_ROOT = $fixtureRoot
        $env:MSA_REALDATA_BASE_URL = $baseUrl
        $env:MSA_REALDATA_FAISS_PATH = Join-Path $dataDir "index\image_vec.faiss"
        $env:MSA_REALDATA_FACE_FAISS_PATH = Join-Path $dataDir "index\face_vec.faiss"
        $env:MSA_REALDATA_THUMB_DIR = Join-Path $dataDir "data\thumbnails"
        $env:MSA_REALDATA_FACE_THUMB_DIR = Join-Path $dataDir "data\face_thumbnails"
        # Ranker ledger only when the (private) ranker wheel is actually installed, else the
        # end-to-end capture test skips cleanly (mirrors the macOS/bundle harnesses).
        # NCE-001: on the mirrored tree the wheel is absent and the probe legitimately fails;
        # under EAP=Stop its ImportError traceback on stderr terminates the script despite
        # 2>$null, so run the best-effort probe under EAP=Continue restored in finally.
        $prevEAP = $ErrorActionPreference
        $ErrorActionPreference = 'Continue'
        try {
            & $VenvPython -c "import msa_ranker.serving" 2>$null
        } finally {
            $ErrorActionPreference = $prevEAP
        }
        if ($LASTEXITCODE -eq 0) { $env:MSA_REALDATA_LEDGER_DIR = $ledgerDir }
        & $VenvPython -m pytest (Join-Path $testRoot "test_real_media_runtime.py") -v
        if ($LASTEXITCODE -ne 0) { Fail "runtime real-media suite failed" }
        Ok "runtime real-media suite passed"
    } else {
        $hits = Invoke-RestMethod -Uri "$baseUrl/search" -Method POST -Body '{"q":"a photo"}' `
            -ContentType "application/json" -TimeoutSec 60
        if ($null -eq $hits) { Fail "search returned no payload" }
        Ok "search returned a payload"
    }
} finally {
    # -- Step 7: quit the owned app -> reap + report any orphan (two-process tree) -----------
    # Runs even if the suite above failed/threw, so the app is always torn down and uninstall
    # can remove the venv (see Stop-AppAndReapOrphans for the #171 / WebView2 detail).
    Step "[7/8] Quit app -> reap + report orphan python.exe (watchdog; #171)"
    if (Stop-AppAndReapOrphans -Gui $gui) { $OrphanSeen = $true }
}
# Orphan-on-quit is tracked as #171 (the python sidecar) and reproduces on macOS too; the macOS
# harness reaps + warns and exits on the runtime result, so match it here (non-fatal). The WebView2
# host can orphan the same way on a hard kill. Flip to a hard Fail once the supervisor reliably reaps
# its child tree, so this BVT then guards teardown on both platforms.
if ($OrphanSeen) {
    Write-Host "WARN: orphaned process(es) reaped during teardown (python sidecar #171 and/or WebView2 host) - see issue #171" -ForegroundColor Yellow
}

# -- Step 8: silent uninstall -> app-private runtime removed -----------------------------
# The NSIS /S uninstaller detaches and its POSTUNINSTALL hook RMDir's the app-private runtime
# (venv/python/uv + the EBWebView WebView2 user-data dir) under %LOCALAPPDATA%\<AppId>
# asynchronously, so poll for the dir to vanish rather than a fixed sleep. Step 7 reaps the
# orphaned WebView2 host too, so this should complete; if the dir still persists (some process still
# holds a file under it open after the hard kill) it is a real-host (human) DoD failure, but a
# NON-FATAL warning in CI mode.
Step "[8/8] Silent uninstall -> app-private runtime removed"
if (Test-Path $Uninstaller) {
    & $Uninstaller /S | Out-Null
    for ($i = 0; $i -lt 30; $i++) { if (-not (Test-Path $AppPrivateDir)) { break }; Start-Sleep -Seconds 1 }
} else {
    Write-Host "  (uninstaller not found at $Uninstaller; skipping silent-uninstall assertion)"
}
if (Test-Path $AppPrivateDir) {
    $msg = "app-private runtime dir persisted after uninstall: $AppPrivateDir"
    if ($Ci) {
        Write-Host "WARN: $msg - a process still holds a file under it open after the hard kill (EBWebView/venv); uninstall+cleanup is a real-host (human) DoD, non-fatal in CI mode." -ForegroundColor Yellow
    } else {
        Fail "$msg (POSTUNINSTALL hook did not run / RMDir blocked)"
    }
} else {
    Ok "app-private runtime dir removed"
}

Write-Host "`nAll desktop validation steps passed." -ForegroundColor Green
