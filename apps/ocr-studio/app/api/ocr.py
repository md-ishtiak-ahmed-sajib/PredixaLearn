"""Safe, bounded FastAPI routes for the four OCR workflows."""

from __future__ import annotations

import logging
import os
import uuid
from importlib import import_module
from importlib.metadata import version as package_version
from pathlib import Path
from typing import Annotated, Any, Callable

import aiofiles
from fastapi import APIRouter, File, Form, HTTPException, Query, UploadFile, status
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import JSONResponse
from PIL import Image, UnidentifiedImageError

from app import __version__
from app.core.capabilities import (
    DOCUMENT_PROFILES,
    language_capabilities,
    supported_languages,
    validate_document_profile,
    validate_language,
)
from app.core.config import get_settings
from app.core.engine import get_manager
from app.core.errors import InputLimitError, InvalidInputError
from app.core.queue import JobResult, JobStatus, get_queue
from app.core.resource_profiles import profile_allows_workflow, resolve_profile
from app.core.serialization import json_safe
from app.core.warmup import get_warmup_status
from app.documents.docx import pandoc_executable
from app.documents.libreoffice import libreoffice_health
from app.documents.text_filters import (
    MAX_REMOVE_TEXT_LENGTH,
    normalize_remove_terms,
    normalize_remove_text,
)
from app.teaching.languages import reliability_registry
from app.workflows import (
    run_layout_parsing,
    run_table_extraction,
    run_text_recognition,
    run_vl_processing,
)
from app.workflows.pdf_utils import inspect_pdf

logger = logging.getLogger("predixalearn.api")
router = APIRouter(prefix="/api/v1", tags=["ocr"])

_BYTES_PER_MEBIBYTE = 1024 * 1024
_UPLOAD_SIGNATURE_BYTES = 16
_CUDNN_MAJOR_UNIT = 10_000
_CUDNN_MINOR_UNIT = 100
_IMAGE_FORMATS = {
    "PNG": ".png",
    "JPEG": ".jpg",
    "BMP": ".bmp",
    "TIFF": ".tiff",
    "WEBP": ".webp",
}
_CLIENT_ERROR_CODES = {
    "InvalidInputError": status.HTTP_422_UNPROCESSABLE_CONTENT,
    "InputLimitError": status.HTTP_413_CONTENT_TOO_LARGE,
    "UnsupportedInputError": status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
    "WorkflowResultError": status.HTTP_502_BAD_GATEWAY,
    "ResourceExhaustedError": status.HTTP_503_SERVICE_UNAVAILABLE,
}


def _signature_type(header: bytes) -> str | None:
    if header.startswith(b"%PDF-"):
        return "PDF"
    if header.startswith(b"\x89PNG\r\n\x1a\n"):
        return "PNG"
    if header.startswith(b"\xff\xd8\xff"):
        return "JPEG"
    if header.startswith(b"BM"):
        return "BMP"
    if header.startswith((b"II*\x00", b"MM\x00*")):
        return "TIFF"
    if len(header) >= 12 and header.startswith(b"RIFF") and header[8:12] == b"WEBP":
        return "WEBP"
    return None


def _validate_saved_upload(path: Path, signature_format: str) -> str:
    settings = get_settings()
    if signature_format == "PDF":
        inspect_pdf(path, settings)
        return ".pdf"
    try:
        with Image.open(path) as image:
            actual_format = (image.format or "").upper()
            if actual_format not in _IMAGE_FORMATS or actual_format != signature_format:
                raise InvalidInputError("The uploaded image format does not match its content")
            frame_count = int(getattr(image, "n_frames", 1))
            if frame_count > settings.max_pdf_pages:
                raise InputLimitError(
                    f"Image has {frame_count} frames; maximum is {settings.max_pdf_pages}"
                )
            for frame_index in range(frame_count):
                image.seek(frame_index)
                width, height = image.size
                if width < 1 or height < 1:
                    raise InvalidInputError("The uploaded image has invalid dimensions")
                if width * height > settings.max_image_pixels:
                    raise InputLimitError(
                        f"Image frame {frame_index + 1} exceeds the "
                        f"{settings.max_image_pixels:,}-pixel limit"
                    )
                image.load()
            return _IMAGE_FORMATS[actual_format]
    except (UnidentifiedImageError, OSError, ValueError) as exc:
        raise InvalidInputError("The uploaded image is malformed or unreadable") from exc


