<#
.SYNOPSIS
    Build a self-contained, offline-capable PredixaLearn Windows setup executable.

.DESCRIPTION
    Embeds the selected Python runtime and checked LibreOffice runtime directly
    from their source directories into an Inno Setup installer. This avoids a
    duplicate multi-gigabyte staging copy. It is intended for local testing or
    private distribution while the production signed GitHub Release channel is
    being prepared. It does not sign the resulting EXE; Windows may show the
    normal unsigned-app warning.
#>

[CmdletBinding()]
param(
  [Parameter(Mandatory = $true)]
  [string] $OutputDirectory,
  [string] $Version = "local",
  [string] $RuntimePath,
  [string] $PythonPath = "python",
  [switch] $InstallBuildDependencies
)

$ErrorActionPreference = "Stop"
$installerRoot = (Resolve-Path (Join-Path $PSScriptRoot ".." -Resolve)).Path
$workspaceRoot = (Resolve-Path (Join-Path $installerRoot ".." -Resolve)).Path
$projectRoot = (Resolve-Path (Join-Path $workspaceRoot "apps\ocr-studio" -Resolve)).Path
$output = [IO.Path]::GetFullPath($OutputDirectory)
New-Item -ItemType Directory -Force -Path $output | Out-Null

if (-not $RuntimePath) {
  $RuntimePath = Join-Path $workspaceRoot ".venv"
}
$runtime = Resolve-Path -LiteralPath $RuntimePath -ErrorAction Stop
$libreOffice = Join-Path $workspaceRoot ".tools\libreoffice"
if (-not (Test-Path -LiteralPath $libreOffice -PathType Container)) {
  throw "The validated LibreOffice runtime is required: $libreOffice"
}

# Inno Setup reads these trees directly.  This avoids copying and hashing
# approximately 60,000 files before the installer compiler sees them.
& (Join-Path $PSScriptRoot "build_desktop_installer.ps1") `
  -OutputDirectory $output `
  -Version $Version `
  -BundledPythonRuntimePath $runtime.Path `
  -BundledLibreOfficePath $libreOffice `
  -PythonPath $PythonPath `
  -InstallBuildDependencies:$InstallBuildDependencies
if (-not $?) { throw "Could not create the self-contained PredixaLearn installer." }

Write-Output "Created offline-capable installer: $(Join-Path $output "PredixaLearn-Setup-$Version.exe")"
