[CmdletBinding()]
param(
  [Parameter(Mandatory = $true)]
  [string] $OutputPath,
  [string] $PythonPath = "python",
  [switch] $InstallBuildDependencies
)

$ErrorActionPreference = "Stop"
$installerRoot = (Resolve-Path (Join-Path $PSScriptRoot ".." -Resolve)).Path
$workspaceRoot = (Resolve-Path (Join-Path $installerRoot ".." -Resolve)).Path
$projectRoot = (Resolve-Path (Join-Path $workspaceRoot "apps\ocr-studio" -Resolve)).Path
$output = [IO.Path]::GetFullPath($OutputPath)
$temporary = Join-Path ([IO.Path]::GetTempPath()) ("predixalearn-bootstrap-" + [guid]::NewGuid().ToString("N"))
New-Item -ItemType Directory -Path $temporary | Out-Null
try {
  if ($InstallBuildDependencies) {
    & $PythonPath -m pip install -r (Join-Path $installerRoot "requirements-desktop-bootstrap.txt")
    if ($LASTEXITCODE -ne 0) { throw "Could not install desktop bootstrap build dependencies." }
  }
  & $PythonPath -c "import Crypto, cryptography, PyInstaller" 2>$null
  if ($LASTEXITCODE -ne 0) {
    throw "Desktop bootstrap dependencies are missing. Re-run with -InstallBuildDependencies."
  }
  $publicKey = Join-Path $projectRoot "app\maintenance\predixalearn-update-public.pem"
  & $PythonPath -m PyInstaller --noconfirm --clean --onefile --noconsole `
    --name "PredixaLearnDesktopBootstrapper" `
    --distpath (Join-Path $temporary "dist") `
    --workpath (Join-Path $temporary "work") `
    --specpath (Join-Path $temporary "spec") `
    --add-data "$publicKey;." `
    --collect-all cryptography `
    (Join-Path $projectRoot "app\desktop_bootstrap.py")
  if ($LASTEXITCODE -ne 0) { throw "PyInstaller could not build the PredixaLearn desktop bootstrapper." }
  New-Item -ItemType Directory -Force -Path (Split-Path -Parent $output) | Out-Null
  Copy-Item -LiteralPath (Join-Path $temporary "dist\PredixaLearnDesktopBootstrapper.exe") -Destination $output -Force
  Write-Output "Created $output"
}
finally {
  if (Test-Path -LiteralPath $temporary) { Remove-Item -LiteralPath $temporary -Recurse -Force }
}