async def _save_upload(upload: UploadFile) -> Path:
    settings = get_settings()
    settings.upload_dir.mkdir(parents=True, exist_ok=True)
    temporary = settings.upload_dir / f".{uuid.uuid4().hex}.uploading"
    size = 0
    header = bytearray()
    try:
        async with aiofiles.open(temporary, "wb") as destination:
            while chunk := await upload.read(settings.upload_chunk_bytes):
                size += len(chunk)
                if size > settings.max_upload_bytes:
                    raise InputLimitError(
                        f"Upload exceeds the "
                        f"{settings.max_upload_bytes // _BYTES_PER_MEBIBYTE} MB limit"
                    )
                if len(header) < _UPLOAD_SIGNATURE_BYTES:
                    header.extend(chunk[: _UPLOAD_SIGNATURE_BYTES - len(header)])
                await destination.write(chunk)
        if size == 0:
            raise InvalidInputError("The uploaded file is empty")
        detected = _signature_type(bytes(header))
        if detected is None:
            from app.core.errors import UnsupportedInputError

            raise UnsupportedInputError(
                "Only verified PDF, PNG, JPEG, BMP, TIFF, and WebP files are accepted"
            )
        suffix = await run_in_threadpool(_validate_saved_upload, temporary, detected)
        final_path = settings.upload_dir / f"{uuid.uuid4().hex}{suffix}"
        os.replace(temporary, final_path)
        return final_path
    finally:
        await upload.close()
        temporary.unlink(missing_ok=True)


def _job_result_to_dict(
    result: JobResult,
    *,
    queue_position: int | None = None,
) -> dict[str, Any]:
    item_count = result.progress.total_pages
    if item_count is None and isinstance(result.result, dict):
        candidate = result.result.get("page_count")
        item_count = candidate if isinstance(candidate, int) else None
    if item_count is None and result.input_kind == "image":
        item_count = 1
    duration_ms = None
    if result.started_at:
        ended_at = result.completed_at or result.updated_at
        duration_ms = max(
            0,
            round((ended_at - result.started_at).total_seconds() * 1000),
        )
    artifact_status = None
    artifacts = None
    if isinstance(result.result, dict):
        artifact_status = result.result.get("artifact_status")
        artifacts = result.result.get("artifacts")
    return {
        "job_id": result.job_id,
        "status": result.status.value,
        "workflow": result.workflow,
        "input_name": result.input_name,
        "input_kind": result.input_kind,
        "language": result.language,
        "document_profile": result.document_profile,
        "item_count": item_count,
        "result": json_safe(result.result),
        "error": result.error,
        "history_saved": result.history_saved,
        "history_warning": result.history_warning,
        "cancel_requested": result.cancel_requested,
        "cancellable": result.status in {JobStatus.PENDING, JobStatus.PROCESSING},
        "queue_position": queue_position,
        "artifact_status": artifact_status,
        "artifacts": json_safe(artifacts),
        "progress": {
            "stage": result.progress.stage,
            "message": result.progress.message,
            "current_page": result.progress.current_page,
            "total_pages": result.progress.total_pages,
            "completed_pages": result.progress.completed_pages,
            "updated_at": result.progress.updated_at.isoformat(),
        },
        "created_at": result.created_at.isoformat(),
        "started_at": result.started_at.isoformat() if result.started_at else None,
        "completed_at": result.completed_at.isoformat() if result.completed_at else None,
        "duration_ms": duration_ms,
        "updated_at": result.updated_at.isoformat(),
    }


