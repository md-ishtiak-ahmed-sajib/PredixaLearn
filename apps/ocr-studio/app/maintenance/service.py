"""Conservative, signed maintenance channel for local runtime components.

The browser never supplies package URLs, commands, or filesystem locations.
Only a signed GitHub Release manifest can describe an update and each allowed
component kind is mapped to a small, hard-coded staging handler below.
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import os
import platform
import re
import shutil
import subprocess
import sys
import threading
import uuid
import zipfile
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as package_version
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import Request, urlopen

from Crypto.PublicKey import ECC
from Crypto.Signature import eddsa

from app import __version__
from app.core.config import Settings, get_settings, tools_root
from app.core.queue import JobStatus, get_queue

logger = logging.getLogger("predixalearn.maintenance")

_MANIFEST_SCHEMA_VERSION = 1
_MAX_MANIFEST_BYTES = 1_000_000
_MAX_SIGNATURE_BYTES = 8_192
_MAX_ARTIFACT_BYTES = 10 * 1024 * 1024 * 1024
_MAX_COMPONENTS = 32
_ID_PATTERN = re.compile(r"^[a-z][a-z0-9_-]{1,63}$")
_SHA256_PATTERN = re.compile(r"^[a-f0-9]{64}$")
_RELEASE_PATH_PREFIX = "/md-ishtiak-ahmed-sajib/PredixaLearn/releases/"
_ARTIFACT_KINDS = {
    "python_wheelhouse",
    "python_runtime_bundle",
    "libreoffice_msi",
    "libreoffice_bundle",
    "model_bundle",
}
_MANAGED_PACKAGES = (
    ("paddlepaddle-gpu", "PaddlePaddle GPU"),
    ("paddleocr", "PaddleOCR"),
    ("paddlex", "PaddleX"),
    ("pypandoc-binary", "Pandoc runtime"),
    ("python-docx", "python-docx"),
)


class MaintenanceError(RuntimeError):
    """A safe maintenance error which never exposes runtime paths or document data."""


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _iso(value: datetime) -> str:
    return value.isoformat().replace("+00:00", "Z")


def _parse_time(value: object, name: str) -> datetime:
    if not isinstance(value, str):
        raise MaintenanceError(f"The signed manifest has no valid {name}")
    try:
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise MaintenanceError(f"The signed manifest has no valid {name}") from exc
    if result.tzinfo is None:
        raise MaintenanceError(f"The signed manifest has no valid {name}")
    return result.astimezone(UTC)


def _installed_version(name: str) -> str | None:
    try:
        return package_version(name)
    except PackageNotFoundError:
        return None


def _version_at_least(installed: str, minimum: str) -> bool:
    """Compare numeric release parts without accepting arbitrary version syntax."""
    def parts(value: str) -> tuple[int, ...] | None:
        match = re.fullmatch(r"v?(\d+(?:\.\d+)*)", value.strip())
        return tuple(int(item) for item in match.group(1).split(".")) if match else None

    actual = parts(installed)
    required = parts(minimum)
    if actual is None or required is None:
        return False
    size = max(len(actual), len(required))
    return actual + (0,) * (size - len(actual)) >= required + (0,) * (size - len(required))


def _active_ocr_jobs() -> bool:
    active = {JobStatus.PENDING, JobStatus.PROCESSING}
    return any(item.status in active for item in get_queue().list_jobs().values())


def _trusted_release_url(value: object) -> str:
    if not isinstance(value, str):
        raise MaintenanceError("A signed update artifact has no URL")
    parsed = urlsplit(value)
    if (
        parsed.scheme != "https"
        or parsed.hostname != "github.com"
        or parsed.username
        or parsed.password
        or not parsed.path.startswith(_RELEASE_PATH_PREFIX)
    ):
        raise MaintenanceError("A signed update artifact is not hosted on the PredixaLearn Release channel")
    return value


def _final_download_url_is_trusted(value: str) -> bool:
    """Permit only GitHub's normal release-asset redirect destinations."""
    parsed = urlsplit(value)
    return parsed.scheme == "https" and parsed.hostname in {
        "github.com",
        "objects.githubusercontent.com",
        "release-assets.githubusercontent.com",
    }


