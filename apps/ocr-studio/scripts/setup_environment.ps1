<#
.SYNOPSIS
    Create the local PredixaLearn GPU environment from pinned dependencies.
#>

param(
    [ValidateSet("cu118", "cu126", "cu129")]
    [string]$CudaVersion = "cu129",
    [string]$PaddleVersion = "3.3.0",
    [switch]$Development
)

$ErrorActionPreference = "Stop"
$ProjectRoot = Split-Path -Parent $PSScriptRoot
$VenvDir = if ($env:PREDIXALEARN_VENV_DIR) {
    [IO.Path]::GetFullPath($env:PREDIXALEARN_VENV_DIR)
} else {
    Join-Path $ProjectRoot ".venv"
}
$VenvPython = Join-Path $VenvDir "Scripts\python.exe"
$Requirements = if ($Development) {
    Join-Path $ProjectRoot "requirements-dev.txt"
} else {
    Join-Path $ProjectRoot "requirements.txt"
}

function Write-Step { param([string]$Message) Write-Host "`n[STEP] $Message" -ForegroundColor Cyan }
function Write-Ok { param([string]$Message) Write-Host "  [OK] $Message" -ForegroundColor Green }
function Write-Warn { param([string]$Message) Write-Host "  [WARN] $Message" -ForegroundColor Yellow }
function Invoke-PythonValidation {
    param(
        [Parameter(Mandatory)][string]$Code,
        [Parameter(Mandatory)][string]$FailureMessage
    )

    $Payload = [Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes($Code))
    $PreviousErrorActionPreference = $ErrorActionPreference
    try {
        $ErrorActionPreference = "Continue"
        $Output = & $VenvPython -c "import base64; exec(base64.b64decode('$Payload'))" 2>&1
        $ExitCode = $LASTEXITCODE
    } finally {
        $ErrorActionPreference = $PreviousErrorActionPreference
    }
    if ($ExitCode -ne 0) {
        $Output | ForEach-Object { Write-Host $_ }
        throw $FailureMessage
    }
    return @($Output | ForEach-Object { $_.ToString() })
}

Write-Step "Checking Python"
$SystemPython = (Get-Command python -ErrorAction Stop).Source
$PythonVersion = & $SystemPython --version
Write-Ok $PythonVersion

Write-Step "Checking NVIDIA driver"
try {
    $Gpu = & nvidia-smi --query-gpu=name,driver_version,memory.total --format=csv,noheader
    Write-Ok $Gpu
} catch {
    Write-Warn "nvidia-smi is unavailable; GPU acceleration may not work."
}

Write-Step "Creating virtual environment"
if (-not (Test-Path -LiteralPath $VenvPython)) {
    & $SystemPython -m venv $VenvDir
}
Write-Ok $VenvPython

Write-Step "Installing PaddlePaddle GPU"
& $VenvPython -m pip install --upgrade pip
$PaddleIndex = "https://www.paddlepaddle.org.cn/packages/stable/$CudaVersion/"
& $VenvPython -m pip install --upgrade "paddlepaddle-gpu==$PaddleVersion" --index-url $PaddleIndex
if ($LASTEXITCODE -ne 0) { throw "PaddlePaddle GPU installation failed." }

Write-Step "Installing pinned application dependencies"
& $VenvPython -m pip install --upgrade --requirement $Requirements
if ($LASTEXITCODE -ne 0) { throw "Application dependency installation failed." }

# Remove packages left behind by the former paddleocr[all] installation.
$LegacyPackages = @(
    "langchain",
    "langchain-core",
    "langchain-openai",
    "langchain-community",
    "langchain-text-splitters"
)
$InstalledPackages = & $VenvPython -m pip list --format=json | ConvertFrom-Json
if ($LASTEXITCODE -ne 0) { throw "Installed-package inspection failed." }
$InstalledNames = @($InstalledPackages | ForEach-Object { $_.name.ToLowerInvariant() })
$PackagesToRemove = @($LegacyPackages | Where-Object { $InstalledNames -contains $_ })
if ($PackagesToRemove.Count -gt 0) {
    & $VenvPython -m pip uninstall --yes @PackagesToRemove
    if ($LASTEXITCODE -ne 0) { throw "Legacy package cleanup failed." }
    Write-Ok "Removed obsolete optional packages: $($PackagesToRemove -join ', ')"
} else {
    Write-Ok "No obsolete optional packages are installed."
}

Write-Step "Installing project-local LibreOffice"
$LibreOfficeSetup = Join-Path $PSScriptRoot "setup_libreoffice.ps1"
& $LibreOfficeSetup
Write-Ok "Pinned LibreOffice runtime is ready."

Write-Step "Validating the environment"
& $VenvPython -m pip check
if ($LASTEXITCODE -ne 0) { throw "Installed dependencies are inconsistent." }
$RuntimeCode = @'
import paddle

runtime = paddle.device.get_cudnn_version()
runtime_text = f"{runtime // 10000}.{(runtime % 10000) // 100}.{runtime % 100}"
compiled = paddle.version.cudnn()
print(f"device={paddle.device.get_device()}; cuDNN runtime={runtime_text}; compiled={compiled}")
if runtime_text != compiled:
    raise RuntimeError("The loaded cuDNN runtime does not match Paddle's compiled cuDNN version")
paddle.utils.run_check()
layer = paddle.nn.Conv2D(3, 8, 3)
output = layer(paddle.randn([1, 3, 64, 64]))
paddle.device.synchronize()
print(f"GPU convolution passed: shape={list(output.shape)}")
'@
$Runtime = Invoke-PythonValidation -Code $RuntimeCode -FailureMessage (
    "Paddle GPU runtime validation failed."
)
$Runtime | ForEach-Object { Write-Host "  $_" }

Write-Step "Validating local document export"
$DocumentRuntimeCode = @'
from pathlib import Path
import pypandoc
import docx
import symspellpy

pandoc = Path(pypandoc.get_pandoc_path())
if not pandoc.is_file() and pandoc.with_suffix('.exe').is_file():
    pandoc = pandoc.with_suffix('.exe')
if not pandoc.is_file():
    raise RuntimeError('The packaged Pandoc executable is missing')
print(f'Pandoc={pypandoc.get_pandoc_version()}; python-docx={docx.__version__}; binary={pandoc.name}')
'@
$DocumentRuntime = Invoke-PythonValidation -Code $DocumentRuntimeCode -FailureMessage (
    "Document export validation failed."
)
$DocumentRuntime | ForEach-Object { Write-Ok $_ }

Write-Host "`nSetup complete." -ForegroundColor Green
Write-Host "Start PredixaLearn with:"
Write-Host "  & '$VenvPython' '$ProjectRoot\run.py'"