async def _run_uploaded_workflow(
    upload: UploadFile,
    *,
    workflow: Callable[..., Any],
    workflow_name: str,
    async_mode: bool,
    remove_text: str | None = None,
    remove_terms: list[str] | None = None,
    language: str | None = None,
    document_profile: str = "auto",
) -> Any:
    settings = get_settings()
    if not profile_allows_workflow(settings.runtime_profile, workflow_name):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                "Vision-Language processing is unavailable in the low-memory profile. "
                "Select the balanced CPU or full profile and restart PredixaLearn."
            ),
        )
    try:
        normalized_remove_text = normalize_remove_text(remove_text)
        normalized_remove_terms = normalize_remove_terms(remove_terms)
        selected_language = validate_language(
            language,
            ocr_version=settings.ocr_version,
            structure_version=settings.structure_ocr_version,
            default=settings.ocr_lang,
        )
        selected_profile = validate_document_profile(document_profile)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=str(exc),
        ) from exc
    original_name = Path((upload.filename or "upload").replace("\\", "/")).name
    saved_path = await _save_upload(upload)
    queue = get_queue()
    try:
        job_id = await queue.submit(
            workflow,
            saved_path,
            workflow_name=workflow_name,
            input_name=original_name or saved_path.name,
            delete_input=True,
            remove_text=normalized_remove_text,
            remove_terms=normalized_remove_terms,
            language=selected_language,
            document_profile=selected_profile,
        )
    except Exception:
        saved_path.unlink(missing_ok=True)
        raise
    if async_mode:
        return JSONResponse(
            status_code=status.HTTP_202_ACCEPTED,
            content={
                "job_id": job_id,
                "status": JobStatus.PENDING.value,
                "language": selected_language,
                "document_profile": selected_profile,
            },
        )
    result = await queue.wait_for_result(job_id)
    if result.status == JobStatus.COMPLETED:
        if result.history_warning:
            return JSONResponse(
                content=json_safe(result.result),
                headers={"X-OCR-History-Warning": result.history_warning},
            )
        return result.result
    response_status = _CLIENT_ERROR_CODES.get(
        result.error_code or "", status.HTTP_500_INTERNAL_SERVER_ERROR
    )
    raise HTTPException(
        status_code=response_status,
        detail=result.error or "OCR processing failed",
    )


def _format_cudnn_runtime(version: int | None) -> str | None:
    if version is None:
        return None
    major = version // _CUDNN_MAJOR_UNIT
    minor = (version % _CUDNN_MAJOR_UNIT) // _CUDNN_MINOR_UNIT
    patch = version % _CUDNN_MINOR_UNIT
    return f"{major}.{minor}.{patch}"


@router.get("/capabilities")
async def capabilities() -> dict[str, Any]:
    settings = get_settings()
    languages = supported_languages(settings.ocr_version, settings.structure_ocr_version)
    return {
        "runtime_profile": resolve_profile(settings.runtime_profile, settings.device).public(),
        "languages": language_capabilities(languages),
        "language_reliability": reliability_registry(),
        "document_profiles": list(DOCUMENT_PROFILES),
        "primary_modes": [
            {
                "id": "document",
                "workflow": "text",
                "label": "Document OCR",
                "description": "Complete document recognition and export",
                "recommended": True,
            },
            {
                "id": "vision_language",
                "workflow": "vl",
                "label": "Vision-Language",
                "description": "Advanced understanding for complex pages",
                "recommended": False,
            },
        ],
        "document_strategies": [
            {
                "id": "complete",
                "workflow": "text",
                "label": "Complete document",
                "description": "Text, structure, figures, formulas, and verified tables",
                "recommended": True,
            },
            {
                "id": "table_focused",
                "workflow": "table",
                "label": "Table-focused extraction",
                "description": "Verified tables with the complete document bundle",
                "recommended": False,
            },
            {
                "id": "layout_diagnostics",
                "workflow": "layout",
                "label": "Layout diagnostics",
                "description": "Structure-only parsing for specialist review",
                "recommended": False,
            },
        ],
        "defaults": {
            "language": settings.ocr_lang,
            "document_profile": "auto",
            "primary_mode": "document",
            "document_strategy": "complete",
        },
        "vision_language": {
            "language_mode": "english_only",
            "accepts_language_override": False,
        },
        "limits": {
            "max_upload_bytes": settings.max_upload_bytes,
            "max_pdf_pages": settings.max_pdf_pages,
            "max_image_pixels": settings.max_image_pixels,
            "max_queued_jobs": settings.max_queued_jobs,
            "file_extensions": [".pdf", ".png", ".jpg", ".jpeg", ".bmp", ".tif", ".tiff", ".webp"],
            "mime_types": [
                "application/pdf",
                "image/png",
                "image/jpeg",
                "image/bmp",
                "image/tiff",
                "image/webp",
            ],
        },
    }


