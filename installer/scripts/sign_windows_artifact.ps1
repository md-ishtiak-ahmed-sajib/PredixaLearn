[CmdletBinding()]
param(
  [Parameter(Mandatory = $true)]
  [string] $Path
)

$ErrorActionPreference = "Stop"
if (-not $env:WINDOWS_CODESIGN_PFX_BASE64 -or -not $env:WINDOWS_CODESIGN_PFX_PASSWORD -or -not $env:WINDOWS_CODESIGN_TIMESTAMP_URL) {
  throw "WINDOWS_CODESIGN_PFX_BASE64, WINDOWS_CODESIGN_PFX_PASSWORD, and WINDOWS_CODESIGN_TIMESTAMP_URL are required."
}
$artifact = (Resolve-Path -LiteralPath $Path).Path
$signTool = (Get-Command signtool.exe -ErrorAction SilentlyContinue).Source
if (-not $signTool) {
  $signTool = Get-ChildItem -Path "${env:ProgramFiles(x86)}\Windows Kits\10\bin" -Filter signtool.exe -Recurse -ErrorAction SilentlyContinue |
    Sort-Object FullName -Descending | Select-Object -First 1 -ExpandProperty FullName
}
if (-not $signTool) { throw "Windows SignTool was not found." }
$pfxPath = Join-Path ([IO.Path]::GetTempPath()) ("predixalearn-signing-" + [guid]::NewGuid().ToString("N") + ".pfx")
try {
  [IO.File]::WriteAllBytes($pfxPath, [Convert]::FromBase64String($env:WINDOWS_CODESIGN_PFX_BASE64))
  & $signTool sign /fd SHA256 /f $pfxPath /p $env:WINDOWS_CODESIGN_PFX_PASSWORD /tr $env:WINDOWS_CODESIGN_TIMESTAMP_URL /td SHA256 $artifact
  if ($LASTEXITCODE -ne 0) { throw "Authenticode signing failed for $artifact" }
  & $signTool verify /pa /v $artifact
  if ($LASTEXITCODE -ne 0) { throw "Authenticode verification failed for $artifact" }
}
finally {
  Remove-Item -LiteralPath $pfxPath -Force -ErrorAction SilentlyContinue
}
