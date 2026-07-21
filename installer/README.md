# Windows browser-only desktop release

PredixaLearn is a local web application. The installer starts a private
Python/FastAPI service and opens the user's existing default browser at
`https://app.predixalearn.com/`. It does not bundle Chromium and does not
use Electron, Tauri, CEF, or a reverse-proxy process.

## What the installer configures

The Inno Setup package requires one Windows administrator approval. During
installation it automatically:

- installs and verifies the signed PredixaLearn runtime;
- generates a unique local root certificate and a leaf certificate for
  `app.predixalearn.com`;
- installs the root certificate into the Windows trusted Root store;
- adds exactly one marked hosts entry mapping `app.predixalearn.com` to
  `127.0.0.1`;
- restricts the generated private-key directory;
- verifies the certificate chain, hostname, private-key match, trust-store
  entry, and hosts mapping before reporting success; and
- creates Start Menu and desktop shortcuts.

No manual hosts-file editing, certificate import, public DNS, Caddy download,
Caddyfile, or browser configuration is required. `app.predixalearn.com` is a
local hostname only; it is not a public website or public certificate.

The installed desktop host serves HTTPS directly on `127.0.0.1:443`, waits for
`GET /api/v1/health`, and then opens the default browser. A second shortcut
launch reuses the existing healthy service. The tray icon offers **Open
PredixaLearn** and **Exit**, and the browser provides **Quit PredixaLearn**.
Shutdown is blocked while OCR work is active so a result is not interrupted.

## Failure and repair behavior

The installer and desktop host fail safely when:

- port 443 is already occupied;
- the certificate cannot be trusted or validated;
- the hosts file cannot be updated;
- the signed runtime is unavailable or fails checksum verification; or
- an installed runtime or TLS state is incomplete.

PredixaLearn never falls back to insecure HTTP or an alternate port. Resolve the
reported issue and run the installer **Repair** option as administrator. Repair
is idempotent: it removes and recreates only PredixaLearn's own certificate,
state, and marked hosts mapping, without duplicating entries.

Development remains available at `http://127.0.0.1:8000/`; that URL is used by
local development, CI, and automated tests only.

## Data and uninstall guarantees

Program files and generated desktop TLS state are kept separate from runtime
data. Uninstall removes only PredixaLearn binaries, shortcuts, its marked hosts
entry, and its generated certificate. It preserves:

```text
%LOCALAPPDATA%\PredixaLearn\output
%LOCALAPPDATA%\PredixaLearn\uploads
%LOCALAPPDATA%\PredixaLearn\history
%LOCALAPPDATA%\PredixaLearn\logs
```

The first installation needs internet access when it downloads the signed
runtime. Once installed, the local desktop application remains usable offline.
On its first run after the PredixaLearn rename, the application non-destructively
copies default local output, History, and logs into `%LOCALAPPDATA%\PredixaLearn`.
The prior data remains untouched.

## Building a release

1. Build and attach a tested runtime ZIP to the chosen GitHub Release:

   ```powershell
   ./installer/scripts/build_desktop_runtime.ps1 -OutputPath ./installer/builds/release/PredixaLearn-runtime.zip -Version 1.0.0 -RuntimePath C:\path\to\validated-python-runtime
   ```

2. Configure `PREDIXALEARN_UPDATE_SIGNING_KEY` plus the protected Authenticode
   secrets `WINDOWS_CODESIGN_PFX_BASE64`, `WINDOWS_CODESIGN_PFX_PASSWORD`, and
   `WINDOWS_CODESIGN_TIMESTAMP_URL` in GitHub Actions.

3. Dispatch **Publish signed PredixaLearn desktop installer**, supplying the
   release tag, runtime asset name, and installer version.

For local release work, install Inno Setup 7 and run from the repository root:

```powershell
./installer/scripts/build_desktop_installer.ps1 -OutputDirectory ./installer/builds/release -Version 1.0.0 -InstallBuildDependencies
```

Add `-Sign` only in a secured build environment with the signing secrets.
For a self-contained local bundle, use `build_local_desktop_installer.ps1`.
Keep the generated `.exe` and every accompanying `.bin` file together.