@router.get("/health")
async def health_check() -> dict[str, Any]:
    settings = get_settings()
    issues: list[str] = []
    gpu: dict[str, Any] = {"requested_device": settings.device}
    ready = True
    document_export: dict[str, Any] = {"ready": False}
    warmup = get_warmup_status()
    if warmup["status"] == "initializing":
        # PaddleX requires PaddleOCR to control its first import. Do not let the
        # readiness probe pre-import ``paddle`` while the warmup thread is doing so.
        gpu["status"] = "initializing"
    else:
        try:
            import paddle

            actual_device = paddle.device.get_device()
            runtime_cudnn = _format_cudnn_runtime(paddle.device.get_cudnn_version())
            compiled_cudnn = paddle.version.cudnn()
            gpu.update(
                {
                    "device": actual_device,
                    "gpu_available": "gpu" in actual_device,
                    "cuda_compiled": paddle.version.cuda(),
                    "cudnn_compiled": compiled_cudnn,
                    "cudnn_runtime": runtime_cudnn,
                }
            )
            if settings.device.startswith("gpu") and "gpu" not in actual_device:
                issues.append("GPU was requested but Paddle is not using a GPU")
                ready = False
            if settings.device.startswith("gpu"):
                if not runtime_cudnn or not compiled_cudnn:
                    issues.append("The active cuDNN versions could not be verified")
                    ready = False
                elif runtime_cudnn != compiled_cudnn:
                    issues.append(
                        f"cuDNN runtime {runtime_cudnn} differs from "
                        f"Paddle's compiled {compiled_cudnn}"
                    )
                    ready = False
        except Exception:
            logger.exception("Paddle health check failed")
            # Keep the health payload schema stable when the optional Paddle
            # runtime is unavailable (for example, on a CPU-only CI runner).
            # Callers can still distinguish this degraded state through the
            # top-level status and the explicit ``None`` diagnostics.
            gpu.update(
                {
                    "device": None,
                    "gpu_available": False,
                    "cuda_compiled": None,
                    "cudnn_compiled": None,
                    "cudnn_runtime": None,
                }
            )
            issues.append("Paddle runtime is unavailable")
            ready = False

    try:
        import_module("docx")
        import_module("symspellpy")
        import_module("wordfreq")

        pandoc = pandoc_executable()
        libreoffice = libreoffice_health(settings)
        document_export = {
            "ready": True,
            "pandoc": pandoc.name,
            "python_docx": package_version("python-docx"),
            "symspellpy": package_version("symspellpy"),
            "wordfreq": package_version("wordfreq"),
            "libreoffice": libreoffice,
        }
        if not libreoffice["ready"]:
            document_export["ready"] = False
            issue = libreoffice.get("issue")
            if not issue and libreoffice.get("status") == "checking":
                issue = "Project-local LibreOffice Writer conversion is still being checked"
            issues.append(str(issue or "Project-local LibreOffice is unavailable"))
            ready = False
    except Exception:
        logger.exception("Document export health check failed")
        issues.append("Markdown/DOCX export dependencies are unavailable")
        ready = False

    if warmup["status"] == "initializing":
        ready = False
        issues.append("OCR engine is still initializing")
    elif warmup["status"] == "failed":
        ready = False
        issues.append(str(warmup["message"]))
    status_value = "ready" if ready and not issues else (
        "initializing" if warmup["status"] == "initializing" else "degraded"
    )
    return {
        "service_id": "predixalearn",
        "service_name": "PredixaLearn",
        "service_version": __version__,
        "status": status_value,
        "ready": ready,
        "issues": issues,
        "gpu": gpu,
        "engines": get_manager().status,
        "warmup": warmup,
        "document_export": document_export,
        "queue": {
            "active_or_queued": sum(
                item.status in {JobStatus.PENDING, JobStatus.PROCESSING}
                for item in get_queue().list_jobs().values()
            ),
            "max_queued": settings.max_queued_jobs,
        },
    }


@router.post("/ocr/text")
async def ocr_text(
    file: Annotated[UploadFile, File()],
    remove_text: Annotated[
        str | None,
        Form(max_length=MAX_REMOVE_TEXT_LENGTH),
    ] = None,
    remove_terms: Annotated[list[str] | None, Form()] = None,
    language: Annotated[str | None, Form(max_length=32)] = None,
    document_profile: Annotated[str, Form(max_length=16)] = "auto",
    async_mode: Annotated[bool, Query()] = False,
) -> Any:
    return await _run_uploaded_workflow(
        file,
        workflow=run_text_recognition,
        workflow_name="text_recognition",
        async_mode=async_mode,
        remove_text=remove_text,
        remove_terms=remove_terms,
        language=language,
        document_profile=document_profile,
    )


