<#
.SYNOPSIS
    Remove only regenerable PredixaLearn development caches.
#>

[CmdletBinding(SupportsShouldProcess)]
param(
    [switch]$RemoveValidatedEnvironmentBackup
)

$ErrorActionPreference = "Stop"
$ProjectRoot = [System.IO.Path]::GetFullPath((Split-Path -Parent $PSScriptRoot))
$ProjectPrefix = $ProjectRoot.TrimEnd("\") + "\"
$ProtectedRoots = @(
    ".venv",
    ".tools",
    "app\history",
    "input",
    "logs",
    "output"
) | ForEach-Object {
    [System.IO.Path]::GetFullPath((Join-Path $ProjectRoot $_))
}

function Resolve-SafeCachePath {
    param([Parameter(Mandatory)][string]$Path)

    $Resolved = [System.IO.Path]::GetFullPath($Path)
    if ($Resolved -eq $ProjectRoot -or -not $Resolved.StartsWith(
        $ProjectPrefix,
        [System.StringComparison]::OrdinalIgnoreCase
    )) {
        throw "Refusing to remove a path outside the project: $Resolved"
    }
    $StagingPrefix = $StagingRoot.TrimEnd("\") + "\"
    if ($Resolved.StartsWith($StagingPrefix, [System.StringComparison]::OrdinalIgnoreCase)) {
        return $Resolved
    }
    foreach ($ProtectedRoot in $ProtectedRoots) {
        $ProtectedPrefix = $ProtectedRoot.TrimEnd("\") + "\"
        if (
            $Resolved -eq $ProtectedRoot -or
            $Resolved.StartsWith($ProtectedPrefix, [System.StringComparison]::OrdinalIgnoreCase)
        ) {
            throw "Refusing to remove protected project data: $Resolved"
        }
    }
    return $Resolved
}

$Targets = [System.Collections.Generic.List[string]]::new()
foreach ($RelativePath in @(
    ".pytest_cache",
    ".ruff_cache",
    ".mypy_cache",
    ".pytype",
    ".hypothesis",
    ".ipynb_checkpoints",
    "__pycache__",
    "app\__pycache__"
)) {
    $Candidate = Join-Path $ProjectRoot $RelativePath
    if (Test-Path -LiteralPath $Candidate) {
        $Targets.Add($Candidate)
    }
}

foreach ($SourceRoot in @(
    "app\api",
    "app\core",
    "app\documents",
    "app\storage",
    "app\workflows",
    "tests"
)) {
    $CandidateRoot = Join-Path $ProjectRoot $SourceRoot
    if (-not (Test-Path -LiteralPath $CandidateRoot)) {
        continue
    }
    Get-ChildItem -LiteralPath $CandidateRoot -Directory -Recurse -Force |
        Where-Object Name -eq "__pycache__" |
        ForEach-Object { $Targets.Add($_.FullName) }
}

$TemporaryRoot = Join-Path $ProjectRoot "tmp"
if (Test-Path -LiteralPath $TemporaryRoot) {
    $Targets.Add($TemporaryRoot)
}

$StagingRoot = Join-Path $ProjectRoot "output\.staging"
if (Test-Path -LiteralPath $StagingRoot) {
    Get-ChildItem -LiteralPath $StagingRoot -Directory -Force |
        ForEach-Object { $Targets.Add($_.FullName) }
}

if ($RemoveValidatedEnvironmentBackup) {
    $ActivePython = Join-Path $ProjectRoot ".venv\Scripts\python.exe"
    if (-not (Test-Path -LiteralPath $ActivePython)) {
        throw "The active virtual environment is unavailable; backup removal is unsafe."
    }
    & $ActivePython -m pip check
    if ($LASTEXITCODE -ne 0) {
        throw "The active virtual environment failed pip check; backups were preserved."
    }
    Get-ChildItem -LiteralPath $ProjectRoot -Directory -Filter ".venv-backup-*" -Force |
        ForEach-Object { $Targets.Add($_.FullName) }
}

$Removed = 0
foreach ($Target in $Targets | Sort-Object -Unique) {
    $SafeTarget = Resolve-SafeCachePath -Path $Target
    if ($PSCmdlet.ShouldProcess($SafeTarget, "Remove regenerable cache")) {
        Remove-Item -LiteralPath $SafeTarget -Recurse -Force
        $Removed += 1
    }
}

Write-Host "Removed $Removed regenerable cache directories."
