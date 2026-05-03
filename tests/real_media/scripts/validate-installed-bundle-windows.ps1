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
# full runtime stack — torch+torchvision were installed explicitly from
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
# (%USERPROFILE%\MediaSearchAgent\config.yaml) doesn't match — msa.cmd
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

# Step 7: Run the full installed CLI workflow: index the staged fixtures,
# expose the generated artifact paths to the runtime tests, and start the API.
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
