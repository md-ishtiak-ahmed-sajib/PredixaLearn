<#
.SYNOPSIS
    Install the pinned x86-64 LibreOffice runtime inside this project only.
#>

[CmdletBinding()]
param(
    [switch]$Force
)

$ErrorActionPreference = "Stop"
Add-Type -AssemblyName System.Net.Http
$ProjectRoot = [IO.Path]::GetFullPath((Split-Path -Parent $PSScriptRoot))
$ProjectPrefix = $ProjectRoot.TrimEnd("\") + "\"
$LockPath = Join-Path $PSScriptRoot "libreoffice.lock.json"
$Lock = Get-Content -LiteralPath $LockPath -Raw | ConvertFrom-Json
$ToolsRoot = if ($env:PREDIXALEARN_TOOLS_ROOT) {
    [IO.Path]::GetFullPath($env:PREDIXALEARN_TOOLS_ROOT)
} else {
    Join-Path $ProjectRoot ".tools"
}
$ToolsPrefix = $ToolsRoot.TrimEnd("\") + "\"
$DownloadRoot = Join-Path $ToolsRoot "downloads"
$LibreOfficeRoot = Join-Path $ToolsRoot "libreoffice"
$InstallRoot = [IO.Path]::GetFullPath((Join-Path $ToolsRoot "libreoffice\$($Lock.version)"))
$ExpectedExecutable = [IO.Path]::GetFullPath(
    (Join-Path $InstallRoot $Lock.executable_relative_path)
)
$InstallerName = Split-Path -Leaf ([Uri]$Lock.url).AbsolutePath
$InstallerPath = Join-Path $DownloadRoot $InstallerName
$PartialPath = "$InstallerPath.part"
$StagingRoot = Join-Path $LibreOfficeRoot ".staging-$([Guid]::NewGuid().ToString('N'))"
$BackupRoot = Join-Path $LibreOfficeRoot ".backup-$([Guid]::NewGuid().ToString('N'))"
$LogRoot = Join-Path $ProjectRoot "logs"
$InstallLog = Join-Path $LogRoot "libreoffice-install.log"
$VenvPython = if ($env:PREDIXALEARN_VENV_DIR) {
    Join-Path ([IO.Path]::GetFullPath($env:PREDIXALEARN_VENV_DIR)) "Scripts\python.exe"
} else {
    Join-Path $ProjectRoot ".venv\Scripts\python.exe"
}

function Assert-ProjectPath {
    param([Parameter(Mandatory)][string]$Path)

    $Resolved = [IO.Path]::GetFullPath($Path)
    if (-not ($Resolved.StartsWith($ProjectPrefix, [StringComparison]::OrdinalIgnoreCase) -or
        $Resolved.StartsWith($ToolsPrefix, [StringComparison]::OrdinalIgnoreCase))) {
        throw "LibreOffice path escapes the OCR studio: $Resolved"
    }
    return $Resolved
}

function Assert-NoReparsePoint {
    param([Parameter(Mandatory)][string]$Path)

    $Current = [IO.DirectoryInfo]$Path
    while ($null -ne $Current -and (
        $Current.FullName.StartsWith($ProjectPrefix, [StringComparison]::OrdinalIgnoreCase) -or
        $Current.FullName.StartsWith($ToolsPrefix, [StringComparison]::OrdinalIgnoreCase)
    )) {
        if ($Current.Exists -and ($Current.Attributes -band [IO.FileAttributes]::ReparsePoint)) {
            throw "Refusing to use a reparse point for the project-local LibreOffice runtime."
        }
        $Current = $Current.Parent
    }
}

function Get-FileSha256 {
    param([Parameter(Mandatory)][string]$Path)
    return (Get-FileHash -LiteralPath $Path -Algorithm SHA256).Hash.ToLowerInvariant()
}

function Get-PeMachine {
    param([Parameter(Mandatory)][string]$Path)

    $Stream = [IO.File]::OpenRead($Path)
    $Reader = [IO.BinaryReader]::new($Stream)
    try {
        if ($Reader.ReadUInt16() -ne 0x5A4D) {
            throw "The extracted LibreOffice executable is not a valid PE file."
        }
        $Stream.Position = 0x3C
        $PeOffset = $Reader.ReadUInt32()
        if ($PeOffset -gt ($Stream.Length - 6)) {
            throw "The extracted LibreOffice executable has an invalid PE header."
        }
        $Stream.Position = $PeOffset
        if ($Reader.ReadUInt32() -ne 0x00004550) {
            throw "The extracted LibreOffice executable has an invalid PE signature."
        }
        return $Reader.ReadUInt16()
    } finally {
        $Reader.Dispose()
        $Stream.Dispose()
    }
}

function Invoke-BoundedDownload {
    param(
        [Parameter(Mandatory)][Uri]$Uri,
        [Parameter(Mandatory)][string]$Destination,
        [Parameter(Mandatory)][long]$MaximumBytes
    )

    [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
    $Handler = [Net.Http.HttpClientHandler]::new()
    $Handler.AllowAutoRedirect = $true
    $Handler.MaxAutomaticRedirections = 5
    $Client = [Net.Http.HttpClient]::new($Handler)
    $Client.Timeout = [Threading.Timeout]::InfiniteTimeSpan
    $Response = $null
    $Input = $null
    $Output = $null
    try {
        $Response = $Client.GetAsync(
            $Uri,
            [Net.Http.HttpCompletionOption]::ResponseHeadersRead
        ).GetAwaiter().GetResult()
        [void]$Response.EnsureSuccessStatusCode()
        if ($Response.RequestMessage.RequestUri.Scheme -ne "https") {
            throw "LibreOffice download redirected to an insecure transport."
        }
        $DeclaredLength = $Response.Content.Headers.ContentLength
        if ($null -ne $DeclaredLength -and $DeclaredLength -gt $MaximumBytes) {
            throw "LibreOffice download exceeds the configured size limit."
        }
        $Input = $Response.Content.ReadAsStreamAsync().GetAwaiter().GetResult()
        $Output = [IO.File]::Open($Destination, [IO.FileMode]::CreateNew, [IO.FileAccess]::Write)
        $Buffer = [byte[]]::new(1048576)
        [long]$Total = 0
        while (($Read = $Input.Read($Buffer, 0, $Buffer.Length)) -gt 0) {
            $Total += $Read
            if ($Total -gt $MaximumBytes) {
                throw "LibreOffice download exceeded the configured size limit."
            }
            $Output.Write($Buffer, 0, $Read)
        }
    } finally {
        if ($null -ne $Output) { $Output.Dispose() }
        if ($null -ne $Input) { $Input.Dispose() }
        if ($null -ne $Response) { $Response.Dispose() }
        $Client.Dispose()
        $Handler.Dispose()
    }
}

function Invoke-HiddenProcess {
    param(
        [Parameter(Mandatory)][string]$FilePath,
        [Parameter(Mandatory)][string[]]$ArgumentList,
        [Parameter(Mandatory)][int]$TimeoutSeconds,
        [string]$WorkingDirectory = $ProjectRoot
    )

    function ConvertTo-NativeArgument {
        param([AllowEmptyString()][string]$Value)

        if ($Value.Length -eq 0) { return '""' }
        if ($Value -notmatch '[\s"]') { return $Value }
        $Builder = [Text.StringBuilder]::new('"')
        $Backslashes = 0
        foreach ($Character in $Value.ToCharArray()) {
            if ($Character -eq '\') {
                $Backslashes += 1
                continue
            }
            if ($Character -eq '"') {
                [void]$Builder.Append(('\' * ($Backslashes * 2 + 1)))
                [void]$Builder.Append('"')
            } else {
                if ($Backslashes) { [void]$Builder.Append(('\' * $Backslashes)) }
                [void]$Builder.Append($Character)
            }
            $Backslashes = 0
        }
        if ($Backslashes) { [void]$Builder.Append(('\' * ($Backslashes * 2))) }
        [void]$Builder.Append('"')
        return $Builder.ToString()
    }

    $StartInfo = [Diagnostics.ProcessStartInfo]::new()
    $StartInfo.FileName = $FilePath
    $StartInfo.WorkingDirectory = $WorkingDirectory
    $StartInfo.UseShellExecute = $false
    $StartInfo.CreateNoWindow = $true
    $StartInfo.RedirectStandardOutput = $true
    $StartInfo.RedirectStandardError = $true
    $StartInfo.Arguments = ($ArgumentList | ForEach-Object {
        ConvertTo-NativeArgument -Value $_
    }) -join " "
    $Process = [Diagnostics.Process]::new()
    $Process.StartInfo = $StartInfo
    try {
        if (-not $Process.Start()) { throw "Failed to start $FilePath" }
        if (-not $Process.WaitForExit($TimeoutSeconds * 1000)) {
            try {
                $TaskkillInfo = [Diagnostics.ProcessStartInfo]::new()
                $TaskkillInfo.FileName = "$env:SystemRoot\System32\taskkill.exe"
                $TaskkillInfo.Arguments = "/PID $($Process.Id) /T /F"
                $TaskkillInfo.UseShellExecute = $false
                $TaskkillInfo.CreateNoWindow = $true
                $Taskkill = [Diagnostics.Process]::Start($TaskkillInfo)
                if ($null -ne $Taskkill) {
                    [void]$Taskkill.WaitForExit(10000)
                    $Taskkill.Dispose()
                }
            } catch {
                try { $Process.Kill() } catch { }
            }
            throw "$([IO.Path]::GetFileName($FilePath)) exceeded the timeout."
        }
        $Stdout = $Process.StandardOutput.ReadToEnd().Trim()
        $Stderr = $Process.StandardError.ReadToEnd().Trim()
        if ($Process.ExitCode -ne 0) {
            throw "$([IO.Path]::GetFileName($FilePath)) failed with exit code $($Process.ExitCode)."
        }
        return [pscustomobject]@{ Stdout = $Stdout; Stderr = $Stderr }
    } finally {
        $Process.Dispose()
    }
}

function Set-ProjectBootstrap {
    param(
        [Parameter(Mandatory)][string]$Executable,
        [Parameter(Mandatory)][string]$RuntimeRoot
    )

    $Bootstrap = Join-Path (Split-Path -Parent $Executable) "bootstrap.ini"
    if (-not (Test-Path -LiteralPath $Bootstrap -PathType Leaf)) {
        throw "The LibreOffice bootstrap configuration is missing."
    }
    $Text = Get-Content -LiteralPath $Bootstrap -Raw
    foreach ($Required in @("[Bootstrap]", "InstallMode=", "ProductKey=")) {
        if (-not $Text.Contains($Required)) {
            throw "The LibreOffice bootstrap configuration is incomplete."
        }
    }
    if ($Text -match '(?im)^UserInstallation=') {
        $Text = [Regex]::Replace(
            $Text,
            '(?im)^UserInstallation=.*$',
            'UserInstallation=$ORIGIN/../Data/settings'
        )
    } else {
        $Text = $Text.TrimEnd() + "`r`n" + 'UserInstallation=$ORIGIN/../Data/settings' + "`r`n"
    }
    # Windows PowerShell's `-Encoding UTF8` writes a BOM. LibreOffice's
    # bootstrap parser treats that BOM as corrupt, so write UTF-8 explicitly
    # without a preamble.
    $Utf8NoBom = [Text.UTF8Encoding]::new($false)
    [IO.File]::WriteAllText($Bootstrap, $Text, $Utf8NoBom)
    New-Item -ItemType Directory -Path (Join-Path $RuntimeRoot "Data\settings") `
        -Force | Out-Null
}

function Test-WriterConversion {
    param(
        [Parameter(Mandatory)][string]$Executable,
        [Parameter(Mandatory)][string]$RuntimeRoot
    )

    if (-not (Test-Path -LiteralPath $VenvPython -PathType Leaf)) {
        throw "The project virtual environment is required for LibreOffice validation."
    }
    $SmokeRoot = Join-Path $ToolsRoot ".libreoffice-validation-$([Guid]::NewGuid().ToString('N'))"
    try {
        $SmokeDocx = Join-Path $SmokeRoot "libreoffice-smoke.docx"
        New-Item -ItemType Directory -Path $SmokeRoot -Force | Out-Null
        $SmokeCode = @'
from docx import Document
from docx.shared import Mm

document = Document()
document.sections[0].page_width = Mm(210)
document.sections[0].page_height = Mm(297)
document.add_heading("PredixaLearn LibreOffice validation", level=1)
document.add_paragraph("Project-local Writer document rendering is ready.")
document.save(r"__SMOKE_DOCX__")
'@.Replace("__SMOKE_DOCX__", $SmokeDocx.Replace("\", "\\"))
        Invoke-HiddenProcess -FilePath $VenvPython -ArgumentList @("-c", $SmokeCode) `
            -TimeoutSeconds 30 | Out-Null

        $Modes = @("isolated", "persistent")
        foreach ($Mode in $Modes) {
            $OutputRoot = Join-Path $SmokeRoot "$Mode-out"
            New-Item -ItemType Directory -Path $OutputRoot -Force | Out-Null
            $Arguments = @("--headless", "--nologo", "--nodefault", "--norestore")
            if ($Mode -eq "isolated") {
                $Profile = Join-Path $SmokeRoot "isolated-profile"
                New-Item -ItemType Directory -Path $Profile -Force | Out-Null
                $Arguments += "-env:UserInstallation=$(([Uri]$Profile).AbsoluteUri)"
            }
            $Arguments += @(
                "--convert-to", "pdf:writer_pdf_Export", "--outdir", $OutputRoot, $SmokeDocx
            )
            Invoke-HiddenProcess -FilePath $Executable -ArgumentList $Arguments `
                -TimeoutSeconds 120 | Out-Null
            $SmokePdf = Join-Path $OutputRoot "libreoffice-smoke.pdf"
            if (-not (Test-Path -LiteralPath $SmokePdf -PathType Leaf) -or `
                (Get-Item $SmokePdf).Length -lt 1000) {
                throw "LibreOffice $Mode Writer conversion did not create a valid PDF."
            }
            $PdfCode = @'
import numpy as np
import pypdfium2 as pdfium

document = pdfium.PdfDocument(r"__SMOKE_PDF__")
assert len(document) >= 1
page = document[0]
bitmap = page.render(scale=0.75)
pixels = np.asarray(bitmap.to_numpy())
luminance = pixels[:, :, :3].astype(np.float32).mean(axis=2)
assert float(np.count_nonzero(luminance < 245)) / float(luminance.size) > 0.00005
bitmap.close()
page.close()
document.close()
'@.Replace("__SMOKE_PDF__", $SmokePdf.Replace("\", "\\"))
            Invoke-HiddenProcess -FilePath $VenvPython -ArgumentList @("-c", $PdfCode) `
                -TimeoutSeconds 30 | Out-Null
        }
    } finally {
        if (Test-Path -LiteralPath $SmokeRoot) {
            [IO.Directory]::Delete($SmokeRoot, $true)
        }
    }
}

function Test-LibreOfficeRuntime {
    param(
        [Parameter(Mandatory)][string]$Executable,
        [Parameter(Mandatory)][string]$RuntimeRoot
    )

    if (-not (Test-Path -LiteralPath $Executable -PathType Leaf)) { return $false }
    try {
        if ((Get-PeMachine -Path $Executable) -ne 0x8664) { return $false }
        $Bootstrap = Join-Path (Split-Path -Parent $Executable) "bootstrap.ini"
        $BootstrapText = Get-Content -LiteralPath $Bootstrap -Raw
        if ($BootstrapText -notmatch `
            '(?im)^UserInstallation=\$ORIGIN/\.\./Data/settings\s*$') { return $false }
        $Version = Invoke-HiddenProcess -FilePath $Executable -ArgumentList @(
            "--headless", "--version"
        ) -TimeoutSeconds 60
        if ($Version.Stdout -notmatch "LibreOffice\s+$([Regex]::Escape($Lock.version))") {
            return $false
        }
        Test-WriterConversion -Executable $Executable -RuntimeRoot $RuntimeRoot
        return $true
    } catch {
        return $false
    }
}

Assert-ProjectPath -Path $InstallRoot | Out-Null
Assert-ProjectPath -Path $ExpectedExecutable | Out-Null
foreach ($GuardedPath in @($ToolsRoot, $DownloadRoot, $LibreOfficeRoot, $InstallRoot)) {
    Assert-NoReparsePoint -Path $GuardedPath
}
if (-not [Environment]::Is64BitOperatingSystem -or -not [Environment]::Is64BitProcess) {
    throw "The pinned LibreOffice runtime requires 64-bit Windows and 64-bit PowerShell."
}
if ($Lock.architecture -ne "x86_64") {
    throw "The LibreOffice lock manifest must target x86-64 Windows."
}

New-Item -ItemType Directory -Path $ToolsRoot, $DownloadRoot, $LibreOfficeRoot, $LogRoot `
    -Force | Out-Null

if (-not $Force -and (Test-LibreOfficeRuntime `
    -Executable $ExpectedExecutable -RuntimeRoot $InstallRoot)) {
    Write-Host "LibreOffice $($Lock.version) is already ready at $InstallRoot" -ForegroundColor Green
    return
}

if (Test-Path -LiteralPath $PartialPath) { [IO.File]::Delete($PartialPath) }

if (
    -not (Test-Path -LiteralPath $InstallerPath -PathType Leaf) -or
    (Get-FileSha256 -Path $InstallerPath) -ne $Lock.sha256
) {
    if (Test-Path -LiteralPath $InstallerPath) { [IO.File]::Delete($InstallerPath) }
    Write-Host "Downloading LibreOffice $($Lock.version) x86-64..." -ForegroundColor Cyan
    try {
        Invoke-BoundedDownload -Uri ([Uri]$Lock.url) -Destination $PartialPath `
            -MaximumBytes ([long]$Lock.maximum_download_bytes)
        if ((Get-FileSha256 -Path $PartialPath) -ne $Lock.sha256) {
            throw "LibreOffice installer checksum verification failed."
        }
        [IO.File]::Move($PartialPath, $InstallerPath)
    } finally {
        if (Test-Path -LiteralPath $PartialPath) { [IO.File]::Delete($PartialPath) }
    }
}

$Signature = Get-AuthenticodeSignature -LiteralPath $InstallerPath
if (
    $Signature.Status -ne [Management.Automation.SignatureStatus]::Valid -or
    $null -eq $Signature.SignerCertificate -or
    $Signature.SignerCertificate.Subject -notmatch "The Document Foundation"
) {
    throw "The LibreOffice installer does not have the expected valid publisher signature."
}

if (Test-Path -LiteralPath $StagingRoot) {
    [IO.Directory]::Delete($StagingRoot, $true)
}
New-Item -ItemType Directory -Path $StagingRoot -Force | Out-Null
try {
    Write-Host "Extracting LibreOffice inside this project..." -ForegroundColor Cyan
    $MsiArguments = @(
        "/a",
        $InstallerPath,
        "/qn",
        "TARGETDIR=$StagingRoot",
        "/L*v",
        $InstallLog
    )
    Invoke-HiddenProcess -FilePath "$env:SystemRoot\System32\msiexec.exe" `
        -ArgumentList $MsiArguments -TimeoutSeconds 300 | Out-Null

    $Candidates = @(Get-ChildItem -LiteralPath $StagingRoot -Filter "soffice.com" `
        -File -Recurse -Force)
    if ($Candidates.Count -ne 1) {
        throw "Expected one LibreOffice console executable, found $($Candidates.Count)."
    }
    $ExtractedExecutable = $Candidates[0].FullName
    $ExtractedRoot = $Candidates[0].Directory.Parent.FullName
    Set-ProjectBootstrap -Executable $ExtractedExecutable -RuntimeRoot $ExtractedRoot
    if ((Get-PeMachine -Path $ExtractedExecutable) -ne 0x8664) {
        throw "The extracted LibreOffice executable is not x86-64."
    }
    $Version = Invoke-HiddenProcess -FilePath $ExtractedExecutable -ArgumentList @(
        "--headless", "--version"
    ) -TimeoutSeconds 60
    if ($Version.Stdout -notmatch "LibreOffice\s+$([Regex]::Escape($Lock.version))") {
        throw "The extracted LibreOffice version does not match the lock manifest."
    }

    if (-not (Test-Path -LiteralPath $VenvPython -PathType Leaf)) {
        throw "The project virtual environment is required for the LibreOffice smoke test."
    }
    $SmokeRoot = Join-Path $StagingRoot ".smoke"
    $SmokeOut = Join-Path $SmokeRoot "out"
    $SmokeProfile = Join-Path $SmokeRoot "profile"
    New-Item -ItemType Directory -Path $SmokeOut, $SmokeProfile -Force | Out-Null
    $SmokeDocx = Join-Path $SmokeRoot "libreoffice-smoke.docx"
    $SmokeCode = @'
from docx import Document
from docx.shared import Mm

document = Document()
document.sections[0].page_width = Mm(210)
document.sections[0].page_height = Mm(297)
document.add_heading("PredixaLearn LibreOffice validation", level=1)
document.add_paragraph("Project-local headless document rendering is ready.")
document.save(r"__SMOKE_DOCX__")
'@.Replace("__SMOKE_DOCX__", $SmokeDocx.Replace("\", "\\"))
    Invoke-HiddenProcess -FilePath $VenvPython -ArgumentList @("-c", $SmokeCode) `
        -TimeoutSeconds 30 | Out-Null
    $ProfileUri = ([Uri]$SmokeProfile).AbsoluteUri
    Invoke-HiddenProcess -FilePath $ExtractedExecutable -ArgumentList @(
        "--headless",
        "--nologo",
        "--nodefault",
        "--norestore",
        "-env:UserInstallation=$ProfileUri",
        "--convert-to",
        "pdf:writer_pdf_Export",
        "--outdir",
        $SmokeOut,
        $SmokeDocx
    ) -TimeoutSeconds 120 | Out-Null
    $SmokePdf = Join-Path $SmokeOut "libreoffice-smoke.pdf"
    if (-not (Test-Path -LiteralPath $SmokePdf -PathType Leaf) -or (Get-Item $SmokePdf).Length -lt 1000) {
        throw "LibreOffice smoke conversion did not create a valid PDF."
    }

    if (Test-Path -LiteralPath $InstallRoot) {
        if (Test-Path -LiteralPath $BackupRoot) { [IO.Directory]::Delete($BackupRoot, $true) }
        [IO.Directory]::Move($InstallRoot, $BackupRoot)
    }
    try {
        [IO.Directory]::Move($ExtractedRoot, $InstallRoot)
        if (-not (Test-LibreOfficeRuntime `
            -Executable $ExpectedExecutable -RuntimeRoot $InstallRoot)) {
            throw "The promoted LibreOffice runtime failed validation."
        }
    } catch {
        if (Test-Path -LiteralPath $InstallRoot) { [IO.Directory]::Delete($InstallRoot, $true) }
        if (Test-Path -LiteralPath $BackupRoot) { [IO.Directory]::Move($BackupRoot, $InstallRoot) }
        throw
    }
    if (Test-Path -LiteralPath $BackupRoot) { [IO.Directory]::Delete($BackupRoot, $true) }
    Write-Host "LibreOffice $($Lock.version) is ready at $InstallRoot" -ForegroundColor Green
} finally {
    if (Test-Path -LiteralPath $StagingRoot) { [IO.Directory]::Delete($StagingRoot, $true) }
}