def _download_bytes(url: str, *, maximum: int) -> bytes:
    request = Request(  # noqa: S310 - caller validates the curated HTTPS Release URL.
        url, headers={"User-Agent": "PredixaLearn-maintenance/1"}
    )
    try:
        with urlopen(request, timeout=20) as response:  # noqa: S310 - URL is allow-listed above.
            if not _final_download_url_is_trusted(response.geturl()):
                raise MaintenanceError("The update download redirected to an untrusted location")
            advertised = response.headers.get("Content-Length")
            if advertised and int(advertised) > maximum:
                raise MaintenanceError("The update download exceeds its approved size")
            chunks: list[bytes] = []
            size = 0
            while True:
                chunk = response.read(min(1024 * 1024, maximum - size + 1))
                if not chunk:
                    break
                size += len(chunk)
                if size > maximum:
                    raise MaintenanceError("The update download exceeds its approved size")
                chunks.append(chunk)
            return b"".join(chunks)
    except (HTTPError, URLError, OSError, ValueError) as exc:
        raise MaintenanceError("The online maintenance channel is unavailable") from exc


def _decode_signature(value: bytes) -> bytes:
    raw = value.strip()
    if len(raw) == 64:
        return raw
    try:
        decoded = base64.b64decode(raw, validate=True)
    except ValueError as exc:
        raise MaintenanceError("The update manifest signature is malformed") from exc
    if len(decoded) != 64:
        raise MaintenanceError("The update manifest signature is malformed")
    return decoded


def _safe_extract_zip(archive: Path, destination: Path) -> None:
    """Extract a vendor bundle without traversal paths, links, or zip bombs."""
    try:
        with zipfile.ZipFile(archive) as bundle:
            entries = bundle.infolist()
            if len(entries) > 25_000:
                raise MaintenanceError("The approved update archive contains too many files")
            total = 0
            for entry in entries:
                target = (destination / entry.filename).resolve()
                try:
                    target.relative_to(destination.resolve())
                except ValueError as exc:
                    raise MaintenanceError("The approved update archive has an unsafe path") from exc
                is_link = (entry.external_attr >> 16) & 0o170000 == 0o120000
                if is_link:
                    raise MaintenanceError("The approved update archive contains links")
                total += entry.file_size
                if total > _MAX_ARTIFACT_BYTES or entry.file_size > _MAX_ARTIFACT_BYTES:
                    raise MaintenanceError("The approved update archive is too large")
            bundle.extractall(destination)
    except zipfile.BadZipFile as exc:
        raise MaintenanceError("The approved update archive is invalid") from exc


@dataclass(slots=True)
class MaintenanceJob:
    job_id: str
    manifest_version: str
    status: str = "queued"
    stage: str = "queued"
    message: str = "Maintenance job is waiting to start"
    progress: int = 0
    cancellable: bool = True
    cancel_requested: bool = False
    created_at: datetime = field(default_factory=_utcnow)
    updated_at: datetime = field(default_factory=_utcnow)
    error: str | None = None
    artifacts: list[dict[str, str]] = field(default_factory=list)

    def snapshot(self) -> dict[str, Any]:
        data = asdict(self)
        data["created_at"] = _iso(self.created_at)
        data["updated_at"] = _iso(self.updated_at)
        return data


