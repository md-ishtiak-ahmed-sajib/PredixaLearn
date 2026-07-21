; Browser-only PredixaLearn desktop installer. Build with Inno Setup 7.
#define AppName "PredixaLearn"
#ifndef AppVersion
  #define AppVersion "0.0.0"
#endif
#ifndef BootstrapperPath
  #define BootstrapperPath "..\dist\PredixaLearnDesktopBootstrapper.exe"
#endif
#ifndef RuntimeManifestUrl
  #define RuntimeManifestUrl "https://github.com/md-ishtiak-ahmed-sajib/PredixaLearn/releases/latest/download/predixalearn-desktop-manifest.json"
#endif
#ifdef BundledRuntimePath
  #ifndef BundledRuntimeSha256
    #error "BundledRuntimeSha256 is required whenever BundledRuntimePath is supplied."
  #endif
#endif
#ifdef BundledPythonRuntimePath
  #ifndef BundledApplicationRoot
    #error "BundledApplicationRoot is required whenever BundledPythonRuntimePath is supplied."
  #endif
  #ifndef BundledLibreOfficePath
    #error "BundledLibreOfficePath is required whenever BundledPythonRuntimePath is supplied."
  #endif
#endif
#if defined(BundledRuntimePath) && defined(BundledPythonRuntimePath)
  #error "Specify either BundledRuntimePath or BundledPythonRuntimePath, not both."
#endif

