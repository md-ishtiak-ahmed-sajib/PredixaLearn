"""Confirmed, legal-hold-aware retention for terminal institutional records."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import Field
from sqlalchemy.orm import Session

from .api import db_session, service_for
from .models import Resource
from .schemas import StrictModel
from .security import Principal, require
from .storage import create_storage, validate_key

router = APIRouter(prefix="/api/v2/retention", tags=["retention"])

ALLOWED_RESOURCE_TYPES = {
    "archive_document",
    "evidence_version",
    "review_task",
    "answer_script",
    "answer_response",
    "dataset_item",
    "dataset_release",
    "accessibility_description",
    "worker_job",
    "worker_source",
}
TERMINAL_STATUSES = {
    "approved",
    "archived",
    "cancelled",
    "completed",
    "consumed",
    "expired",
    "published",
    "rejected",
    "released",
    "retired",
    "superseded",
}


class RetentionRequest(StrictModel):
    older_than_days: int = Field(ge=1, le=36_500)
    resource_types: list[str] = Field(min_length=1, max_length=20)
    confirm: bool = False


def _candidates(
    session: Session, principal: Principal, request: Request, body: RetentionRequest
) -> tuple[Any, list[Resource]]:
    unknown = set(body.resource_types) - ALLOWED_RESOURCE_TYPES
    if unknown:
        raise HTTPException(
            status_code=422,
            detail={
                "code": "unsupported_retention_type",
                "message": "Retention request contains unsupported resource types",
                "resource_types": sorted(unknown),
            },
        )
    service = service_for(session, principal, request)
    cutoff = datetime.now(timezone.utc) - timedelta(days=body.older_than_days)
    rows = list(
        session.scalars(
            service._query()  # noqa: SLF001
            .where(
                Resource.resource_type.in_(body.resource_types),
                Resource.status.in_(TERMINAL_STATUSES),
                Resource.updated_at < cutoff,
            )
            .order_by(Resource.updated_at)
            .limit(101)
        )
    )
    return service, [row for row in rows if not bool(row.payload.get("legal_hold"))]


@router.post("/preview")
async def preview_retention(
    body: RetentionRequest,
    request: Request,
    session: Annotated[Session, Depends(db_session)],
    principal: Annotated[Principal, Depends(require("*"))],
) -> dict[str, Any]:
    _service, candidates = _candidates(session, principal, request, body)
    return {
        "apply_allowed": len(candidates) <= 100,
        "candidate_count": len(candidates),
        "truncated": len(candidates) > 100,
        "items": [
            {
                "id": item.resource_id,
                "type": item.resource_type,
                "status": item.status,
                "updated_at": item.updated_at.isoformat(),
            }
            for item in candidates[:100]
        ],
        "legal_holds_preserved": True,
    }


@router.post("/apply")
async def apply_retention(
    body: RetentionRequest,
    request: Request,
    session: Annotated[Session, Depends(db_session)],
    principal: Annotated[Principal, Depends(require("*"))],
) -> dict[str, Any]:
    if not body.confirm:
        raise HTTPException(
            status_code=409,
            detail={"code": "confirmation_required", "message": "Retention deletion requires confirm=true"},
        )
    service, candidates = _candidates(session, principal, request, body)
    if len(candidates) > 100:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "retention_batch_too_large",
                "message": "Reduce the retention window to a batch of at most 100 records",
            },
        )
    storage_keys: list[str] = []
    deleted: list[dict[str, str]] = []
    for resource in candidates:
        possible_storage: list[object] = [
            resource.payload.get("storage"),
            resource.payload,
            resource.payload.get("source_artifact"),
        ]
        uploaded = resource.payload.get("uploaded_artifacts", [])
        if isinstance(uploaded, list):
            possible_storage.extend(uploaded)
        for storage in possible_storage:
            if not isinstance(storage, dict):
                continue
            candidate = storage.get("key", storage.get("storage_key"))
            if not isinstance(candidate, str):
                continue
            try:
                normalized = validate_key(candidate)
            except ValueError:
                continue
            if normalized.startswith(f"{principal.tenant_id}/"):
                storage_keys.append(normalized)
        service.audit(
            "retention.deleted",
            resource,
            {"resource_type": resource.resource_type, "legal_hold": False},
        )
        deleted.append({"id": resource.resource_id, "type": resource.resource_type})
        session.delete(resource)
    session.commit()
    object_storage = create_storage()
    object_delete_failures = 0
    for key in dict.fromkeys(storage_keys):
        try:
            object_storage.delete(key)
        except Exception:
            object_delete_failures += 1
    return {
        "deleted": deleted,
        "deleted_count": len(deleted),
        "object_delete_failures": object_delete_failures,
        "legal_holds_preserved": True,
    }
