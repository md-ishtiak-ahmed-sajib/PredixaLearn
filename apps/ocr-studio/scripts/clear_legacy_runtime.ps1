[CmdletBinding()]
param()

$ErrorActionPreference = "Stop"
$root = (Resolve-Path (Join-Path $PSScriptRoot ".." -Resolve)).Path
$names = @("input", "output", "logs", "tmp")

foreach ($name in $names) {
  $target = Join-Path $root $name
  if (-not (Test-Path -LiteralPath $target)) {
    New-Item -ItemType Directory -Path $target | Out-Null
    continue
  }
  $resolved = (Resolve-Path -LiteralPath $target).Path
  if ((Split-Path -Parent $resolved) -ne $root) {
    throw "Refusing to remove a path outside the project root: $resolved"
  }
  Get-ChildItem -LiteralPath $resolved -Force | Remove-Item -Recurse -Force
}