[Setup]
AppId={{A3CB6B2B-D30A-4A0C-92B5-4A5A88A0E0B7}
AppName={#AppName}
AppVersion={#AppVersion}
AppPublisher=Md Ishtiak Ahmed Sajib
DefaultDirName={autopf}\PredixaLearn
DefaultGroupName={#AppName}
DisableProgramGroupPage=yes
PrivilegesRequired=admin
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
OutputBaseFilename=PredixaLearn-Setup-{#AppVersion}
DiskSpanning=yes
DiskSliceSize=2000000000
#ifdef FastLocalBuild
; The offline developer bundle contains mostly already-compressed GPU DLLs.
; Avoid spending hours recompressing them; Inno still performs payload
; integrity checks and splits the installer into manageable .bin files.
Compression=none
SolidCompression=no
#else
Compression=lzma2/ultra64
SolidCompression=yes
#endif
WizardStyle=modern
UninstallDisplayIcon={app}\desktop\PredixaLearnDesktopBootstrapper.exe

[Files]
Source: "{#BootstrapperPath}"; DestDir: "{app}\desktop"; DestName: "PredixaLearnDesktopBootstrapper.exe"; Flags: ignoreversion
Source: "scripts\launch_desktop.ps1"; DestDir: "{app}\scripts"; Flags: ignoreversion
#ifdef BundledRuntimePath
Source: "{#BundledRuntimePath}"; DestDir: "{tmp}"; DestName: "PredixaLearn-runtime.zip"; Flags: deleteafterinstall
#endif
#ifdef BundledPythonRuntimePath
; List source packages deliberately instead of recursing through app\*.  The
; repository can contain legacy app\history runtime records, and user OCR
; data must never be distributed in a desktop installer.
Source: "{#BundledApplicationRoot}\app\*.py"; DestDir: "{tmp}\PredixaLearn-runtime\app"; Excludes: "*.pyc,*.pyo"; Flags: deleteafterinstall
Source: "{#BundledApplicationRoot}\app\api\*"; DestDir: "{tmp}\PredixaLearn-runtime\app\api"; Excludes: "__pycache__\*,*.pyc,*.pyo"; Flags: recursesubdirs createallsubdirs deleteafterinstall
Source: "{#BundledApplicationRoot}\app\core\*"; DestDir: "{tmp}\PredixaLearn-runtime\app\core"; Excludes: "__pycache__\*,*.pyc,*.pyo"; Flags: recursesubdirs createallsubdirs deleteafterinstall
Source: "{#BundledApplicationRoot}\app\documents\*"; DestDir: "{tmp}\PredixaLearn-runtime\app\documents"; Excludes: "__pycache__\*,*.pyc,*.pyo"; Flags: recursesubdirs createallsubdirs deleteafterinstall
Source: "{#BundledApplicationRoot}\app\education\*"; DestDir: "{tmp}\PredixaLearn-runtime\app\education"; Excludes: "__pycache__\*,*.pyc,*.pyo"; Flags: recursesubdirs createallsubdirs deleteafterinstall
Source: "{#BundledApplicationRoot}\app\maintenance\*"; DestDir: "{tmp}\PredixaLearn-runtime\app\maintenance"; Excludes: "__pycache__\*,*.pyc,*.pyo"; Flags: recursesubdirs createallsubdirs deleteafterinstall
Source: "{#BundledApplicationRoot}\app\storage\*"; DestDir: "{tmp}\PredixaLearn-runtime\app\storage"; Excludes: "__pycache__\*,*.pyc,*.pyo"; Flags: recursesubdirs createallsubdirs deleteafterinstall
Source: "{#BundledApplicationRoot}\app\workflows\*"; DestDir: "{tmp}\PredixaLearn-runtime\app\workflows"; Excludes: "__pycache__\*,*.pyc,*.pyo"; Flags: recursesubdirs createallsubdirs deleteafterinstall
Source: "{#BundledApplicationRoot}\web\*"; DestDir: "{tmp}\PredixaLearn-runtime\web"; Excludes: "__pycache__\*,*.pyc,*.pyo"; Flags: recursesubdirs createallsubdirs deleteafterinstall
Source: "{#BundledApplicationRoot}\run.py"; DestDir: "{tmp}\PredixaLearn-runtime"; Flags: deleteafterinstall
Source: "{#BundledApplicationRoot}\requirements.txt"; DestDir: "{tmp}\PredixaLearn-runtime"; Flags: deleteafterinstall
Source: "{#BundledApplicationRoot}\constraints.txt"; DestDir: "{tmp}\PredixaLearn-runtime"; Flags: deleteafterinstall
Source: "{#BundledApplicationRoot}\package.json"; DestDir: "{tmp}\PredixaLearn-runtime"; Flags: deleteafterinstall
Source: "{#BundledApplicationRoot}\README.md"; DestDir: "{tmp}\PredixaLearn-runtime"; Flags: deleteafterinstall
Source: "{#BundledApplicationRoot}\scripts\libreoffice.lock.json"; DestDir: "{tmp}\PredixaLearn-runtime\scripts"; Flags: deleteafterinstall
Source: "{#BundledApplicationRoot}\scripts\launch_release.ps1"; DestDir: "{tmp}\PredixaLearn-runtime\scripts"; Flags: deleteafterinstall
Source: "{#BundledPythonRuntimePath}\*"; DestDir: "{tmp}\PredixaLearn-runtime\runtime"; Excludes: "__pycache__\*,*.pyc,*.pyo,*.pdb,*.lib,*.a"; Flags: recursesubdirs createallsubdirs deleteafterinstall
Source: "{#BundledLibreOfficePath}\*"; DestDir: "{tmp}\PredixaLearn-runtime\.tools\libreoffice"; Excludes: "*.pdb,*.lib,*.a"; Flags: recursesubdirs createallsubdirs deleteafterinstall
#endif

[Icons]
Name: "{autoprograms}\PredixaLearn"; Filename: "{sys}\WindowsPowerShell\v1.0\powershell.exe"; Parameters: "-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File ""{app}\scripts\launch_desktop.ps1"""; WorkingDir: "{app}"; IconFilename: "{app}\desktop\PredixaLearnDesktopBootstrapper.exe"
Name: "{autodesktop}\PredixaLearn"; Filename: "{sys}\WindowsPowerShell\v1.0\powershell.exe"; Parameters: "-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File ""{app}\scripts\launch_desktop.ps1"""; WorkingDir: "{app}"; IconFilename: "{app}\desktop\PredixaLearnDesktopBootstrapper.exe"

[UninstallRun]
Filename: "{app}\desktop\PredixaLearnDesktopBootstrapper.exe"; Parameters: "remove-https --install-root ""{app}"""; Flags: runhidden waituntilterminated; RunOnceId: "PredixaLearnRemoveLocalHttps"; Check: FileExists(ExpandConstant('{app}\desktop\PredixaLearnDesktopBootstrapper.exe'))

[Code]
function RunBootstrap(const Arguments: String): Boolean;
var
  ResultCode: Integer;
begin
  Result := Exec(ExpandConstant('{app}\desktop\PredixaLearnDesktopBootstrapper.exe'), ExpandConstant(Arguments), ExpandConstant('{app}'), SW_HIDE, ewWaitUntilTerminated, ResultCode) and (ResultCode = 0);
end;

procedure CurStepChanged(CurStep: TSetupStep);
begin
  if CurStep = ssPostInstall then begin
#ifdef BundledPythonRuntimePath
    if not RunBootstrap('install-bundled-runtime-directory --source "{tmp}\PredixaLearn-runtime" --destination "{app}\runtime"') then begin
      RaiseException('PredixaLearn could not verify and install its bundled local OCR runtime. No browser application was started.');
    end;
#elif defined(BundledRuntimePath)
    if not RunBootstrap('install-bundled-runtime --archive "{tmp}\PredixaLearn-runtime.zip" --destination "{app}\runtime" --sha256 "{#BundledRuntimeSha256}"') then begin
      RaiseException('PredixaLearn could not verify and install its bundled local OCR runtime. No browser application was started.');
    end;
#else
    if not RunBootstrap('download-runtime --manifest-url "{#RuntimeManifestUrl}" --destination "{app}\runtime"') then begin
      RaiseException('PredixaLearn could not download and verify its signed local OCR runtime. No browser application was started.');
    end;
#endif
    if not RunBootstrap('install-https --install-root "{app}"') then begin
      RaiseException('PredixaLearn could not finish its automatic local HTTPS setup. No manual hosts-file or certificate configuration is required; rerun Repair as administrator. The installer did not fall back to insecure HTTP.');
    end;
  end;
end;
