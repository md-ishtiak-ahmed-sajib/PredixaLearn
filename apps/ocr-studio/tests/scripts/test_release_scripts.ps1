[CmdletBinding()]
param()

$ErrorActionPreference = "Stop"
$projectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..\.." -Resolve)).Path
$root = Join-Path ([IO.Path]::GetTempPath()) ("predixalearn-release-test-" + [guid]::NewGuid().ToString("N"))
$package = Join-Path $root "PredixaLearn.zip"
$install = Join-Path $root "Programs\PredixaLearn"
New-Item -ItemType Directory -Path $root | Out-Null
try {
  & (Join-Path $projectRoot "scripts\build_release.ps1") -OutputPath $package -Version "ci-smoke"
  if (-not (Test-Path -LiteralPath ($package + ".sha256"))) { throw "Release checksum was not created" }

  & (Join-Path $projectRoot "scripts\install_release.ps1") -PackagePath $package -InstallPath $install -NoShortcuts
  $launcher = Join-Path $install "scripts\launch_release.ps1"
  if (-not (Test-Path -LiteralPath $launcher)) { throw "Installed release is missing its launcher" }

  $before = (Get-Content -LiteralPath $launcher -Raw)
  Set-Content -LiteralPath ($package + ".sha256") -Value ("0" * 64) -Encoding ASCII
  $rejected = $false
  try {
    & (Join-Path $projectRoot "scripts\install_release.ps1") -PackagePath $package -InstallPath $install -NoShortcuts
  }
  catch {
    $rejected = $true
  }
  if (-not $rejected) { throw "Installer accepted a package with an invalid checksum" }
  if ((Get-Content -LiteralPath $launcher -Raw) -ne $before) {
    throw "Failed update changed the installed release"
  }
  Write-Output "Release build/install/checksum rollback smoke passed"
}
finally {
  if (Test-Path -LiteralPath $root) { Remove-Item -LiteralPath $root -Recurse -Force }
}
