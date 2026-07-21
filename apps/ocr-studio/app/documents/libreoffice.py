"""Project-local LibreOffice discovery and bounded DOCX render validation."""

from __future__ import annotations

import hashlib
import logging
import os
import re
import shutil
import stat
import subprocess
import threading
import time
import uuid
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from functools import lru_cache
from pathlib import Path
from typing import Any

import numpy as np

from app.core.config import Settings, get_settings, tools_root
from app.core.warmup import get_warmup_status

logger = logging.getLogger("predixalearn.libreoffice")
_VERSION = re.compile(r"\bLibreOffice\s+([0-9]+(?:\.[0-9]+){2,3})\b")
_MINIMUM_INK_RATIO = 0.00005
_PDF_OBSERVABILITY_SECONDS = 5.0
_WARMUP_POLL_SECONDS = 0.25
_PROBE_LOCK = threading.Lock()
_PROBE_STATE: dict[str, Any] = {
    "status": "not_started",
    "ready": False,
    "last_check_time": None,
    "bootstrap_hash": None,
    "profile_mode": None,
    "conversion_probe": None,
    "issue_code": None,
    "issue": None,
}
_PROBE_THREAD: threading.Thread | None = None


class LibreOfficeValidationError(RuntimeError):
    """Internal bounded document-rendering failure with a safe public message."""


@dataclass(frozen=True, slots=True)
class DocumentValidation:
    status: str
    renderer: str
    version: str | None
    rendered_pages: int | None
    duration_seconds: float
    warning: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def resolve_libreoffice_executable(settings: Settings | None = None) -> Path:
    cfg = settings or get_settings()
    project_root = cfg.project_root.resolve()
    executable = cfg.libreoffice_path.resolve()
    allowed_roots = (project_root, tools_root())
    for allowed_root in allowed_roots:
        try:
            executable.relative_to(allowed_root)
            break
        except ValueError:
            continue
    else:
        raise LibreOfficeValidationError("Project-local LibreOffice configuration is invalid")
    if executable.name.casefold() != "soffice.com" or not executable.is_file():
        raise LibreOfficeValidationError("Project-local LibreOffice is unavailable")
    return executable


def _terminate_process_tree(process: subprocess.Popen[str]) -> None:
    if process.poll() is not None:
        return
    if os.name == "nt":
        try:
            taskkill = (
                Path(os.environ.get("SystemRoot", r"C:\Windows"))
                / "System32"
                / "taskkill.exe"
            )
            subprocess.run(  # noqa: S603
                [str(taskkill), "/PID", str(process.pid), "/T", "/F"],
                check=False,
                capture_output=True,
                text=True,
                timeout=10,
            )
            return
        except (OSError, subprocess.TimeoutExpired):
            logger.debug("LibreOffice process-tree cleanup failed", exc_info=True)
    process.kill()


