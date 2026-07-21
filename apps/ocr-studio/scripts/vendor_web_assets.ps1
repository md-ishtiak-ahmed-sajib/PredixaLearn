$ErrorActionPreference = "Stop"
$projectRoot = Split-Path -Parent $PSScriptRoot
$nodeModulesRoots = @(
    (Join-Path $projectRoot "node_modules"),
    ([IO.Path]::GetFullPath((Join-Path $projectRoot "..\..\node_modules")))
) | Where-Object { Test-Path -LiteralPath $_ -PathType Container }
if ($nodeModulesRoots.Count -eq 0) {
    throw "Install workspace browser dependencies with npm ci at the repository root first."
}
$vendorRoot = Join-Path $projectRoot "web\static\vendor"
$fontRoot = Join-Path $projectRoot "web\static\fonts"
New-Item -ItemType Directory -Path $vendorRoot -Force | Out-Null
New-Item -ItemType Directory -Path $fontRoot -Force | Out-Null
$licenseRoot = Join-Path $vendorRoot "licenses"
New-Item -ItemType Directory -Path $licenseRoot -Force | Out-Null

$assets = @{
    "node_modules\@fontsource-variable\manrope\files\manrope-latin-wght-normal.woff2" = "web\static\fonts\manrope-latin-wght-normal.woff2"
    "node_modules\@fontsource-variable\manrope\LICENSE" = "web\static\fonts\MANROPE-LICENSE.txt"
    "node_modules\dompurify\dist\purify.min.js" = "web\static\vendor\purify.min.js"
    "node_modules\marked\lib\marked.umd.js" = "web\static\vendor\marked.umd.js"
    "node_modules\pdfjs-dist\build\pdf.min.mjs" = "web\static\vendor\pdf.min.mjs"
    "node_modules\pdfjs-dist\build\pdf.worker.min.mjs" = "web\static\vendor\pdf.worker.min.mjs"
    "node_modules\swagger-ui-dist\swagger-ui-bundle.js" = "web\static\vendor\swagger-ui-bundle.js"
    "node_modules\swagger-ui-dist\swagger-ui.css" = "web\static\vendor\swagger-ui.css"
}

function Resolve-WorkspaceAsset {
    param([Parameter(Mandatory)][string]$RelativePath)

    $moduleRelative = $RelativePath -replace '^node_modules\\', ''
    foreach ($nodeModulesRoot in $nodeModulesRoots) {
        $candidate = Join-Path $nodeModulesRoot $moduleRelative
        if (Test-Path -LiteralPath $candidate -PathType Leaf) {
            return $candidate
        }
    }
    return $null
}

foreach ($entry in $assets.GetEnumerator()) {
    $source = Resolve-WorkspaceAsset $entry.Key
    $destination = Join-Path $projectRoot $entry.Value
    if (-not (Test-Path -LiteralPath $source -PathType Leaf)) {
        throw "Required vendor asset is missing: $source"
    }
    Copy-Item -LiteralPath $source -Destination $destination -Force
}

$licenses = @{
    "node_modules\dompurify\LICENSE" = "web\static\vendor\licenses\DOMPURIFY-LICENSE.txt"
    "node_modules\marked\LICENSE" = "web\static\vendor\licenses\MARKED-LICENSE.txt"
    "node_modules\pdfjs-dist\LICENSE" = "web\static\vendor\licenses\PDFJS-LICENSE.txt"
    "node_modules\swagger-ui-dist\LICENSE" = "web\static\vendor\licenses\SWAGGER-UI-LICENSE.txt"
}
foreach ($entry in $licenses.GetEnumerator()) {
    $source = Resolve-WorkspaceAsset $entry.Key
    $destination = Join-Path $projectRoot $entry.Value
    if (-not (Test-Path -LiteralPath $source -PathType Leaf)) {
        throw "Required vendor license is missing: $source"
    }
    Copy-Item -LiteralPath $source -Destination $destination -Force
}

Get-FileHash -Algorithm SHA256 (($assets.Values + $licenses.Values) | ForEach-Object { Join-Path $projectRoot $_ }) |
    Sort-Object Path |
    ForEach-Object { "{0}  {1}" -f $_.Hash.ToLowerInvariant(), $_.Path.Substring($projectRoot.Length + 1).Replace("\", "/") } |
    Set-Content -LiteralPath (Join-Path $vendorRoot "SHA256SUMS") -Encoding utf8
