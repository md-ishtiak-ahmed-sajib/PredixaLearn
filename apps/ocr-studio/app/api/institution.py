"""Explicit local institution enrollment and History-sync controls."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, ConfigDict, Field

from app.core.config import get_settings
from app.institution.credentials import CredentialStoreError, delete_device_credential
from app.institution.service import (
    InstitutionSyncError,
    enroll,
    queue_history,
    read_connection,
    sync_pending,
)
from app.storage.history import (
    HistoryConflictError,
    HistoryNotFoundError,
    HistoryResultError,
    get_history_store,
)

router = APIRouter(prefix="/api/v1/institution", tags=["institution-sync"])


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class EnrollmentRequest(StrictModel):
    base_url: str = Field(min_length=8, max_length=2000)
    enrollment_token: str = Field(min_length=32, max_length=500)


class QueueRequest(StrictModel):
    job_ids: list[str] = Field(min_length=1, max_length=100)
    include_approved_previews: bool = False


def _safe_error(exc: Exception) -> HTTPException:
    return HTTPException(status_code=409, detail=str(exc))


@router.get("/status")
async def institution_status() -> dict[str, Any]:
    try:
        connection = read_connection()
    except InstitutionSyncError as exc:
        raise _safe_error(exc) from exc
    outbox = get_history_store().list_sync_outbox()
    counts = {status: 0 for status in ("pending", "in_flight", "completed", "failed")}
    for item in outbox:
        counts[item.status] += 1
    return {
        "enrolled": connection is not None,
        "connection": (
            {
                "base_url": connection.base_url,
                "tenant_id": connection.tenant_id,
                "worker_id": connection.worker_id,
                "enrolled_at": connection.enrolled_at,
            }
            if connection
            else None
        ),
        "outbox": counts,
        "automatic_history_upload": False,
    }


@router.post("/enroll", status_code=201)
async def enroll_device(body: EnrollmentRequest) -> dict[str, Any]:
    try:
        connection = await enroll(body.base_url, body.enrollment_token)
    except (InstitutionSyncError, CredentialStoreError) as exc:
        raise _safe_error(exc) from exc
    return {
        "enrolled": True,
        "base_url": connection.base_url,
        "tenant_id": connection.tenant_id,
        "worker_id": connection.worker_id,
        "existing_history_uploaded": False,
    }


@router.delete("/enrollment")
async def remove_enrollment() -> dict[str, bool]:
    connection = read_connection()
    if connection is None:
        return {"removed": False}
    try:
        delete_device_credential(connection.worker_id)
    except CredentialStoreError as exc:
        raise _safe_error(exc) from exc
    (get_settings().institution_dir / "connection.json").unlink(missing_ok=True)
    return {"removed": True}


@router.post("/outbox", status_code=201)
async def select_history_for_sync(body: QueueRequest) -> dict[str, Any]:
    try:
        queued = queue_history(
            body.job_ids, include_approved_previews=body.include_approved_previews
        )
    except (InstitutionSyncError, HistoryConflictError, HistoryNotFoundError, HistoryResultError, ValueError) as exc:
        raise _safe_error(exc) from exc
    return {
        "items": [item.public() for item in queued],
        "explicit_selection": True,
        "source_uploads_included": False,
        "approved_previews_included": body.include_approved_previews,
    }


@router.get("/outbox")
async def list_outbox(status: str | None = None) -> dict[str, Any]:
    return {
        "items": [item.public() for item in get_history_store().list_sync_outbox(status=status)]
    }


@router.post("/sync-now")
async def synchronize_selected_history() -> dict[str, Any]:
    try:
        return await sync_pending()
    except InstitutionSyncError as exc:
        raise _safe_error(exc) from exc
