[CmdletBinding()]
param(
  [Parameter(Mandatory = $true)]
  [string] $OutputDirectory,
  [string] $Version = "dev",
  [string] $RuntimeManifestUrl = "https://github.com/md-ishtiak-ahmed-sajib/PredixaLearn/releases/latest/download/predixalearn-desktop-manifest.json",
  [string] $BundledRuntimePath,
  [string] $BundledPythonRuntimePath,
  [string] $BundledLibreOfficePath,
  [string] $PythonPath = "python",
  [switch] $InstallBuildDependencies,
  [switch] $Sign
)

$ErrorActionPreference = "Stop"
$installerRoot = (Resolve-Path (Join-Path $PSScriptRoot ".." -Resolve)).Path
$workspaceRoot = (Resolve-Path (Join-Path $installerRoot ".." -Resolve)).Path
$projectRoot = (Resolve-Path (Join-Path $workspaceRoot "apps\ocr-studio" -Resolve)).Path
$output = [IO.Path]::GetFullPath($OutputDirectory)
New-Item -ItemType Directory -Force -Path $output | Out-Null
if ($BundledRuntimePath -and $BundledPythonRuntimePath) {
  throw "Specify either BundledRuntimePath or BundledPythonRuntimePath, not both."
}
$bootstrapper = Join-Path $output "PredixaLearnDesktopBootstrapper.exe"
& (Join-Path $PSScriptRoot "build_desktop_bootstrapper.ps1") `
  -OutputPath $bootstrapper -PythonPath $PythonPath -InstallBuildDependencies:$InstallBuildDependencies
if ($Sign) { & (Join-Path $PSScriptRoot "sign_windows_artifact.ps1") -Path $bootstrapper }

$iscc = (Get-Command ISCC.exe -ErrorAction SilentlyContinue).Source
if (-not $iscc) {
  $candidates = @(
    (Join-Path $env:ProgramFiles "Inno Setup 7\ISCC.exe"),
    (Join-Path ${env:ProgramFiles(x86)} "Inno Setup 7\ISCC.exe"),
    (Join-Path $env:LOCALAPPDATA "Programs\Inno Setup 7\ISCC.exe"),
    (Join-Path ${env:ProgramFiles(x86)} "Inno Setup 6\ISCC.exe"),
    (Join-Path $env:LOCALAPPDATA "Programs\Inno Setup 6\ISCC.exe"),
    (Join-Path $env:ProgramFiles "Inno Setup 6\ISCC.exe")
  )
  $iscc = $candidates | Where-Object { Test-Path -LiteralPath $_ -PathType Leaf } | Select-Object -First 1
}
if (-not $iscc) { throw "Inno Setup 6 or 7 (ISCC.exe) was not found." }
$isccArguments = @(
  "/DAppVersion=$Version",
  "/DBootstrapperPath=$bootstrapper",
  "/DRuntimeManifestUrl=$RuntimeManifestUrl",
  "/O$output"
)
if ($BundledRuntimePath) {
  $bundle = Resolve-Path -LiteralPath $BundledRuntimePath -ErrorAction Stop
  if (-not (Test-Path -LiteralPath $bundle -PathType Leaf)) {
    throw "Bundled runtime must be a ZIP file: $BundledRuntimePath"
  }
  if ([IO.Path]::GetExtension($bundle.Path) -ne ".zip") {
    throw "Bundled runtime must use the .zip extension: $($bundle.Path)"
  }
  $bundleHash = (Get-FileHash -LiteralPath $bundle -Algorithm SHA256).Hash.ToLowerInvariant()
  $isccArguments += "/DBundledRuntimePath=$($bundle.Path)"
  $isccArguments += "/DBundledRuntimeSha256=$bundleHash"
}
if ($BundledPythonRuntimePath) {
  $pythonRuntime = Resolve-Path -LiteralPath $BundledPythonRuntimePath -ErrorAction Stop
  if (-not (Test-Path -LiteralPath $pythonRuntime -PathType Container)) {
    throw "Bundled Python runtime was not found: $BundledPythonRuntimePath"
  }
  $pythonCandidates = @(
    (Join-Path $pythonRuntime.Path "python.exe"),
    (Join-Path $pythonRuntime.Path "Scripts\python.exe")
  )
  if (-not ($pythonCandidates | Where-Object { Test-Path -LiteralPath $_ -PathType Leaf })) {
    throw "Bundled Python runtime must contain python.exe or Scripts\python.exe: $($pythonRuntime.Path)"
  }
  $libreOffice = Resolve-Path -LiteralPath $BundledLibreOfficePath -ErrorAction Stop
  if (-not (Test-Path -LiteralPath $libreOffice -PathType Container)) {
    throw "Bundled LibreOffice runtime was not found: $BundledLibreOfficePath"
  }
  $isccArguments += "/DBundledApplicationRoot=$projectRoot"
  $isccArguments += "/DBundledPythonRuntimePath=$($pythonRuntime.Path)"
  $isccArguments += "/DBundledLibreOfficePath=$($libreOffice.Path)"
  $isccArguments += "/DFastLocalBuild=1"
}
& $iscc @isccArguments (Join-Path $installerRoot "PredixaLearnDesktop.iss")
if ($LASTEXITCODE -ne 0) { throw "Inno Setup could not build the PredixaLearn installer." }
$installer = Join-Path $output "PredixaLearn-Setup-$Version.exe"
if (-not (Test-Path -LiteralPath $installer)) { throw "Inno Setup did not create the expected installer." }
if ($Sign) { & (Join-Path $PSScriptRoot "sign_windows_artifact.ps1") -Path $installer }
$hash = (Get-FileHash -LiteralPath $installer -Algorithm SHA256).Hash.ToLowerInvariant()
Set-Content -LiteralPath ($installer + ".sha256") -Value "$hash  $(Split-Path $installer -Leaf)" -Encoding ASCII
$payload = @(Get-Item -LiteralPath $installer) + @(
  Get-ChildItem -LiteralPath $output -File -Filter "PredixaLearn-Setup-$Version*.bin" -ErrorAction SilentlyContinue
)
$checksumLines = foreach ($file in $payload | Sort-Object Name -Unique) {
  $fileHash = (Get-FileHash -LiteralPath $file.FullName -Algorithm SHA256).Hash.ToLowerInvariant()
  "$fileHash  $($file.Name)"
}
Set-Content -LiteralPath (Join-Path $output "PredixaLearn-Setup-$Version-checksums.txt") `
  -Value $checksumLines -Encoding ASCII
Write-Output "Created $installer"
