<#
.SYNOPSIS
    Compatibility setup launcher for the relocated OCR studio.
#>

[CmdletBinding()]
param(
    [ValidateSet("cu118", "cu126", "cu129")]
    [string]$CudaVersion = "cu129",
    [string]$PaddleVersion = "3.3.0",
    [switch]$Development
)

$ErrorActionPreference = "Stop"
$Root = $PSScriptRoot
$env:PREDIXALEARN_VENV_DIR = Join-Path $Root ".venv"
$env:PREDIXALEARN_TOOLS_ROOT = Join-Path $Root ".tools"
& (Join-Path $Root "apps\ocr-studio\setup_env.ps1") @PSBoundParameters
