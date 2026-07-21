<# Regenerate each application's public logos from packages/brand-assets. #>
[CmdletBinding()]
param()

$ErrorActionPreference = "Stop"
$Root = (Resolve-Path (Join-Path $PSScriptRoot "..\.." -Resolve)).Path
$Python = if ($env:PREDIXALEARN_PYTHON) {
    $env:PREDIXALEARN_PYTHON
} elseif (Test-Path -LiteralPath (Join-Path $Root ".venv\Scripts\python.exe")) {
    Join-Path $Root ".venv\Scripts\python.exe"
} else {
    "python"
}
& $Python (Join-Path $Root "apps\ocr-studio\scripts\sync_brand_assets.py")
exit $LASTEXITCODE
