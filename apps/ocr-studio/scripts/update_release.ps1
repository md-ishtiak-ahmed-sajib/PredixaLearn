[CmdletBinding()]
param(
  [Parameter(Mandatory = $true)]
  [string] $PackagePath,
  [string] $InstallPath = "$env:LOCALAPPDATA\Programs\PredixaLearn",
  [switch] $CreateDesktopShortcut,
  [switch] $NoShortcuts
)

$ErrorActionPreference = "Stop"
& (Join-Path $PSScriptRoot "install_release.ps1") @PSBoundParameters