class MaintenanceService:
    """In-process coordinator; all permanent data is under LocalAppData."""

    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()
        self._lock = threading.RLock()
        self._plans: dict[str, dict[str, Any]] = {}
        self._jobs: dict[str, MaintenanceJob] = {}

    def _persist_job(self, job: MaintenanceJob) -> None:
        jobs_dir = self.settings.maintenance_dir / "jobs"
        jobs_dir.mkdir(parents=True, exist_ok=True)
        pending = jobs_dir / f".{job.job_id}.tmp"
        target = jobs_dir / f"{job.job_id}.json"
        pending.write_text(json.dumps(job.snapshot(), indent=2), encoding="utf-8")
        os.replace(pending, target)

    def _set_job(self, job: MaintenanceJob, **values: Any) -> None:
        with self._lock:
            for key, value in values.items():
                setattr(job, key, value)
            job.updated_at = _utcnow()
            self._persist_job(job)

    def _inventory(self) -> list[dict[str, str]]:
        libreoffice_health = "ready" if self.settings.libreoffice_path.is_file() else "unavailable"
        inventory = [
            {
                "id": "predixalearn",
                "name": "PredixaLearn application",
                "installed_version": __version__,
                "health": "protected",
                "detail": "Application source and UI are not updated by Maintenance.",
            },
            {
                "id": "python",
                "name": "Python runtime",
                "installed_version": platform.python_version(),
                "health": "ready",
                "detail": "Runtime packages are updated only from an approved bundle.",
            },
            {
                "id": "libreoffice",
                "name": "LibreOffice",
                "installed_version": self.settings.libreoffice_version,
                "health": libreoffice_health,
                "detail": "Local DOCX conversion runtime.",
            },
        ]
        for package, label in _MANAGED_PACKAGES:
            installed = _installed_version(package)
            inventory.append(
                {
                    "id": package,
                    "name": label,
                    "installed_version": installed or "not installed",
                    "health": "ready" if installed else "unavailable",
                    "detail": "Approved OCR runtime dependency.",
                }
            )
        inventory.append(
            {
                "id": "ocr-models",
                "name": "Approved OCR model packages",
                "installed_version": "managed by runtime",
                "health": "ready",
                "detail": "Models are never downloaded until an approved signed release is reviewed.",
            }
        )
        return inventory

    def _verify_manifest(self) -> tuple[dict[str, Any], str]:
        key_path = self.settings.update_public_key_path
        if not key_path.is_file():
            raise MaintenanceError("The signed maintenance channel is not configured yet")
        try:
            public_key = ECC.import_key(key_path.read_bytes())
            if public_key.curve != "Ed25519" or public_key.has_private():
                raise ValueError("not an Ed25519 public key")
        except (OSError, ValueError, IndexError) as exc:
            raise MaintenanceError("The installed maintenance signing key is invalid") from exc

        manifest_url = _trusted_release_url(self.settings.update_manifest_url)
        manifest_bytes = _download_bytes(manifest_url, maximum=_MAX_MANIFEST_BYTES)
        signature = _decode_signature(
            _download_bytes(f"{manifest_url}.sig", maximum=_MAX_SIGNATURE_BYTES)
        )
        try:
            eddsa.new(public_key, mode="rfc8032").verify(manifest_bytes, signature)
        except ValueError as exc:
            raise MaintenanceError("The update manifest signature could not be verified") from exc
        try:
            manifest = json.loads(manifest_bytes)
        except (TypeError, ValueError) as exc:
            raise MaintenanceError("The signed update manifest is not valid JSON") from exc
        if not isinstance(manifest, dict):
            raise MaintenanceError("The signed update manifest must be an object")
        return manifest, hashlib.sha256(manifest_bytes).hexdigest()

    @staticmethod
    def _validate_manifest(manifest: dict[str, Any]) -> list[dict[str, Any]]:
        if manifest.get("schema_version") != _MANIFEST_SCHEMA_VERSION:
            raise MaintenanceError("This maintenance manifest schema is not supported")
        manifest_version = manifest.get("manifest_version")
        if not isinstance(manifest_version, str) or not manifest_version.strip():
            raise MaintenanceError("The signed manifest has no version")
        expires_at = _parse_time(manifest.get("expires_at"), "expiry date")
        issued_at = _parse_time(manifest.get("issued_at"), "issue date")
        now = _utcnow()
        if expires_at <= now:
            raise MaintenanceError("The signed maintenance manifest has expired")
        if issued_at > now.replace(microsecond=0):
            raise MaintenanceError("The signed maintenance manifest is not valid yet")
        components = manifest.get("components")
        if not isinstance(components, list) or len(components) > _MAX_COMPONENTS:
            raise MaintenanceError("The signed manifest has an invalid component list")
        seen: set[str] = set()
        for component in components:
            if not isinstance(component, dict):
                raise MaintenanceError("The signed manifest has an invalid component")
            identifier = component.get("id")
            kind = component.get("kind")
            artifact = component.get("artifact")
            if not isinstance(identifier, str) or not _ID_PATTERN.fullmatch(identifier):
                raise MaintenanceError("The signed manifest has an invalid component identifier")
            if identifier in seen:
                raise MaintenanceError("The signed manifest repeats a component identifier")
            seen.add(identifier)
            if kind not in _ARTIFACT_KINDS:
                # Unknown kinds are represented as blocked candidates, never executed.
                continue
            if not isinstance(artifact, dict):
                raise MaintenanceError("The signed component has no approved artifact")
            _trusted_release_url(artifact.get("url"))
            digest = artifact.get("sha256")
            if not isinstance(digest, str) or not _SHA256_PATTERN.fullmatch(digest):
                raise MaintenanceError("The signed component has no valid SHA-256 hash")
            maximum = artifact.get("maximum_download_bytes", _MAX_ARTIFACT_BYTES)
            if not isinstance(maximum, int) or not 1 <= maximum <= _MAX_ARTIFACT_BYTES:
                raise MaintenanceError("The signed component has an invalid download limit")
        return components

    def _compatibility_reason(self, manifest: dict[str, Any], component: dict[str, Any]) -> str | None:
        if os.name != "nt" or platform.machine().lower() not in {"amd64", "x86_64"}:
            return "Maintenance is available only on supported Windows x64 installations."
        platforms = manifest.get("platforms", ["windows-x86_64"])
        if not isinstance(platforms, list) or "windows-x86_64" not in platforms:
            return "This signed release does not support Windows x64."
        minimum_app = manifest.get("minimum_predixalearn_version", "0")
        if not isinstance(minimum_app, str) or not _version_at_least(__version__, minimum_app):
            return "This runtime release needs a newer PredixaLearn application."
        requirements = component.get("requires", {})
        if not isinstance(requirements, dict):
            return "The signed component has invalid compatibility requirements."
        minimum_python = requirements.get("minimum_python")
        if minimum_python is not None and (
            not isinstance(minimum_python, str)
            or not _version_at_least(platform.python_version(), minimum_python)
        ):
            return "This component needs a newer Python runtime."
        free_space = requirements.get("free_disk_bytes", 0)
        if not isinstance(free_space, int) or free_space < 0:
            return "The signed component has invalid disk-space requirements."
        if shutil.disk_usage(self.settings.maintenance_dir.parent).free < free_space:
            return "There is not enough free disk space to stage this update safely."
        gpu_reason = self._gpu_compatibility_reason(requirements)
        if gpu_reason:
            return gpu_reason
        if _active_ocr_jobs():
            return "An OCR job is active or queued. Finish or cancel it before maintenance."
        if component.get("kind") not in _ARTIFACT_KINDS:
            return "This signed candidate does not have an approved updater handler."
        if component.get("kind") == "python_wheelhouse":
            return (
                "This artifact is approved for validation but is not a promotable local runtime "
                "bundle. Publish the tested bundle form instead."
            )
        return None

    @staticmethod
    def _gpu_compatibility_reason(requirements: dict[str, Any]) -> str | None:
        minimum_cuda = requirements.get("minimum_cuda")
        minimum_cudnn = requirements.get("minimum_cudnn")
        minimum_driver = requirements.get("minimum_gpu_driver")
        if all(value is None for value in (minimum_cuda, minimum_cudnn, minimum_driver)):
            return None
        if not all(value is None or isinstance(value, str) for value in (minimum_cuda, minimum_cudnn, minimum_driver)):
            return "The signed component has invalid GPU compatibility requirements."
        try:
            import paddle

            if not paddle.is_compiled_with_cuda() or paddle.device.cuda.device_count() < 1:
                return "This component requires an available CUDA GPU runtime."
            if minimum_cuda and not _version_at_least(str(paddle.version.cuda()), minimum_cuda):
                return "The installed CUDA runtime is older than this approved update requires."
            compiled_cudnn = str(paddle.version.cudnn())
            runtime_cudnn = paddle.device.get_cudnn_version()
            if minimum_cudnn and (
                not _version_at_least(compiled_cudnn, minimum_cudnn)
                or runtime_cudnn is None
            ):
                return "The installed cuDNN runtime is not compatible with this approved update."
        except Exception:
            return "GPU/CUDA compatibility could not be verified for this approved update."
        if minimum_driver:
            executable = shutil.which("nvidia-smi")
            if executable is None:
                return "The NVIDIA driver version could not be verified for this approved update."
            try:
                completed = subprocess.run(  # noqa: S603 - fixed NVIDIA version query.
                    [
                        str(Path(executable).resolve()),
                        "--query-gpu=driver_version",
                        "--format=csv,noheader",
                    ],
                    check=False,
                    capture_output=True,
                    text=True,
                    timeout=20,
                )
            except (OSError, subprocess.TimeoutExpired):
                return "The NVIDIA driver version could not be verified for this approved update."
            driver = completed.stdout.splitlines()[0].strip() if completed.returncode == 0 and completed.stdout else ""
            if not _version_at_least(driver, minimum_driver):
                return "The NVIDIA driver is older than this approved update requires."
        return None

    def check(self) -> dict[str, Any]:
        inventory = self._inventory()
        try:
            manifest, content_hash = self._verify_manifest()
            components = self._validate_manifest(manifest)
        except MaintenanceError as exc:
            return {
                "channel": {"status": "unavailable", "message": str(exc)},
                "inventory": inventory,
                "ready_to_update": [],
                "blocked": [],
                "can_apply": False,
            }

        ready: list[dict[str, Any]] = []
        blocked: list[dict[str, Any]] = []
        for component in components:
            item = {
                "id": component.get("id", "unknown"),
                "name": component.get("name", component.get("id", "Unknown component")),
                "kind": component.get("kind", "unknown"),
                "installed_version": component.get("installed_version", "managed runtime"),
                "target_version": component.get("target_version", "unspecified"),
            }
            reason = self._compatibility_reason(manifest, component)
            if reason:
                blocked.append({**item, "reason": reason})
            else:
                ready.append(item)
        manifest_version = str(manifest["manifest_version"])
        review_token = hashlib.sha256(
            f"{manifest_version}:{content_hash}".encode("utf-8")
        ).hexdigest()
        with self._lock:
            self._plans[manifest_version] = {
                "manifest": manifest,
                "components": components,
                "review_token": review_token,
                "checked_at": _iso(_utcnow()),
            }
        return {
            "channel": {
                "status": "available",
                "manifest_version": manifest_version,
                "expires_at": manifest["expires_at"],
                "message": "Signed maintenance release verified.",
            },
            "inventory": inventory,
            "ready_to_update": ready,
            "blocked": blocked,
            "review_token": review_token,
            "can_apply": bool(ready),
        }

    def apply(self, manifest_version: str, review_token: str) -> dict[str, Any]:
        with self._lock:
            plan = self._plans.get(manifest_version)
        if plan is None or plan["review_token"] != review_token:
            raise MaintenanceError("Review the current signed release before applying maintenance")
        if _active_ocr_jobs():
            raise MaintenanceError("Finish or cancel active OCR work before maintenance")
        components = [
            component
            for component in plan["components"]
            if self._compatibility_reason(plan["manifest"], component) is None
        ]
        if not components:
            raise MaintenanceError("There are no compatible approved updates to apply")
        with self._lock:
            if any(job.status not in {"completed", "failed", "cancelled"} for job in self._jobs.values()):
                raise MaintenanceError("Another maintenance job is already running")
            job = MaintenanceJob(job_id=uuid.uuid4().hex, manifest_version=manifest_version)
            self._jobs[job.job_id] = job
            self._persist_job(job)
        threading.Thread(
            target=self._run_job,
            args=(job, components),
            name=f"predixalearn-maintenance-{job.job_id[:8]}",
            daemon=True,
        ).start()
        return job.snapshot()

    def _cancelled(self, job: MaintenanceJob) -> bool:
        with self._lock:
            return job.cancel_requested

    def _run_job(self, job: MaintenanceJob, components: list[dict[str, Any]]) -> None:
        stage_root = self.settings.maintenance_dir / "staging" / job.job_id
        try:
            self._set_job(job, status="downloading", stage="download", message="Downloading approved files")
            stage_root.mkdir(parents=True, exist_ok=False)
            total = len(components)
            for position, component in enumerate(components, start=1):
                if self._cancelled(job):
                    self._set_job(
                        job,
                        status="cancelled",
                        stage="cancelled",
                        message="Maintenance was cancelled before installation.",
                        progress=0,
                        cancellable=False,
                    )
                    shutil.rmtree(stage_root, ignore_errors=True)
                    return
                artifact = component["artifact"]
                filename = f"{position:02d}-{component['id']}-{Path(artifact['url']).name}"
                destination = stage_root / filename
                payload = _download_bytes(artifact["url"], maximum=artifact["maximum_download_bytes"])
                if hashlib.sha256(payload).hexdigest() != artifact["sha256"]:
                    raise MaintenanceError("An approved update file did not match its SHA-256 hash")
                destination.write_bytes(payload)
                job.artifacts.append({"component": component["id"], "filename": filename})
                self._set_job(
                    job,
                    progress=round(position / total * 55),
                    message=f"Downloaded approved {component['name']}",
                )

            self._set_job(job, status="validating", stage="validation", message="Validating staged runtime")
            for position, component in enumerate(components, start=1):
                if self._cancelled(job):
                    self._set_job(
                        job,
                        status="cancelled",
                        stage="cancelled",
                        message="Maintenance was cancelled before atomic installation.",
                        cancellable=False,
                    )
                    shutil.rmtree(stage_root, ignore_errors=True)
                    return
                artifact_path = stage_root / job.artifacts[position - 1]["filename"]
                kind = component["kind"]
                if kind.endswith("_bundle") or kind == "python_wheelhouse":
                    extract_root = stage_root / f"validated-{component['id']}"
                    extract_root.mkdir()
                    _safe_extract_zip(artifact_path, extract_root)
                    self._validate_bundle(kind, extract_root)
                elif kind == "libreoffice_msi":
                    self._validate_msi(artifact_path, component)
                    extract_root = stage_root / f"validated-{component['id']}"
                    extract_root.mkdir()
                    self._stage_libreoffice_msi(artifact_path, extract_root)
                    self._validate_bundle("libreoffice_bundle", extract_root)
                else:  # Defensive: validation should have blocked unknown kinds.
                    raise MaintenanceError("No updater handler exists for a signed component")
                self._set_job(job, progress=55 + round(position / total * 35))

            self._write_pending_plan(job, stage_root, components)
            self._set_job(
                job,
                status="restart_required",
                stage="restart",
                message=(
                    "Approved files passed staging validation. Close and reopen PredixaLearn "
                    "to let the local maintenance bootstrapper perform the atomic swap and rollback check."
                ),
                progress=90,
                cancellable=False,
            )
            self._write_audit(job, "validated")
        except MaintenanceError as exc:
            logger.warning("Maintenance job %s failed safely: %s", job.job_id, exc)
            self._set_job(
                job,
                status="failed",
                stage="failed",
                message=str(exc),
                error=str(exc),
                cancellable=False,
            )
            self._write_audit(job, "failed")
        except Exception:
            logger.exception("Maintenance job %s failed unexpectedly", job.job_id)
            self._set_job(
                job,
                status="failed",
                stage="failed",
                message="Maintenance validation failed without changing the installed runtime.",
                error="internal validation failure",
                cancellable=False,
            )
            self._write_audit(job, "failed")

    @staticmethod
    def _validate_msi(artifact: Path, component: dict[str, Any]) -> None:
        """Check the MSI's Windows publisher signature with a fixed local command."""
        publisher = component.get("publisher")
        if not isinstance(publisher, str) or not publisher.strip():
            raise MaintenanceError("The LibreOffice update has no approved publisher expectation")
        if os.name != "nt":
            raise MaintenanceError("LibreOffice maintenance is available only on Windows")
        command = (
            "$signature = Get-AuthenticodeSignature -LiteralPath $args[0]; "
            "\"$($signature.Status)|$($signature.SignerCertificate.Subject)\""
        )
        powershell = (
            Path(os.environ.get("SystemRoot", r"C:\\Windows"))
            / "System32"
            / "WindowsPowerShell"
            / "v1.0"
            / "powershell.exe"
        )
        try:
            completed = subprocess.run(  # noqa: S603 - fixed PowerShell validation only.
                [str(powershell), "-NoProfile", "-NonInteractive", "-Command", command, str(artifact)],
                check=False,
                capture_output=True,
                text=True,
                timeout=30,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise MaintenanceError("LibreOffice publisher validation could not run") from exc
        output = completed.stdout.strip()
        status_value, _, subject = output.partition("|")
        if (
            completed.returncode != 0
            or status_value != "Valid"
            or publisher.casefold() not in subject.casefold()
        ):
            raise MaintenanceError("LibreOffice publisher validation failed")

    @staticmethod
    def _stage_libreoffice_msi(artifact: Path, root: Path) -> None:
        """Use Windows Installer's fixed administrative extraction mode in staging."""
        msiexec = Path(os.environ.get("SystemRoot", r"C:\\Windows")) / "System32" / "msiexec.exe"
        administrative = root / "administrative"
        try:
            completed = subprocess.run(  # noqa: S603 - fixed Windows Installer invocation.
                [str(msiexec), "/a", str(artifact), "/qn", f"TARGETDIR={administrative}"],
                check=False,
                capture_output=True,
                text=True,
                timeout=900,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise MaintenanceError("LibreOffice could not be staged for validation") from exc
        if completed.returncode != 0:
            raise MaintenanceError("LibreOffice could not be staged for validation")
        executable = next(administrative.rglob("soffice.com"), None)
        if executable is None or executable.parent.name.casefold() != "program":
            raise MaintenanceError("The staged LibreOffice MSI has no program/soffice.com")
        source = executable.parent.parent
        destination = root / "libreoffice"
        shutil.move(str(source), str(destination))

    @staticmethod
    def _validate_bundle(kind: str, root: Path) -> None:
        """Validate only the documented fixed bundle layouts before promotion."""
        if kind == "python_runtime_bundle":
            python = root / "runtime" / "Scripts" / "python.exe"
            if not python.is_file():
                raise MaintenanceError("The approved Python runtime bundle has no runtime/Scripts/python.exe")
            checks = (
                [str(python), "-m", "pip", "check"],
                [str(python), "-c", "import fastapi, paddleocr, paddlex"],
            )
            for command in checks:
                try:
                    completed = subprocess.run(  # noqa: S603 - fixed candidate validation.
                        command,
                        check=False,
                        capture_output=True,
                        text=True,
                        timeout=180,
                    )
                except (OSError, subprocess.TimeoutExpired) as exc:
                    raise MaintenanceError("The staged Python runtime could not be validated") from exc
                if completed.returncode != 0:
                    raise MaintenanceError("The staged Python runtime failed dependency validation")
        elif kind == "libreoffice_bundle":
            executable = root / "libreoffice" / "program" / "soffice.com"
            if not executable.is_file():
                raise MaintenanceError("The approved LibreOffice bundle has no program/soffice.com")
            try:
                completed = subprocess.run(  # noqa: S603 - fixed candidate validation.
                    [str(executable), "--version"],
                    check=False,
                    capture_output=True,
                    text=True,
                    timeout=30,
                )
            except (OSError, subprocess.TimeoutExpired) as exc:
                raise MaintenanceError("The staged LibreOffice bundle could not be validated") from exc
            if completed.returncode != 0 or "LibreOffice" not in completed.stdout:
                raise MaintenanceError("The staged LibreOffice bundle failed version validation")
            MaintenanceService._validate_libreoffice_docx_conversion(executable, root)
        elif kind == "model_bundle" and not (root / "models" / "manifest.json").is_file():
            raise MaintenanceError("The approved model bundle has no models/manifest.json")

    @staticmethod
    def _validate_libreoffice_docx_conversion(executable: Path, root: Path) -> None:
        """Exercise DOCX-to-PDF conversion without reading or writing user documents."""
        smoke_root = root / "libreoffice-smoke"
        smoke_root.mkdir(exist_ok=True)
        document = smoke_root / "maintenance-smoke.docx"
        with zipfile.ZipFile(document, "w", compression=zipfile.ZIP_DEFLATED) as bundle:
            bundle.writestr(
                "[Content_Types].xml",
                "<?xml version='1.0'?><Types xmlns='http://schemas.openxmlformats.org/package/2006/content-types'><Default Extension='rels' ContentType='application/vnd.openxmlformats-package.relationships+xml'/><Default Extension='xml' ContentType='application/xml'/><Override PartName='/word/document.xml' ContentType='application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml'/></Types>",
            )
            bundle.writestr(
                "_rels/.rels",
                "<?xml version='1.0'?><Relationships xmlns='http://schemas.openxmlformats.org/package/2006/relationships'><Relationship Id='rId1' Type='http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument' Target='word/document.xml'/></Relationships>",
            )
            bundle.writestr(
                "word/document.xml",
                "<?xml version='1.0'?><w:document xmlns:w='http://schemas.openxmlformats.org/wordprocessingml/2006/main'><w:body><w:p><w:r><w:t>PredixaLearn maintenance smoke test</w:t></w:r></w:p><w:sectPr/></w:body></w:document>",
            )
        profile = (smoke_root / "profile").resolve().as_uri()
        try:
            completed = subprocess.run(  # noqa: S603 - fixed local candidate conversion.
                [
                    str(executable),
                    "--headless",
                    f"-env:UserInstallation={profile}",
                    "--convert-to",
                    "pdf",
                    "--outdir",
                    str(smoke_root),
                    str(document),
                ],
                check=False,
                capture_output=True,
                text=True,
                timeout=120,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise MaintenanceError("The staged LibreOffice bundle failed DOCX conversion") from exc
        if completed.returncode != 0 or not (smoke_root / "maintenance-smoke.pdf").is_file():
            raise MaintenanceError("The staged LibreOffice bundle failed DOCX conversion")

    def _write_pending_plan(
        self,
        job: MaintenanceJob,
        stage_root: Path,
        components: list[dict[str, Any]],
    ) -> None:
        """Write a fixed promotion plan consumed only by the local PowerShell bootstrapper."""
        promotion: list[dict[str, str]] = []
        for component in components:
            validated = stage_root / f"validated-{component['id']}"
            kind = component["kind"]
            if kind == "python_runtime_bundle":
                promotion.append({"source": str(validated / "runtime"), "target": sys.prefix, "kind": kind})
            elif kind in {"libreoffice_bundle", "libreoffice_msi"}:
                promotion.append(
                    {
                        "source": str(validated / "libreoffice"),
                        "target": str(tools_root() / "libreoffice" / "current"),
                        "kind": kind,
                    }
                )
            elif kind == "model_bundle":
                promotion.append(
                    {
                        "source": str(validated / "models"),
                        "target": str(tools_root() / "models" / "current"),
                        "kind": kind,
                    }
                )
        if not promotion:
            raise MaintenanceError("The signed release has no promotable runtime bundle")
        plan = {
            "schema_version": 1,
            "job_id": job.job_id,
            "manifest_version": job.manifest_version,
            "promotion": promotion,
            "app_root": str(self.settings.project_root),
            "tools_root": str(tools_root()),
            "port": self.settings.port,
        }
        self.settings.maintenance_dir.mkdir(parents=True, exist_ok=True)
        pending = self.settings.maintenance_dir / ".pending-update.tmp"
        target = self.settings.maintenance_dir / "pending-update.json"
        pending.write_text(json.dumps(plan, indent=2), encoding="utf-8")
        os.replace(pending, target)

    def _write_audit(self, job: MaintenanceJob, outcome: str) -> None:
        self.settings.maintenance_dir.mkdir(parents=True, exist_ok=True)
        entry = {
            "time": _iso(_utcnow()),
            "job_id": job.job_id,
            "manifest_version": job.manifest_version,
            "outcome": outcome,
            "components": [item["component"] for item in job.artifacts],
        }
        with (self.settings.maintenance_dir / "audit.jsonl").open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(entry, separators=(",", ":")) + "\n")

    def get_job(self, job_id: str) -> dict[str, Any] | None:
        with self._lock:
            job = self._jobs.get(job_id)
            if job:
                return job.snapshot()
        if not re.fullmatch(r"[a-f0-9]{32}", job_id):
            return None
        record = self.settings.maintenance_dir / "jobs" / f"{job_id}.json"
        try:
            value = json.loads(record.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        return value if isinstance(value, dict) else None

    def cancel(self, job_id: str) -> dict[str, Any] | None:
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None:
                return None
            if not job.cancellable:
                return job.snapshot()
            job.cancel_requested = True
            job.updated_at = _utcnow()
            self._persist_job(job)
            return job.snapshot()


_service: MaintenanceService | None = None
_service_lock = threading.Lock()


def get_maintenance_service() -> MaintenanceService:
    global _service
    if _service is None:
        with _service_lock:
            if _service is None:
                _service = MaintenanceService()
    return _service


def reset_maintenance_service() -> None:
    global _service
    with _service_lock:
        _service = None
