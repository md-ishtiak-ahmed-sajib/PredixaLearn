[CmdletBinding()]
param(
  [switch] $NoBrowser
)

$ErrorActionPreference = "Stop"
$root = (Resolve-Path (Join-Path $PSScriptRoot ".." -Resolve)).Path
$candidates = @(
  (Join-Path $root "runtime\python.exe"),
  (Join-Path $root "runtime\Scripts\python.exe")
)
$python = $candidates | Where-Object { Test-Path -LiteralPath $_ -PathType Leaf } | Select-Object -First 1
if (-not $python) {
  $command = Get-Command python.exe -ErrorAction SilentlyContinue
  if ($command) { $python = $command.Source }
}
if (-not $python) {
  throw "No bundled Python runtime was found. Install a package built with -IncludeRuntime or install Python 3.12."
}

$arguments = @("run.py")
if ($NoBrowser) { $arguments += "--no-browser" }
Push-Location $root
try {
  & $python @arguments
  exit $LASTEXITCODE
}
finally {
  Pop-Location
}
