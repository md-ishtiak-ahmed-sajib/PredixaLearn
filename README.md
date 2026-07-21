# PredixaLearn monorepo

> **Predict Smarter. Learn Better.**

This repository contains the private, loopback-only Windows OCR studio in
`apps/ocr-studio/` and the optional institution-controlled platform in
`apps/institution-server/`. The institutional service never changes the
standalone app's offline behavior and never uploads existing History
automatically.

Logo masters and brand metadata live in `packages/brand-assets/`. Runtime data belongs in `%LOCALAPPDATA%\PredixaLearn`, never in source control. The root `run.py`, `setup_env.ps1`, `start_web.bat`, and Writer launcher remain compatibility entry points for the OCR studio.

## Common commands

```powershell
# Existing local setup and launch commands remain valid.
.\.venv\Scripts\python.exe .\run.py --no-browser
.\start_web.bat

# OCR checks from the application directory.
Set-Location .\apps\ocr-studio
..\..\.venv\Scripts\python.exe -m pytest -q
..\..\.venv\Scripts\python.exe -m ruff check .

# Workspace browser checks and shared-brand sync.
Set-Location ..\..
npm run assets:sync
npm run typecheck
npm run test:ocr

# Optional institution service checks.
.\.venv\Scripts\python.exe -m pytest -q .\apps\institution-server\tests
npm run typecheck:institution

# After explicit institution enrollment, claim one signed outbound worker job.
Set-Location .\apps\ocr-studio
..\..\.venv\Scripts\python.exe run.py --mode institution-worker --once
```

See [the OCR studio guide](apps/ocr-studio/README.md) for application-specific commands.
See [the institution deployment guide](apps/institution-server/README.md) for
OIDC, PostgreSQL, Redis, object storage, Docker Compose, and Helm configuration.

## Release identity

The signed desktop and maintenance channels use
`md-ishtiak-ahmed-sajib/PredixaLearn`. Rename the GitHub repository to that
identity before publishing the first PredixaLearn release; changing the local
Git remote is intentionally left to the repository owner.

## Installed Windows desktop mode

The signed Windows installer runs PredixaLearn as a local web application in the
user's existing browser. After the administrator prompt, it automatically
creates a per-install local certificate, trusts it in the Windows Root store,
and adds only PredixaLearn's marked hosts entry. No manual hosts-file editing,
certificate setup, public DNS, Caddy, or browser configuration is required.

The installed shortcut opens:

```text
https://app.predixalearn.com/
```

The hostname resolves to `127.0.0.1` on that computer and the service binds
only to loopback port 443. If port 443 is occupied or local certificate trust
cannot be verified, setup stops with an actionable repair message; it never
falls back to insecure HTTP or an alternate port. The tray menu and the
browser's **Quit PredixaLearn** action use the same graceful shutdown guard.

Development and automated tests continue to use
`http://127.0.0.1:8000/`. Uninstall removes only PredixaLearn binaries,
shortcuts, its marked hosts entry, and its generated certificate. User data in
`%LOCALAPPDATA%\PredixaLearn` is preserved.
