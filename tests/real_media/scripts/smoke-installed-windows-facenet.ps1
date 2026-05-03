#Requires -Version 5.1
[Diagnostics.CodeAnalysis.SuppressMessageAttribute('PSAvoidUsingWriteHost', '',
    Justification = 'Manual smoke script - explicit console output is intentional.')]
param(
    [string] $AppDir = "$env:LOCALAPPDATA\MediaSearchAgent",
    [string] $DataDir = "$env:USERPROFILE\MediaSearchAgent",
    [string] $ConfigFile = "",
    [string] $RepoRoot = "",
    [string] $FixturesDir = "",
    [string] $OutputDir = "",
    [switch] $SkipInstall,
    [switch] $InstallWithDeps,
    [switch] $RunCpu,
    [switch] $AllowNoCuda
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

if (-not $RepoRoot) {
    $RepoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..\..\..")).ProviderPath
} else {
    $RepoRoot = (Resolve-Path $RepoRoot).ProviderPath
}

if (-not $FixturesDir) {
    $FixturesDir = Join-Path $RepoRoot "tests\real_media\fixtures\originals"
}
if (-not $OutputDir) {
    $OutputDir = Join-Path $RepoRoot "build\spikes\face-recognition"
}

$uvCandidates = @(
    (Join-Path $AppDir "uv\uv.exe"),
    (Join-Path $AppDir "uv.exe")
)
$UvExe = $uvCandidates | Where-Object { Test-Path $_ } | Select-Object -First 1
$VenvPy = Join-Path $AppDir ".venv\Scripts\python.exe"
$LauncherDir = Join-Path $AppDir "bin"
$MsaCmd = Join-Path $LauncherDir "msa.cmd"
$EvalScript = Join-Path $RepoRoot "scripts\spike_face_recognizer_eval.py"
if (-not $ConfigFile) {
    $ConfigFile = Join-Path $DataDir "config.yaml"
}

function Fail {
    param([string] $Message)
    Write-Host "ERROR: $Message" -ForegroundColor Red
    exit 1
}

function Invoke-Native {
    param(
        [string] $Label,
        [scriptblock] $Command
    )
    Write-Host ""
    Write-Host "==> $Label" -ForegroundColor Cyan
    & $Command
    if ($LASTEXITCODE -ne 0) {
        Fail "$Label failed with exit code $LASTEXITCODE"
    }
}

if (-not $UvExe) {
    Fail "uv.exe not found under $AppDir. Pass -AppDir pointing at the installed MediaSearchAgent app dir."
}
if (-not (Test-Path $VenvPy)) {
    Fail "Installed venv Python not found: $VenvPy"
}
if (-not (Test-Path $MsaCmd)) {
    Fail "Installed power-user CLI not found: $MsaCmd"
}
if (-not (Test-Path $EvalScript)) {
    Fail "Evaluator script not found: $EvalScript"
}
if (-not (Test-Path $FixturesDir)) {
    Fail "Fixtures directory not found: $FixturesDir"
}

New-Item -ItemType Directory -Force -Path $OutputDir | Out-Null

function ConvertTo-CmdArg {
    param([string] $Value)
    return '"' + ($Value -replace '"', '\"') + '"'
}

function Invoke-InMsaCmdEnvironment {
    param(
        [string] $Label,
        [string[]] $CommandArgs
    )
    $cmdFile = Join-Path $env:TEMP ("msa-facenet-smoke-" + [Guid]::NewGuid().ToString("N") + ".cmd")
    $commandLine = ($CommandArgs | ForEach-Object { ConvertTo-CmdArg $_ }) -join " "
    $content = @"
@echo off
call "$MsaCmd" --help >nul
if errorlevel 1 exit /b %ERRORLEVEL%
$commandLine
exit /b %ERRORLEVEL%
"@
    try {
        [System.IO.File]::WriteAllText($cmdFile, $content, [System.Text.Encoding]::GetEncoding(850))
        Invoke-Native $Label {
            & cmd.exe /d /c $cmdFile
        }
    } finally {
        if (Test-Path $cmdFile) {
            Remove-Item $cmdFile -Force -ErrorAction SilentlyContinue
        }
    }
}

function Invoke-InstalledPythonScript {
    param(
        [string] $Label,
        [string] $Code,
        [string[]] $Arguments = @()
    )
    $pyFile = Join-Path $env:TEMP ("msa-facenet-smoke-" + [Guid]::NewGuid().ToString("N") + ".py")
    try {
        [System.IO.File]::WriteAllText($pyFile, $Code, [System.Text.Encoding]::UTF8)
        $raw = & $VenvPy $pyFile @Arguments
        if ($LASTEXITCODE -ne 0) {
            Fail "$Label failed with exit code $LASTEXITCODE"
        }
        return $raw
    } finally {
        if (Test-Path $pyFile) {
            Remove-Item $pyFile -Force -ErrorAction SilentlyContinue
        }
    }
}

$probeCode = @'
import importlib.metadata as md
import json

def version(name):
    try:
        return md.version(name)
    except md.PackageNotFoundError:
        return None

result = {
    "torch": None,
    "torchvision": version("torchvision"),
    "facenet_pytorch": version("facenet-pytorch"),
    "numpy": version("numpy"),
    "opencv_python": version("opencv-python"),
    "opencv_python_headless": version("opencv-python-headless"),
    "cv2": None,
    "cv2_import_error": None,
    "cuda_available": None,
    "cuda_device": None,
    "torch_import_error": None,
}

try:
    import cv2
    result["cv2"] = cv2.__version__
except Exception as exc:
    result["cv2_import_error"] = repr(exc)

try:
    import torch
    result["torch"] = torch.__version__
    result["cuda_available"] = bool(torch.cuda.is_available())
    if result["cuda_available"]:
        result["cuda_device"] = torch.cuda.get_device_name(0)
except Exception as exc:
    result["torch_import_error"] = repr(exc)

print(json.dumps(result, sort_keys=True))
'@

function Get-Probe {
    $raw = Invoke-InstalledPythonScript "Python probe" $probeCode
    return ($raw | ConvertFrom-Json)
}

Write-Host "Installed AppDir: $AppDir"
Write-Host "Installed DataDir: $DataDir"
Write-Host "Installed Python: $VenvPy"
Write-Host "Installed CLI: $MsaCmd"
Write-Host "uv: $UvExe"
Write-Host "RepoRoot: $RepoRoot"
Write-Host "Fixtures: $FixturesDir"
Write-Host "OutputDir: $OutputDir"

Invoke-Native "Verify installed power-user CLI" {
    & $MsaCmd --help | Out-Null
}

$before = Get-Probe
Write-Host ""
Write-Host "Before install:" -ForegroundColor Cyan
$before | ConvertTo-Json

if ($before.torch_import_error) {
    Fail "torch does not import in the installed venv: $($before.torch_import_error)"
}
if (-not $before.torch) {
    Fail "torch is not installed in the installed venv"
}

if (-not $SkipInstall) {
    if ($InstallWithDeps) {
        Write-Host ""
        Write-Host "WARNING: -InstallWithDeps may replace torch/torchvision. Prefer default --no-deps." -ForegroundColor Yellow
        Invoke-Native "Install facenet-pytorch with dependencies" {
            & $UvExe pip install --python $VenvPy facenet-pytorch
        }
    } else {
        Invoke-Native "Install facenet-pytorch without dependencies" {
            & $UvExe pip install --python $VenvPy facenet-pytorch --no-deps
        }
    }
}

$after = Get-Probe
Write-Host ""
Write-Host "After install:" -ForegroundColor Cyan
$after | ConvertTo-Json

if ($after.torch_import_error) {
    Fail "torch import failed after facenet install: $($after.torch_import_error)"
}
if ($before.torch -ne $after.torch) {
    Fail "torch version changed from $($before.torch) to $($after.torch)"
}
if ($before.torchvision -ne $after.torchvision) {
    Fail "torchvision version changed from $($before.torchvision) to $($after.torchvision)"
}
if (-not $after.facenet_pytorch) {
    Fail "facenet-pytorch is not installed"
}
if ($after.cv2_import_error) {
    Write-Host ""
    Write-Host "WARNING: cv2 import failed in the installed runtime: $($after.cv2_import_error)" -ForegroundColor Yellow
    Write-Host "         Image pair checks can still pass, but video fixtures may be skipped." -ForegroundColor Yellow
}
if (-not $after.cuda_available -and -not $AllowNoCuda) {
    Fail "CUDA is not available in installed torch. Use -AllowNoCuda to run CPU-only smoke."
}

$cudaOutput = Join-Path $OutputDir "facenet-pytorch-vggface2-windows-native-cuda.json"
if ($after.cuda_available) {
    Invoke-InMsaCmdEnvironment "Run facenet-pytorch CUDA evaluator" @(
        $VenvPy,
        $EvalScript,
        "--backend", "facenet_pytorch",
        "--model", "vggface2",
        "--device", "cuda",
        "--conf", "0.8",
        "--fixtures", $FixturesDir,
        "--output", $cudaOutput
    )
} else {
    Write-Host ""
    Write-Host "Skipping CUDA evaluator because CUDA is not available." -ForegroundColor Yellow
}

if ($RunCpu -or -not $after.cuda_available) {
    $cpuOutput = Join-Path $OutputDir "facenet-pytorch-vggface2-windows-native-cpu.json"
    Invoke-InMsaCmdEnvironment "Run facenet-pytorch CPU evaluator" @(
        $VenvPy,
        $EvalScript,
        "--backend", "facenet_pytorch",
        "--model", "vggface2",
        "--device", "cpu",
        "--conf", "0.8",
        "--fixtures", $FixturesDir,
        "--output", $cpuOutput
    )
}

$summaryCode = @'
import json
import sys
from pathlib import Path

for raw in sys.argv[1:]:
    path = Path(raw)
    if not path.exists():
        continue
    data = json.loads(path.read_text(encoding="utf-8"))
    print(
        f"{path.name}: device={data.get('device')} "
        f"mtcnn_device={data.get('mtcnn_device')} "
        f"warm_ms={data.get('avg_warm_inference_ms')} "
        f"pos={data.get('positive_pair_sim_min')} "
        f"neg={data.get('negative_pair_sim_max')} "
        f"pairs={data.get('all_pairs_pass')}"
    )
'@

Write-Host ""
Write-Host "Summary:" -ForegroundColor Cyan
$summaryArgs = @($cudaOutput)
if ($RunCpu -or -not $after.cuda_available) {
    $summaryArgs += (Join-Path $OutputDir "facenet-pytorch-vggface2-windows-native-cpu.json")
}
Invoke-InstalledPythonScript "Summary" $summaryCode $summaryArgs

Write-Host ""
Write-Host "OK: Windows installed-runtime facenet smoke completed." -ForegroundColor Green
