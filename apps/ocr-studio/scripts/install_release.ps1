[CmdletBinding()]
param(
  [Parameter(Mandatory = $true)]
  [string] $PackagePath,
  [string] $InstallPath = "$env:LOCALAPPDATA\Programs\PredixaLearn",
  [switch] $CreateDesktopShortcut,
  [switch] $NoShortcuts
)

$ErrorActionPreference = "Stop"
$package = (Resolve-Path -LiteralPath $PackagePath).Path
$checksum = $package + ".sha256"
if (-not (Test-Path -LiteralPath $checksum)) { throw "Missing SHA-256 sidecar: $checksum" }
$expected = ((Get-Content -LiteralPath $checksum -Raw).Trim() -split "\s+")[0].ToLowerInvariant()
$actual = (Get-FileHash -LiteralPath $package -Algorithm SHA256).Hash.ToLowerInvariant()
if ($expected -ne $actual) { throw "Release checksum verification failed" }
$parent = Split-Path -Parent ([IO.Path]::GetFullPath($InstallPath))
New-Item -ItemType Directory -Force -Path $parent | Out-Null
$stage = Join-Path $parent (".predixalearn-install-" + [guid]::NewGuid().ToString("N"))
New-Item -ItemType Directory -Path $stage | Out-Null
function New-PredixaLearnShortcut([string] $ShortcutPath, [string] $LauncherPath) {
  $shell = New-Object -ComObject WScript.Shell
  $shortcut = $shell.CreateShortcut($ShortcutPath)
  $shortcut.TargetPath = (Join-Path $env:SystemRoot "System32\WindowsPowerShell\v1.0\powershell.exe")
  $shortcut.Arguments = "-NoProfile -ExecutionPolicy Bypass -File `"$LauncherPath`""
  $shortcut.WorkingDirectory = Split-Path -Parent $LauncherPath
  $shortcut.IconLocation = (Join-Path (Split-Path -Parent $LauncherPath) "web\static\predixalearn-mark-48.png")
  $shortcut.Save()
}
try {
  Expand-Archive -LiteralPath $package -DestinationPath $stage -Force
  $backup = $null
  if (Test-Path -LiteralPath $InstallPath) {
    $backup = $InstallPath + ".backup-" + (Get-Date -Format "yyyyMMddHHmmss")
    Move-Item -LiteralPath $InstallPath -Destination $backup
  }
  try {
    Move-Item -LiteralPath $stage -Destination $InstallPath
    $stage = $null
    Write-Output "Installed PredixaLearn to $InstallPath"
  }
  catch {
    if (Test-Path -LiteralPath $InstallPath) { Remove-Item -LiteralPath $InstallPath -Recurse -Force }
    if ($backup -and (Test-Path -LiteralPath $backup)) { Move-Item -LiteralPath $backup -Destination $InstallPath }
    throw
  }
  if (-not $NoShortcuts) {
    $launcher = Join-Path $InstallPath "scripts\launch_release.ps1"
    try {
      $startMenu = Join-Path $env:APPDATA "Microsoft\Windows\Start Menu\Programs"
      New-Item -ItemType Directory -Force -Path $startMenu | Out-Null
      New-PredixaLearnShortcut (Join-Path $startMenu "PredixaLearn.lnk") $launcher
      if ($CreateDesktopShortcut) {
        New-PredixaLearnShortcut (Join-Path ([Environment]::GetFolderPath("Desktop")) "PredixaLearn.lnk") $launcher
      }
    }
    catch {
      Write-Warning "Installation succeeded, but a shortcut could not be created: $($_.Exception.Message)"
    }
  }
}
finally {
  if ($stage -and (Test-Path -LiteralPath $stage)) { Remove-Item -LiteralPath $stage -Recurse -Force }
}
