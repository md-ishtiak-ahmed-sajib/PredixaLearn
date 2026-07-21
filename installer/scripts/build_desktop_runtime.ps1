<# Build the tested runtime ZIP consumed by the signed online installer. #>
[CmdletBinding()]
param(
  [Parameter(Mandatory = $true)]
  [string] $OutputPath,
  [string] $Version = "dev",
  [Parameter(Mandatory = $true)]
  [string] $RuntimePath
)

$ErrorActionPreference = "Stop"
$installerRoot = (Resolve-Path (Join-Path $PSScriptRoot ".." -Resolve)).Path
$workspaceRoot = (Resolve-Path (Join-Path $installerRoot ".." -Resolve)).Path
$releaseBuilder = Join-Path $workspaceRoot "apps\ocr-studio\scripts\build_release.ps1"
& $releaseBuilder `
  -OutputPath $OutputPath -Version $Version -IncludeRuntime -RuntimePath $RuntimePath
if ($LASTEXITCODE -ne 0) { throw "Could not build the PredixaLearn desktop runtime ZIP." }
Write-Output "Attach this verified runtime ZIP to the selected GitHub Release before building the setup EXE."
