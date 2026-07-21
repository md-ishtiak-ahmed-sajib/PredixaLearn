<# Run the Python checks from the OCR application workspace. #>
[CmdletBinding()]
param()

$ErrorActionPreference = "Stop"
$Root = (Resolve-Path (Join-Path $PSScriptRoot "..\.." -Resolve)).Path
$OcrRoot = Join-Path $Root "apps\ocr-studio"
$Python = if ($env:PREDIXALEARN_PYTHON) {
    $env:PREDIXALEARN_PYTHON
} else {
    Join-Path $Root ".venv\Scripts\python.exe"
}
Push-Location $OcrRoot
try {
    & $Python -m ruff check app tests run.py
    if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
    & $Python -m pytest -q
    exit $LASTEXITCODE
}
finally {
    Pop-Location
}
