<#[.SYNOPSIS
Start the pinned project-local LibreOffice Writer without touching system Office.
#>
[CmdletBinding()]
param(
    [Parameter(Position = 0)]
    [string]$DocumentPath
)

$ErrorActionPreference = "Stop"
$ProjectRoot = [IO.Path]::GetFullPath((Split-Path -Parent $PSScriptRoot))
$ToolsRoot = if ($env:PREDIXALEARN_TOOLS_ROOT) {
    [IO.Path]::GetFullPath($env:PREDIXALEARN_TOOLS_ROOT)
} else {
    Join-Path $ProjectRoot ".tools"
}
$Executable = Join-Path $ToolsRoot "libreoffice\26.2.4\program\soffice.exe"
$Profile = Join-Path $ToolsRoot "libreoffice\26.2.4\Data\settings"

if (-not (Test-Path -LiteralPath $Executable -PathType Leaf)) {
    throw "Project-local LibreOffice is not installed. Run setup_env.ps1 first."
}
New-Item -ItemType Directory -Path $Profile -Force | Out-Null

$Arguments = @(
    "--writer",
    "--nologo",
    "--nodefault",
    "--norestore",
    "-env:UserInstallation=$(([Uri]$Profile).AbsoluteUri)"
)
if ($DocumentPath) {
    if ($DocumentPath.Contains('"')) { throw "The document path contains an invalid quote." }
    $ResolvedDocument = [IO.Path]::GetFullPath($DocumentPath)
    if (-not (Test-Path -LiteralPath $ResolvedDocument -PathType Leaf)) {
        throw "The requested document does not exist."
    }
    $Arguments += $ResolvedDocument
}

$ArgumentString = ($Arguments | ForEach-Object {
    if ($_.Contains('"')) { throw "A LibreOffice argument contains an invalid quote." }
    '"' + $_ + '"'
}) -join " "
Start-Process -FilePath $Executable -ArgumentList $ArgumentString -WorkingDirectory $ProjectRoot `
    -WindowStyle Normal | Out-Null
