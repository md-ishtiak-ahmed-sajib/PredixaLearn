<#
.SYNOPSIS
    Compatibility wrapper for the environment setup script.
#>

[CmdletBinding()]
param(
    [ValidateSet("cu118", "cu126", "cu129")]
    [string]$CudaVersion = "cu129",
    [string]$PaddleVersion = "3.3.0",
    [switch]$Development
)

$ErrorActionPreference = "Stop"
$SetupScript = Join-Path $PSScriptRoot "scripts\setup_environment.ps1"
& $SetupScript @PSBoundParameters
