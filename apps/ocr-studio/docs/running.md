# Running PredixaLearn

Use the root compatibility launchers so local commands remain stable while
their implementation stays organized under `apps/ocr-studio`:

```powershell
./setup_env.ps1 -Development
./start_web.bat

# Start without opening the browser
./.venv/Scripts/python.exe ./run.py --no-browser
```

The development launcher waits for `GET /api/v1/health` before opening
`http://127.0.0.1:8000`. The same server hosts the UI, REST API, static assets,
and `/docs`.

## Installed Windows desktop

Install the signed `PredixaLearn-Setup-<version>.exe` as administrator. The
installer performs the local HTTPS setup automatically:

1. It creates a unique local PredixaLearn root and leaf certificate for
   `app.predixalearn.com`.
2. It installs the root into the Windows trusted Root store and restricts the
   generated private-key directory.
3. It adds one marked hosts entry mapping the hostname to `127.0.0.1`.
4. It verifies the certificate chain, hostname, key match, trust store, and
   hosts mapping before completing.

No manual hosts-file editing, certificate import, public domain, Caddy setup,
or browser configuration is required. The desktop shortcut starts the quiet
tray host and opens the default browser at
`https://app.predixalearn.com/` after HTTPS health is ready. The service
binds only to loopback port 443. A second launch opens the existing healthy
instance.

If port 443 is already occupied, or trust verification fails, setup stops
without changing to HTTP or another port. Run the installer **Repair** option
as administrator after resolving the conflict. A missing runtime or incomplete
TLS state produces the same repair guidance rather than starting an unsafe
server.

The tray menu provides **Open PredixaLearn** and **Exit**. The browser also has
**Quit PredixaLearn**. Both paths refuse to stop while OCR work is active; finish
or cancel the job first. Uninstall removes only the installed program files,
shortcut, PredixaLearn-owned hosts entry, and generated certificate. It does not
remove `%LOCALAPPDATA%\PredixaLearn` output, uploads, History, or logs.

After the first installation, the local desktop app does not need an internet
connection to run. Internet is required only for downloading a signed runtime
during an online installation or update.

The first launch after the PredixaLearn rename copies the previous default
local output, History, and log folders into `%LOCALAPPDATA%\PredixaLearn`.
The originals remain in place; `.runtime-migration-v2.json` prevents duplicate
copies on later launches.

Healthy installations report `status: "ready"` only when Paddle's compiled and loaded cuDNN versions match and the pinned project-local LibreOffice renderer passes its version check. A mismatch or missing renderer remains visible as `degraded` and must not be hidden; OCR still runs and a DOCX-rendering failure is reported as a per-result warning.

`setup_env.ps1` also runs `scripts/setup_libreoffice.ps1`. The latter verifies the official LibreOffice 26.2.4 x86-64 MSI checksum and publisher signature, performs a project-local administrative extraction under `.tools`, and smoke-converts a DOCX before promotion. It does not register LibreOffice, edit PATH, create file associations, or use an installed office application.

Stop the server with `Ctrl+C`. Shutdown stops the bounded job executor, releases engine references, and clears the CUDA cache.

Remove regenerable development caches after testing with:

```powershell
./scripts/clean_caches.ps1
```

The cleaner is restricted to Python/tool caches, DOCX smoke-test directories, and orphaned staging job directories. It refuses to remove the virtual environment, `.tools` LibreOffice runtime, named outputs, uploads, logs, or persistent History.

After the active environment has passed the complete test and GPU validation suite, stale rollback environments can be removed explicitly with `./scripts/clean_caches.ps1 -RemoveValidatedEnvironmentBackup`. The cleaner runs `pip check` again before accepting that option.