def _run_process(
    command: list[str],
    *,
    timeout_seconds: int,
    cwd: Path,
    environment: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    creation_flags = 0
    if os.name == "nt":
        creation_flags = subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW
    process = subprocess.Popen(  # noqa: S603
        command,
        cwd=cwd,
        env=environment,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        shell=False,
        creationflags=creation_flags,
    )
    try:
        stdout, stderr = process.communicate(timeout=timeout_seconds)
    except subprocess.TimeoutExpired as exc:
        _terminate_process_tree(process)
        process.communicate()
        raise LibreOfficeValidationError("LibreOffice document validation timed out") from exc
    if process.returncode != 0:
        logger.warning(
            "LibreOffice returned %d; stdout=%r stderr=%r",
            process.returncode,
            stdout[-500:],
            stderr[-500:],
        )
        raise LibreOfficeValidationError("LibreOffice could not render the generated DOCX")
    return subprocess.CompletedProcess(command, process.returncode, stdout, stderr)


@lru_cache(maxsize=8)
def _cached_version(
    executable_text: str,
    expected_version: str,
    timeout_seconds: int,
) -> str:
    executable = Path(executable_text)
    completed = _run_process(
        [str(executable), "--headless", "--version"],
        timeout_seconds=timeout_seconds,
        cwd=executable.parent,
    )
    match = _VERSION.search(f"{completed.stdout}\n{completed.stderr}")
    if not match:
        raise LibreOfficeValidationError("LibreOffice version could not be verified")
    version = match.group(1)
    if not (version == expected_version or version.startswith(f"{expected_version}.")):
        raise LibreOfficeValidationError("LibreOffice version does not match the project lock")
    return version


def libreoffice_version(settings: Settings | None = None) -> str:
    cfg = settings or get_settings()
    executable = resolve_libreoffice_executable(cfg)
    version_timeout = min(cfg.libreoffice_timeout_seconds, 60)
    return _cached_version(
        str(executable),
        cfg.libreoffice_version,
        version_timeout,
    )


def _bootstrap_metadata(executable: Path) -> tuple[str, str]:
    bootstrap = executable.parent / "bootstrap.ini"
    try:
        payload = bootstrap.read_bytes()
        text = payload.decode("utf-8-sig")
    except (OSError, UnicodeDecodeError) as exc:
        raise LibreOfficeValidationError(
            "Project-local LibreOffice bootstrap configuration is unreadable"
        ) from exc
    if "[Bootstrap]" not in text or "ProductKey=" not in text or "InstallMode=" not in text:
        raise LibreOfficeValidationError(
            "Project-local LibreOffice bootstrap configuration is incomplete"
        )
    profile_mode = (
        "project-local-persistent"
        if re.search(
            r"(?im)^UserInstallation=\$ORIGIN/\.\./Data/settings\s*$",
            text,
        )
        else "isolated-job-only"
    )
    return hashlib.sha256(payload).hexdigest(), profile_mode


def _set_probe_state(**values: Any) -> None:
    with _PROBE_LOCK:
        _PROBE_STATE.update(values)


def _remove_private_tree(path: Path) -> None:
    """Remove QA trees even when LibreOffice leaves read-only profile files."""
    def onerror(function: Any, filename: str, _exc_info: Any) -> None:
        try:
            os.chmod(filename, stat.S_IWRITE)
            function(filename)
        except OSError:
            logger.debug("Could not remove temporary LibreOffice path %s", filename)

    shutil.rmtree(path, onerror=onerror, ignore_errors=True)


def _wait_for_engine_warmup() -> None:
    """Avoid competing with Paddle/CUDA while LibreOffice cold-starts."""
    while get_warmup_status().get("status") not in {"ready", "failed"}:
        time.sleep(_WARMUP_POLL_SECONDS)


def _run_conversion_probe(cfg: Settings) -> None:
    runtime_work_dir = cfg.upload_dir / "libreoffice-health"
    probe_root = runtime_work_dir / f"libreoffice-health-{uuid.uuid4().hex}"
    try:
        _wait_for_engine_warmup()
        executable = resolve_libreoffice_executable(cfg)
        bootstrap_hash, profile_mode = _bootstrap_metadata(executable)
        if profile_mode != "project-local-persistent":
            raise LibreOfficeValidationError(
                "Project-local LibreOffice Writer profile is not configured"
            )
        probe_root.mkdir(parents=True, exist_ok=True)
        from docx import Document

        source = probe_root / "writer-probe.docx"
        document = Document()
        document.add_heading("PredixaLearn document probe", level=1)
        document.add_paragraph("Project-local LibreOffice Writer conversion is operational.")
        document.save(source)
        validation = validate_docx_with_libreoffice(
            source,
            # Keep the QA profile sibling of the configured runtime directory,
            # rather than nesting it below the probe input directory. Some
            # LibreOffice Windows builds silently skip conversion when the
            # input and per-job profile share a freshly-created hidden parent.
            work_dir=runtime_work_dir,
            expected_source_pages=1,
            settings=cfg,
        )
        if validation.status != "passed":
            raise LibreOfficeValidationError(
                validation.warning or "LibreOffice Writer conversion probe failed"
            )
        _set_probe_state(
            status="ready",
            ready=True,
            version=validation.version,
            last_check_time=datetime.now(UTC).isoformat(),
            bootstrap_hash=bootstrap_hash,
            profile_mode=profile_mode,
            conversion_probe={
                "status": "passed",
                "rendered_pages": validation.rendered_pages,
                "duration_seconds": validation.duration_seconds,
            },
            issue_code=None,
            issue=None,
        )
    except LibreOfficeValidationError as exc:
        logger.warning("LibreOffice startup conversion probe failed: %s", exc)
        executable = cfg.libreoffice_path
        bootstrap_hash = None
        profile_mode = None
        try:
            bootstrap_hash, profile_mode = _bootstrap_metadata(executable)
        except LibreOfficeValidationError:
            pass
        _set_probe_state(
            status="degraded",
            ready=False,
            version=None,
            last_check_time=datetime.now(UTC).isoformat(),
            bootstrap_hash=bootstrap_hash,
            profile_mode=profile_mode,
            conversion_probe={"status": "failed"},
            issue_code="libreoffice_conversion_probe_failed",
            issue=str(exc),
        )
    except Exception:
        logger.exception("Unexpected LibreOffice startup conversion probe failure")
        _set_probe_state(
            status="degraded",
            ready=False,
            version=None,
            last_check_time=datetime.now(UTC).isoformat(),
            conversion_probe={"status": "failed"},
            issue_code="libreoffice_probe_unexpected_failure",
            issue="LibreOffice Writer conversion probe failed unexpectedly",
        )
    finally:
        _remove_private_tree(probe_root)


def start_libreoffice_probe(settings: Settings | None = None) -> None:
    """Start one bounded full Writer conversion probe for the current server process."""
    global _PROBE_THREAD
    cfg = settings or get_settings()
    with _PROBE_LOCK:
        if _PROBE_STATE["status"] != "not_started":
            return
        _PROBE_STATE.update(
            {
                "status": "checking",
                "ready": False,
                "issue_code": None,
                "issue": None,
                "conversion_probe": {"status": "checking"},
            }
        )
        _PROBE_THREAD = threading.Thread(
            target=_run_conversion_probe,
            args=(cfg,),
            name="libreoffice-health-probe",
            daemon=True,
        )
        _PROBE_THREAD.start()


def libreoffice_health(settings: Settings | None = None) -> dict[str, Any]:
    cfg = settings or get_settings()
    start_libreoffice_probe(cfg)
    with _PROBE_LOCK:
        state = dict(_PROBE_STATE)
        if isinstance(state.get("conversion_probe"), dict):
            state["conversion_probe"] = dict(state["conversion_probe"])
    return {
        **state,
        "renderer": "libreoffice",
        "architecture": "x86_64",
        "mode": "project-local-headless",
        "validation_required": True,
    }


def _validation_environment(
    profile: Path,
    temporary: Path,
    executable: Path,
) -> dict[str, str]:
    app_data = profile / "appdata"
    local_app_data = profile / "local-appdata"
    for directory in (profile, temporary, app_data, local_app_data):
        directory.mkdir(parents=True, exist_ok=True)
    if os.name == "nt":
        allowed_keys = (
            "ALLUSERSPROFILE",
            "CommonProgramFiles",
            "CommonProgramFiles(x86)",
            "COMSPEC",
            "LANG",
            "LC_ALL",
            "NUMBER_OF_PROCESSORS",
            "OS",
            "PATHEXT",
            "PROCESSOR_ARCHITECTURE",
            "PROCESSOR_IDENTIFIER",
            "ProgramData",
            "ProgramFiles",
            "ProgramFiles(x86)",
            "PUBLIC",
            "SystemDrive",
            "SystemRoot",
            "USERDOMAIN",
            "USERNAME",
            "WINDIR",
        )
        environment = {key: os.environ[key] for key in allowed_keys if key in os.environ}
        system_root = Path(os.environ.get("SystemRoot", r"C:\Windows"))
        environment["PATH"] = os.pathsep.join(
            (str(executable.parent), str(system_root / "System32"), str(system_root))
        )
    else:
        environment = os.environ.copy()
    environment.update(
        {
            "HOME": str(profile),
            "USERPROFILE": str(profile),
            "APPDATA": str(app_data),
            "LOCALAPPDATA": str(local_app_data),
            "TEMP": str(temporary),
            "TMP": str(temporary),
        }
    )
    return environment


def _validate_rendered_pdf(pdf_path: Path, expected_source_pages: int) -> int:
    try:
        import pypdfium2 as pdfium

        document = pdfium.PdfDocument(str(pdf_path))
    except Exception as exc:
        raise LibreOfficeValidationError("LibreOffice produced an unreadable validation PDF") from exc
    try:
        page_count = len(document)
        if page_count < max(1, expected_source_pages):
            raise LibreOfficeValidationError(
                "LibreOffice validation produced fewer pages than the OCR source"
            )
        for index in range(page_count):
            page = None
            bitmap = None
            try:
                page = document[index]
                bitmap = page.render(scale=1.0)
                pixels = np.asarray(bitmap.to_numpy())
                if pixels.ndim == 2:
                    luminance = pixels
                elif pixels.ndim == 3 and pixels.shape[2] >= 3:
                    luminance = pixels[:, :, :3].astype(np.float32).mean(axis=2)
                else:
                    raise LibreOfficeValidationError(
                        "LibreOffice validation produced an invalid page image"
                    )
                ink_ratio = float(np.count_nonzero(luminance < 245)) / float(luminance.size)
                if ink_ratio < _MINIMUM_INK_RATIO:
                    raise LibreOfficeValidationError(
                        f"LibreOffice validation page {index + 1} appears blank"
                    )
            finally:
                if bitmap is not None:
                    bitmap.close()
                if page is not None:
                    page.close()
        return page_count
    finally:
        document.close()


def _wait_for_converted_pdf(output_dir: Path, expected_path: Path) -> Path:
    deadline = time.monotonic() + _PDF_OBSERVABILITY_SECONDS
    while True:
        if expected_path.is_file() and expected_path.stat().st_size >= 1_000:
            return expected_path
        candidates = [
            path
            for path in output_dir.glob("*.pdf")
            if path.is_file() and path.stat().st_size >= 1_000
        ]
        if len(candidates) == 1:
            return candidates[0]
        if time.monotonic() >= deadline:
            raise LibreOfficeValidationError(
                "LibreOffice did not create a usable validation PDF"
            )
        time.sleep(0.1)


def validate_docx_with_libreoffice(
    docx_path: str | Path,
    *,
    work_dir: str | Path,
    expected_source_pages: int,
    settings: Settings | None = None,
) -> DocumentValidation:
    """Render a generated DOCX privately and return safe additive validation metadata."""
    cfg = settings or get_settings()
    started = time.perf_counter()
    version: str | None = None
    qa_root = Path(work_dir).resolve() / f".libreoffice-{uuid.uuid4().hex}"
    try:
        executable = resolve_libreoffice_executable(cfg)
        version = libreoffice_version(cfg)
        source_docx = Path(docx_path).resolve()
        pdf_path: Path | None = None
        for attempt in range(2):
            attempt_root = qa_root / f"attempt-{attempt + 1}"
            output_dir = attempt_root / "out"
            profile_dir = attempt_root / "profile"
            temporary_dir = attempt_root / "tmp"
            output_dir.mkdir(parents=True, exist_ok=True)
            validation_docx = attempt_root / source_docx.name
            shutil.copy2(source_docx, validation_docx)
            environment = _validation_environment(profile_dir, temporary_dir, executable)
            command = [
                str(executable),
                "--headless",
                "--nologo",
                "--nodefault",
                "--norestore",
                f"-env:UserInstallation={profile_dir.as_uri()}",
                "--convert-to",
                "pdf:writer_pdf_Export",
                "--outdir",
                str(output_dir),
                str(validation_docx),
            ]
            remaining_seconds = int(
                cfg.libreoffice_timeout_seconds - (time.perf_counter() - started)
            )
            if remaining_seconds < 1:
                raise LibreOfficeValidationError(
                    "LibreOffice document validation timed out"
                )
            completed = _run_process(
                command,
                timeout_seconds=remaining_seconds,
                cwd=output_dir,
                environment=environment,
            )
            expected_pdf = output_dir / f"{validation_docx.stem}.pdf"
            try:
                pdf_path = _wait_for_converted_pdf(output_dir, expected_pdf)
                break
            except LibreOfficeValidationError:
                generated_files = [
                    (path.suffix.casefold(), path.stat().st_size)
                    for path in output_dir.iterdir()
                    if path.is_file()
                ]
                logger.warning(
                    "LibreOffice conversion attempt %d produced no usable PDF; "
                    "stdout=%s stderr=%s files=%r",
                    attempt + 1,
                    bool(completed.stdout.strip()),
                    bool(completed.stderr.strip()),
                    generated_files,
                )
                if attempt == 1:
                    raise
                time.sleep(0.25)
        if pdf_path is None:
            raise LibreOfficeValidationError(
                "LibreOffice did not create a usable validation PDF"
            )
        rendered_pages = _validate_rendered_pdf(pdf_path, expected_source_pages)
        return DocumentValidation(
            status="passed",
            renderer="libreoffice",
            version=version,
            rendered_pages=rendered_pages,
            duration_seconds=round(time.perf_counter() - started, 3),
        )
    except (LibreOfficeValidationError, OSError) as exc:
        logger.warning("Project-local LibreOffice validation failed: %s", exc)
        return DocumentValidation(
            status="failed",
            renderer="libreoffice",
            version=version,
            rendered_pages=None,
            duration_seconds=round(time.perf_counter() - started, 3),
            warning=str(exc),
        )
    except Exception:
        logger.exception("Unexpected project-local LibreOffice validation failure")
        return DocumentValidation(
            status="failed",
            renderer="libreoffice",
            version=version,
            rendered_pages=None,
            duration_seconds=round(time.perf_counter() - started, 3),
            warning="LibreOffice document validation failed unexpectedly",
        )
    finally:
        _remove_private_tree(qa_root)


def clear_libreoffice_cache() -> None:
    global _PROBE_THREAD
    _cached_version.cache_clear()
    with _PROBE_LOCK:
        _PROBE_STATE.clear()
        _PROBE_STATE.update(
            {
                "status": "not_started",
                "ready": False,
                "last_check_time": None,
                "bootstrap_hash": None,
                "profile_mode": None,
                "conversion_probe": None,
                "issue_code": None,
                "issue": None,
            }
        )
        _PROBE_THREAD = None
