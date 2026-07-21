<# Launch the installed browser-only PredixaLearn desktop host without a console. #>
[CmdletBinding()]
param()

$ErrorActionPreference = "Stop"
$installRoot = (Resolve-Path (Join-Path $PSScriptRoot ".." -Resolve)).Path
$root = Join-Path $installRoot "runtime"
$root = (Resolve-Path -LiteralPath $root).Path
$pythonCandidates = @(
  (Join-Path $root "runtime\pythonw.exe"),
  (Join-Path $root "runtime\Scripts\pythonw.exe"),
  (Join-Path $root "pythonw.exe"),
  (Join-Path $root "Scripts\pythonw.exe"),
  (Join-Path $root "runtime\python.exe"),
  (Join-Path $root "runtime\Scripts\python.exe"),
  (Join-Path $root "python.exe"),
  (Join-Path $root "Scripts\python.exe")
)
$python = $pythonCandidates | Where-Object { Test-Path -LiteralPath $_ -PathType Leaf } | Select-Object -First 1
if (-not $python) { throw "The PredixaLearn desktop runtime is missing. Run the signed installer repair option." }

$certificate = Join-Path $installRoot "desktop\tls\server.pem"
$key = Join-Path $installRoot "desktop\tls\server-key.pem"
if (-not (Test-Path -LiteralPath $certificate) -or -not (Test-Path -LiteralPath $key)) {
  throw "PredixaLearn local HTTPS setup is incomplete. Run the signed installer repair option."
}
$bootstrapper = Join-Path $installRoot "desktop\PredixaLearnDesktopBootstrapper.exe"
if (-not (Test-Path -LiteralPath $bootstrapper -PathType Leaf)) {
  throw "PredixaLearn desktop bootstrapper is missing. Run the signed installer repair option."
}
$verify = Start-Process -FilePath $bootstrapper -ArgumentList @("verify-https", "--install-root", $installRoot) -WindowStyle Hidden -Wait -PassThru
if ($verify.ExitCode -ne 0) {
  throw "PredixaLearn local HTTPS trust is incomplete. Run the signed installer repair option."
}

$bytes = New-Object byte[] 48
$generator = [System.Security.Cryptography.RandomNumberGenerator]::Create()
try { $generator.GetBytes($bytes) } finally { $generator.Dispose() }
$env:OCR_DESKTOP_MODE = "1"
$env:OCR_HOST = "127.0.0.1"
$env:OCR_PORT = "443"
$env:OCR_DESKTOP_CONTROL_TOKEN = [Convert]::ToBase64String($bytes)
$env:OCR_DESKTOP_TLS_CERT_PATH = $certificate
$env:OCR_DESKTOP_TLS_KEY_PATH = $key

Start-Process -FilePath $python -ArgumentList @("run.py", "--desktop") -WorkingDirectory $root -WindowStyle Hidden | Out-Null
