#Requires -Version 5.1
[Diagnostics.CodeAnalysis.SuppressMessageAttribute('PSAvoidUsingWriteHost', '',
    Justification = 'CI validation script - explicit console output is intentional.')]
[Diagnostics.CodeAnalysis.SuppressMessageAttribute('PSAvoidUsingEmptyCatchBlock', '',
    Justification = 'Best-effort polling/cleanup should not mask the primary validation failure.')]
param(
    [Parameter(Mandatory = $true)]
    [string] $Bundle
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$RepoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..\..\..")).Path

if (-not (Test-Path $Bundle)) {
    throw "Bundle not found: $Bundle"
}

# Step 1: Choose isolated app/data roots so the installer exercises a clean
# end-user install layout on the runner.
$runRoot = if ($env:RUNNER_TEMP) { $env:RUNNER_TEMP } else { $env:TEMP }
$appDir = Join-Path $runRoot "msa-app"
$dataDir = Join-Path $runRoot "msa-data"
$ledgerDir = Join-Path $runRoot "ranker-ledger"
$testRoot = Join-Path $runRoot "msa-real-media-tests"
$configPath = Join-Path $dataDir "config.yaml"
$fixtureRoot = Join-Path $testRoot "fixtures"
$apiPort = 18082
# PowerShell Start-Process rejects -RedirectStandardOutput and
# -RedirectStandardError pointing at the same file, so split into two logs.
# Both are dumped to step output on API-readiness failure (see Step 7) and
# uploaded as workflow artifacts.
$apiLogOut = Join-Path $appDir "logs\runtime-api.out.log"
$apiLogErr = Join-Path $appDir "logs\runtime-api.err.log"
$apiProcess = $null

# Step 2: Stage the real-media validation payload outside the installed app.
# These tests/fixtures are external collateral used to validate the bundle.
New-Item -ItemType Directory -Force -Path $testRoot | Out-Null
Copy-Item (Join-Path $RepoRoot "tests\real_media\*") $testRoot -Recurse -Force
Get-ChildItem $testRoot -Directory -Filter "__pycache__" -Recurse | Remove-Item -Recurse -Force

# Step 3: Run the real Windows one-liner installer logic against the bundle.
& powershell -ExecutionPolicy Bypass -File (Join-Path $RepoRoot "installer\windows-native\shell\install.ps1") `
    -Bundle $Bundle `
    -AppDir $appDir `
    -DataDir $dataDir `
    -SkipAutoStart `
    -SkipLaunch
if ($LASTEXITCODE -ne 0) {
    exit $LASTEXITCODE
}

# Step 4: Add the only test-only dep BVT needs (pytest) on top of the
# bundle venv. The bundle's install.ps1 already produced a venv with the
# full runtime stack -- torch+torchvision were installed explicitly from
# the CUDA index URL and facenet-pytorch was installed --no-deps, so
# transformers' RT-DETR imports work. Layering tests/requirements-ci.txt
# on top would re-resolve that tree (with facenet-pytorch as a top-level
# constraint pulling old torch and torchvision pins), defeating the point
# of validating the bundle's runtime. real_media tests only import
# pytest + stdlib + msa_*.
$uvExe = Join-Path $appDir "uv\uv.exe"
$venvPy = Join-Path $appDir ".venv\Scripts\python.exe"
$toolDir = Join-Path $appDir "bin"
$msaCmd = Join-Path $toolDir "msa.cmd"

& $uvExe pip install --python $venvPy pytest
if ($LASTEXITCODE -ne 0) {
    exit $LASTEXITCODE
}

# S-5.4: IF the bundle shipped the learned-reranker serving library (zero-dep wheel),
# confirm it installed into the bundle venv and imports. Public-mirror bundles ship
# without the (private) wheel - skip cleanly there. Serving stays flag-off (heuristic).
$rankerInBundle = Get-ChildItem (Join-Path $appDir "wheels") -Filter "msa_ranker-*.whl" -ErrorAction SilentlyContinue
if ($rankerInBundle) {
    & $venvPy -c "import msa_ranker.serving, msa_ranker.features, msa_ranker.model; print('msa_ranker serving lib OK')"
    if ($LASTEXITCODE -ne 0) {
        Write-Error "msa_ranker wheel bundled but not importable in the bundle venv"
        exit $LASTEXITCODE
    }
} else {
    Write-Host "(no msa_ranker wheel in bundle - serving-lib check skipped; heuristic-only bundle)"
}

# Step 5: Write an isolated installed-app config that points at the staged
# fixture tree and a CI-only API port.
#
# Use a temp .py file rather than `python -c "..."`: PowerShell's argument
# pass-through to native exes mangles embedded `r"..."` raw-string quotes,
# producing `Path(rD:\a\..\config.yaml)` (a SyntaxError) when the value is
# interpolated into a here-string and forwarded to python.exe -c.
$patchScript = Join-Path $runRoot "patch_config.py"
@"
from pathlib import Path
import yaml

config_path = Path(r"$configPath")
fixture_root = r"$fixtureRoot"
api_port = $apiPort
data = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
data["media_sources"] = [{"name": "Real Media Fixtures", "path": fixture_root, "read_only": True}]
api = data.get("api") or {}
api["host"] = "127.0.0.1"
api["port"] = api_port
data["api"] = api
# S-5.4: pin the ranker event ledger to a known dir so the runtime BVT can assert
# end-to-end label capture (search -> shown -> open). Logging is on by default.
ranker = data.get("ranker") or {}
ranker["event_logging"] = True
ranker["ledger_dir"] = r"$ledgerDir"
data["ranker"] = ranker
# BVT runs on CPU (MSA_DEVICE=cpu, see Step 6), where the bundled config
# default `enable_object_detection: auto` resolves to "skip". Force it on
# so the indexer actually populates per-keyframe tags that the runtime
# tests assert on (test_video_media_*_includes_keyframe_tags).
data["enable_object_detection"] = True
data["enable_video_object_detection"] = True
config_path.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
"@ | Set-Content -Path $patchScript -Encoding UTF8
& $venvPy $patchScript
if ($LASTEXITCODE -ne 0) {
    exit $LASTEXITCODE
}

# Step 6: Put the installed media tools on PATH and run the staged fixture
# suite from the installed Python environment.
#
# MSA_CONFIG_PATH is required because this validation passes explicit
# -AppDir / -DataDir to install.ps1 to keep state under runner.temp. With
# non-default install paths the runtime's platform default
# (%USERPROFILE%\MediaSearchAgent\config.yaml) doesn't match -- msa.cmd
# already exports MSA_CONFIG_PATH at launch, but pytest is invoked
# directly via venvPy and would miss it. MSA_DEVICE=cpu pins the runner
# to CPU regardless of any GPU detection on windows-latest.
$env:MSA_CONFIG_PATH = $configPath
$env:MSA_DEVICE = "cpu"
& $msaCmd --help | Out-Null
$env:PATH = "$toolDir;$env:PATH"

Write-Host ("exiftool: " + (where.exe exiftool))
& exiftool -ver
& $venvPy -m pytest (Join-Path $testRoot "test_real_media_fixtures.py") -v -m "not slow"
if ($LASTEXITCODE -ne 0) {
    exit $LASTEXITCODE
}

# Step 7a: Indexer lifecycle stress -- interrupted-run clean-exit gate.
# Start a real `msa index run` against the staged fixtures, wait for the
# first BATCH_COMMIT line (proves CLIP loaded and a per-file commit landed),
# then `msa index stop` -- which writes the stop sentinel (the only delivery
# path that works on Windows because Intel Fortran's console-control handler
# hijacks CTRL_BREAK), and blocks with progress until the indexer exits
# cleanly. Asserts: clean exit (rc=0) and NO "forrtl: error" in the log.
# The forrtl check is the regression gate for the Intel-Fortran abort path
# the stop sentinel was introduced to avoid (PR #121, WIN-006). See
# internal/docs/testing/INDEXER_LIFECYCLE_STRESS.md for terminology and
# per-assertion rationale.
$logsDir = Join-Path $appDir "logs"
if (-not (Test-Path $logsDir)) {
    New-Item -ItemType Directory -Force -Path $logsDir | Out-Null
}
$lifecycleLog    = Join-Path $logsDir "indexer-lifecycle.out.log"
$lifecycleErrLog = Join-Path $logsDir "indexer-lifecycle.err.log"
# Lower commit-batch so the small fixture set reliably crosses a commit
# boundary before we issue the stop.
$env:MSA_INDEXER_COMMIT_BATCH_FILES = "2"
$env:MSA_INDEXER_COMMIT_BATCH_SECONDS = "2"

$lifecycleProcess = Start-Process -FilePath $msaCmd `
    -ArgumentList @("index", "run", "--config", $configPath,
                    "--media-source-override", $fixtureRoot) `
    -RedirectStandardOutput $lifecycleLog `
    -RedirectStandardError $lifecycleErrLog `
    -PassThru

# Up to 240s for BATCH_COMMIT (cold CLIP load can take 30-60s on CPU runners).
$batchCommitSeen = $false
for ($i = 0; $i -lt 480; $i++) {
    if ($lifecycleProcess.HasExited) {
        Write-Host "FAIL: indexer exited before BATCH_COMMIT (rc=$($lifecycleProcess.ExitCode))"
        if (Test-Path $lifecycleLog)    { Write-Host "--- stdout (tail) ---"; Get-Content $lifecycleLog    -Tail 200 }
        if (Test-Path $lifecycleErrLog) { Write-Host "--- stderr (tail) ---"; Get-Content $lifecycleErrLog -Tail 200 }
        exit 1
    }
    foreach ($logFile in @($lifecycleLog, $lifecycleErrLog)) {
        if ((Test-Path $logFile) -and (Select-String -Path $logFile -Pattern "BATCH_COMMIT" -Quiet)) {
            $batchCommitSeen = $true
            break
        }
    }
    if ($batchCommitSeen) { break }
    Start-Sleep -Milliseconds 500
}
if (-not $batchCommitSeen) {
    Write-Host "FAIL: no BATCH_COMMIT within 240s"
    if (Test-Path $lifecycleLog)    { Get-Content $lifecycleLog    -Tail 200 }
    if (Test-Path $lifecycleErrLog) { Get-Content $lifecycleErrLog -Tail 200 }
    try { $lifecycleProcess.Kill() } catch { }
    exit 1
}

& $msaCmd index stop --config $configPath --wait 60 --require-running
$stopRc = $LASTEXITCODE
if ($stopRc -ne 0) {
    Write-Host "FAIL: msa index stop returned exit code $stopRc; killing background indexer (PID $($lifecycleProcess.Id))"
    # Don't leave the Start-Process-launched indexer running for later CI
    # steps to trip over (holds locks, blocks subsequent msa invocations).
    if (-not $lifecycleProcess.HasExited) {
        try { $lifecycleProcess.Kill() } catch { }
        try { $lifecycleProcess.WaitForExit(10000) | Out-Null } catch { }
    }
    if (Test-Path $lifecycleLog)    { Get-Content $lifecycleLog    -Tail 200 }
    if (Test-Path $lifecycleErrLog) { Get-Content $lifecycleErrLog -Tail 200 }
    exit 1
}

# msa index stop already waited for the subprocess to exit; this final check
# guards against PID-race oddities and surfaces the actual rc.
if (-not $lifecycleProcess.WaitForExit(60000)) {
    Write-Host "FAIL: indexer process still alive after msa index stop returned"
    try { $lifecycleProcess.Kill() } catch { }
    exit 1
}
# Drain any async stream handlers post-exit (MSFT-recommended after a
# bounded WaitForExit on a process with redirected stdout/stderr).
$lifecycleProcess.WaitForExit()
# .NET disposes the Process handle once the OS process exits, so
# ExitCode can read back as $null in this scenario even when the
# process actually returned 0. `msa index stop` above (line 184) is
# the ground-truth check for clean indexer exit -- we'd have bailed
# already if it had failed. The check below only fires for a genuine
# non-zero rc; null is treated as success rather than a spurious fail.
$lifecycleExitCode = $lifecycleProcess.ExitCode
if ($null -ne $lifecycleExitCode -and $lifecycleExitCode -ne 0) {
    Write-Host "FAIL: indexer did not exit cleanly after ``msa index stop`` (rc=$lifecycleExitCode)"
    if (Test-Path $lifecycleLog)    { Get-Content $lifecycleLog    -Tail 200 }
    if (Test-Path $lifecycleErrLog) { Get-Content $lifecycleErrLog -Tail 200 }
    exit 1
}
foreach ($logFile in @($lifecycleLog, $lifecycleErrLog)) {
    if ((Test-Path $logFile) -and (Select-String -Path $logFile -Pattern "forrtl: error" -Quiet)) {
        Write-Host "FAIL: 'forrtl: error' in indexer log -- the stop sentinel path"
        Write-Host "      didn't engage. See WIN-006 in BUGS_AND_GOTCHAS.md."
        Get-Content $logFile -Tail 200
        exit 1
    }
}
# Cooperative stop must include Qdrant export of the batches that were
# durably committed before the stop. Without this, SQLite and Qdrant fall
# out of sync -- the API can return search hits backed by SQLite metadata
# that has no Qdrant vector entry.
$qdrantExportComplete = $false
foreach ($logFile in @($lifecycleLog, $lifecycleErrLog)) {
    if ((Test-Path $logFile) -and (Select-String -Path $logFile -Pattern "Qdrant image/video export complete" -Quiet)) {
        $qdrantExportComplete = $true
        break
    }
}
if (-not $qdrantExportComplete) {
    Write-Host "FAIL: cooperative stop did not complete Qdrant export -- SQLite"
    Write-Host "      and Qdrant are now out of sync. The cooperative-stop path"
    Write-Host "      in pipeline.py must run the export on stop_event when files"
    Write-Host "      were committed."
    if (Test-Path $lifecycleLog)    { Get-Content $lifecycleLog    -Tail 200 }
    if (Test-Path $lifecycleErrLog) { Get-Content $lifecycleErrLog -Tail 200 }
    exit 1
}

Remove-Item Env:MSA_INDEXER_COMMIT_BATCH_FILES   -ErrorAction SilentlyContinue
Remove-Item Env:MSA_INDEXER_COMMIT_BATCH_SECONDS -ErrorAction SilentlyContinue

# Step 7b: Resume the indexer to finish the remaining fixtures and export to
# Qdrant. Per-batch durability from Step 7a means rows committed before the
# stop are now skipped. The runtime tests in Step 9 then assert the indexed
# state and API contracts.
& $msaCmd index run --config $configPath --media-source-override $fixtureRoot --export-to-qdrant
if ($LASTEXITCODE -ne 0) {
    exit $LASTEXITCODE
}

$env:MSA_REALDATA_WORKSPACE = $dataDir
$env:MSA_REALDATA_SQLITE_PATH = Join-Path $dataDir "index\media.sqlite"
$env:MSA_REALDATA_FAISS_PATH = Join-Path $dataDir "index\image_vec.faiss"
$env:MSA_REALDATA_FACE_FAISS_PATH = Join-Path $dataDir "index\face_vec.faiss"
$env:MSA_REALDATA_THUMB_DIR = Join-Path $dataDir "data\thumbnails"
$env:MSA_REALDATA_FACE_THUMB_DIR = Join-Path $dataDir "data\face_thumbnails"
$env:MSA_REALDATA_FIXTURE_ROOT = $fixtureRoot
$env:MSA_REALDATA_BASE_URL = "http://127.0.0.1:$apiPort"
# Expose the ledger dir (-> the BVT open-capture test runs) ONLY when the ranker wheel is
# actually bundled. Public-mirror bundles ship without it => no LedgerWriter => no events;
# leaving the var unset makes the test skip cleanly instead of failing.
if (Get-ChildItem (Join-Path $appDir "wheels") -Filter "msa_ranker-*.whl" -ErrorAction SilentlyContinue) {
    $env:MSA_REALDATA_LEDGER_DIR = $ledgerDir
}

try {
    $apiProcess = Start-Process -FilePath $msaCmd `
        -ArgumentList @("api", "start", "--config", $configPath) `
        -RedirectStandardOutput $apiLogOut `
        -RedirectStandardError $apiLogErr `
        -PassThru

    $ready = $false
    for ($i = 0; $i -lt 120; $i++) {
        try {
            $resp = Invoke-WebRequest -Uri "$env:MSA_REALDATA_BASE_URL/health" -UseBasicParsing -TimeoutSec 2
            if ($resp.StatusCode -eq 200 -and $resp.Content -match '"status":"ready"') {
                $ready = $true
                break
            }
        } catch {
        }
        Start-Sleep -Seconds 1
    }

    if (-not $ready) {
        Write-Host "=== API stdout ($apiLogOut) ==="
        Get-Content $apiLogOut -ErrorAction SilentlyContinue
        Write-Host "=== API stderr ($apiLogErr) ==="
        Get-Content $apiLogErr -ErrorAction SilentlyContinue
        throw "API did not become ready. See $apiLogOut and $apiLogErr"
    }

    # Warm up the search path: /health returns ready before CLIP is loaded
    # (lazy-load on first /search), and on slower runners cold-start encoding
    # exceeds pytest's 20s urlopen timeout in test_search_endpoint_returns_json.
    # A throwaway POST /search with a generous timeout absorbs the cold start
    # outside the test budget; subsequent /search calls hit the warm model.
    Write-Host "Warming up CLIP text encoder via sentinel POST /search"
    try {
        Invoke-RestMethod -Uri "$env:MSA_REALDATA_BASE_URL/search" `
            -Method POST -Body '{"q":"warmup"}' `
            -ContentType "application/json" -TimeoutSec 180 | Out-Null
    } catch {
        throw "Sentinel /search warmup failed: $($_.Exception.Message). See $apiLogOut and $apiLogErr"
    }

    # Step 8: Run the runtime test suite against the installed indexed state and
    # live API server, then stop the API in the finally block.
    & $venvPy -m pytest (Join-Path $testRoot "test_real_media_runtime.py") -v
    if ($LASTEXITCODE -ne 0) {
        exit $LASTEXITCODE
    }
} finally {
    & $msaCmd api stop --config $configPath | Out-Null
    if ($apiProcess) {
        try {
            Wait-Process -Id $apiProcess.Id -Timeout 10 -ErrorAction SilentlyContinue
        } catch {
        }
    }
}
