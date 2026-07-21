# Safe online runtime maintenance

PredixaLearn's **Maintenance** control updates only tested local runtime components: the approved Python/Paddle/PaddleOCR bundle, approved model bundles, and LibreOffice. It never updates application source, the browser UI, user documents, History, output, uploads, or logs.

The release channel is `md-ishtiak-ahmed-sajib/PredixaLearn`. Rename the GitHub
repository to that identity before publishing the first signed PredixaLearn
runtime release.

The browser must be online to open the control. An update is never required merely because a vendor published a newer version: it appears only after a maintainer has tested it and included it in a signed PredixaLearn Release manifest.

## Maintainer setup (one time)

1. Generate an Ed25519 keypair on an offline maintainer workstation:

   ```powershell
   openssl genpkey -algorithm Ed25519 -out predixalearn-update-private.pem
   openssl pkey -in predixalearn-update-private.pem -pubout -out apps/ocr-studio/app/maintenance/predixalearn-update-public.pem
   ```

2. Commit only `predixalearn-update-public.pem`. Do not commit, copy into a release, or print the private key.
3. Add the complete private PEM as the GitHub Actions repository secret named `PREDIXALEARN_UPDATE_SIGNING_KEY`.
4. Rotate the key by shipping an application release containing the next public key before signing releases with it.

Until that public key is installed and a signed Release exists, the maintenance dialog deliberately reports that the channel is unavailable. This is the expected first-launch behavior.

## Preparing a runtime release

1. Build and validate Windows x64 artifacts in a clean staging environment. A Python runtime artifact is a complete, tested runtime bundle; a LibreOffice artifact is the vendor MSI with a valid Windows publisher signature; model packages are archive bundles. Do not use vendor "latest" URLs.
2. Record each immutable GitHub Release URL, SHA-256, maximum download size, target version, supported CUDA/cuDNN pairing, minimum Python/app version, and disk-space requirements in a reviewed, committed copy named `tooling/release/runtime-update-components.json`, based on [`runtime-update-components.example.json`](../../../tooling/release/runtime-update-components.example.json).
3. Test `pip check`, imports, Paddle/CUDA/cuDNN compatibility, model availability, LibreOffice signature/version, and a DOCX conversion smoke test before release.
4. Dispatch **Publish signed runtime maintenance manifest** with a release tag. The workflow runs the OCR/browser checks and emits `predixalearn-update-manifest.json` plus its detached `.sig` file.

The manifest endpoint is fixed in the app to:

`https://github.com/md-ishtiak-ahmed-sajib/PredixaLearn/releases/latest/download/predixalearn-update-manifest.json`

## Runtime behavior and recovery

- The server verifies the detached Ed25519 signature before parsing the manifest.
- Artifact URLs are restricted to this repository's GitHub Release channel; hashes and download limits are checked before staging.
- Component types are mapped to fixed updater handlers. A manifest cannot provide a shell command, install path, or executable command.
- Before an item is marked ready, the service checks Windows x64 support, app/Python compatibility, disk space, an idle OCR queue, and approved handler availability. Blocked items are shown separately with a reason.
- Downloads are staged under `%LOCALAPPDATA%\PredixaLearn\maintenance`, never in the source tree or user document directories. An interrupted or failed stage leaves the installed runtime untouched.
- The currently running Windows interpreter cannot rename its own runtime safely. A successful validation therefore reaches **restart required**; the native promotion bootstrapper performs the atomic swap only after the process has stopped, retaining the old runtime for rollback.

The maintenance audit records job IDs, manifest versions, component IDs, and outcomes only. It never records document names, contents, output paths, or upload paths.