@router.post("/ocr/layout")
async def ocr_layout(
    file: Annotated[UploadFile, File()],
    remove_text: Annotated[
        str | None,
        Form(max_length=MAX_REMOVE_TEXT_LENGTH),
    ] = None,
    remove_terms: Annotated[list[str] | None, Form()] = None,
    language: Annotated[str | None, Form(max_length=32)] = None,
    document_profile: Annotated[str, Form(max_length=16)] = "auto",
    async_mode: Annotated[bool, Query()] = False,
) -> Any:
    return await _run_uploaded_workflow(
        file,
        workflow=run_layout_parsing,
        workflow_name="layout_parsing",
        async_mode=async_mode,
        remove_text=remove_text,
        remove_terms=remove_terms,
        language=language,
        document_profile=document_profile,
    )


@router.post("/ocr/table")
async def ocr_table(
    file: Annotated[UploadFile, File()],
    remove_text: Annotated[
        str | None,
        Form(max_length=MAX_REMOVE_TEXT_LENGTH),
    ] = None,
    remove_terms: Annotated[list[str] | None, Form()] = None,
    language: Annotated[str | None, Form(max_length=32)] = None,
    document_profile: Annotated[str, Form(max_length=16)] = "auto",
    async_mode: Annotated[bool, Query()] = False,
) -> Any:
    return await _run_uploaded_workflow(
        file,
        workflow=run_table_extraction,
        workflow_name="table_extraction",
        async_mode=async_mode,
        remove_text=remove_text,
        remove_terms=remove_terms,
        language=language,
        document_profile=document_profile,
    )


@router.post("/ocr/vl")
async def ocr_vl(
    file: Annotated[UploadFile, File()],
    remove_text: Annotated[
        str | None,
        Form(max_length=MAX_REMOVE_TEXT_LENGTH),
    ] = None,
    remove_terms: Annotated[list[str] | None, Form()] = None,
    async_mode: Annotated[bool, Query()] = False,
) -> Any:
    try:
        normalized_remove_text = normalize_remove_text(remove_text)
        normalized_remove_terms = normalize_remove_terms(remove_terms)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=str(exc),
        ) from exc
    original_name = Path((file.filename or "upload").replace("\\", "/")).name
    saved_path = await _save_upload(file)
    try:
        job_id = await get_queue().submit(
            run_vl_processing,
            saved_path,
            workflow_name="vl_processing",
            input_name=original_name or saved_path.name,
            delete_input=True,
            remove_text=normalized_remove_text,
            remove_terms=normalized_remove_terms,
        )
    except Exception:
        saved_path.unlink(missing_ok=True)
        raise
    if async_mode:
        return JSONResponse(
            status_code=status.HTTP_202_ACCEPTED,
            content={"job_id": job_id, "status": JobStatus.PENDING.value},
        )
    result = await get_queue().wait_for_result(job_id)
    if result.status == JobStatus.COMPLETED:
        return result.result
    response_status = _CLIENT_ERROR_CODES.get(
        result.error_code or "", status.HTTP_500_INTERNAL_SERVER_ERROR
    )
    raise HTTPException(
        status_code=response_status,
        detail=result.error or "OCR processing failed",
    )


@router.get("/jobs/{job_id}")
async def get_job_status(job_id: str) -> dict[str, Any]:
    queue = get_queue()
    result = queue.get_result(job_id)
    if result is None:
        raise HTTPException(status_code=404, detail="Job not found")
    return _job_result_to_dict(result, queue_position=queue.queue_position(job_id))


@router.delete("/jobs/{job_id}")
async def cancel_job(job_id: str) -> dict[str, Any]:
    queue = get_queue()
    result = queue.cancel(job_id)
    if result is None:
        raise HTTPException(status_code=404, detail="Job not found")
    return _job_result_to_dict(result, queue_position=queue.queue_position(job_id))


@router.get("/jobs")
async def list_jobs() -> dict[str, dict[str, Any]]:
    queue = get_queue()
    return {
        job_id: _job_result_to_dict(result, queue_position=queue.queue_position(job_id))
        for job_id, result in queue.list_jobs().items()
    }
