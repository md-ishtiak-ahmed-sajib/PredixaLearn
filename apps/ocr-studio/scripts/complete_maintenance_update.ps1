<#
  Complete a previously validated runtime update before PredixaLearn starts.
  This script receives only a backend-written plan under LocalAppData; it never
  reads an online manifest, executes manifest commands, or touches OCR data.
#>
[CmdletBinding()]
param(
  [string] $MaintenanceRoot = "$env:LOCALAPPDATA\PredixaLearn\maintenance"
)

$ErrorActionPreference = "Stop"
$root = [System.IO.Path]::GetFullPath($MaintenanceRoot)
$planPath = Join-Path $root "pending-update.json"
if (-not (Test-Path -LiteralPath $planPath -PathType Leaf)) { exit 0 }

function Assert-ChildPath([string] $Path, [string] $Parent, [string] $Message) {
  $child = [System.IO.Path]::GetFullPath($Path)
  $base = [System.IO.Path]::GetFullPath($Parent).TrimEnd('\') + '\'
  if (-not $child.StartsWith($base, [System.StringComparison]::OrdinalIgnoreCase)) { throw $Message }
  return $child
}

$plan = Get-Content -LiteralPath $planPath -Raw | ConvertFrom-Json
if ($plan.schema_version -ne 1 -or -not $plan.job_id -or -not $plan.promotion) {
  throw "The pending maintenance plan is invalid."
}

$backups = @()
try {
  foreach ($item in $plan.promotion) {
    $source = Assert-ChildPath $item.source (Join-Path $root "staging") "The pending source is outside maintenance staging."
    if (-not (Test-Path -LiteralPath $source -PathType Container)) { throw "A staged maintenance component is missing." }
    $target = [System.IO.Path]::GetFullPath([string]$item.target)
    if ($target -match '\\PredixaLearn\\(output|uploads|history|logs)(\\|$)') {
      throw "Maintenance must never target document data."
    }
    $backup = "$target.maintenance-backup-$($plan.job_id)"
    if (Test-Path -LiteralPath $backup) { throw "A previous runtime backup needs attention before maintenance can continue." }
    $parent = Split-Path -Parent $target
    New-Item -ItemType Directory -Force -Path $parent | Out-Null
    if (Test-Path -LiteralPath $target) {
      Move-Item -LiteralPath $target -Destination $backup -Force
      $backups += [pscustomobject]@{ Target = $target; Backup = $backup }
    }
    Move-Item -LiteralPath $source -Destination $target -Force
  }

  $jobPath = Join-Path (Join-Path $root "jobs") "$($plan.job_id).json"
  if (Test-Path -LiteralPath $jobPath) {
    $job = Get-Content -LiteralPath $jobPath -Raw | ConvertFrom-Json
    $job.status = "completed"
    $job.stage = "completed"
    $job.progress = 100
    $job.cancellable = $false
    $job.message = "Runtime maintenance completed. The previous runtime is retained for rollback."
    $job.updated_at = [DateTime]::UtcNow.ToString("o")
    $temporary = "$jobPath.tmp"
    $job | ConvertTo-Json -Depth 8 | Set-Content -LiteralPath $temporary -Encoding UTF8
    Move-Item -LiteralPath $temporary -Destination $jobPath -Force
  }
  Remove-Item -LiteralPath $planPath -Force
  exit 0
}
catch {
  [array]::Reverse($backups)
  foreach ($entry in $backups) {
    if (Test-Path -LiteralPath $entry.Target) { Remove-Item -LiteralPath $entry.Target -Recurse -Force }
    if (Test-Path -LiteralPath $entry.Backup) { Move-Item -LiteralPath $entry.Backup -Destination $entry.Target -Force }
  }
  Write-Error "Maintenance was not promoted; the prior runtime was restored. $($_.Exception.Message)"
  exit 1
}
