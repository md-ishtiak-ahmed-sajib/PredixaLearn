<# Root release entry point; the packaging implementation stays with the OCR app. #>
[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string]$OutputPath,
    [string]$Version = "dev",
    [switch]$IncludeRuntime,
    [string]$RuntimePath
)

$ErrorActionPreference = "Stop"
$Root = (Resolve-Path (Join-Path $PSScriptRoot "..\.." -Resolve)).Path
& (Join-Path $Root "apps\ocr-studio\scripts\build_release.ps1") @PSBoundParameters
