"""Tenant-scoped artifact publication and retrieval."""

from __future__ import annotations

import hashlib
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, File, HTTPException, Request, Response, UploadFile
from fastapi.responses import StreamingResponse
from sqlalchemy.orm import Session

from .api import db_session, service_for
from .models import Resource
from .schemas import ResourceCreate
from .security import Principal, require
from .service import serialize_resource
from .storage import create_storage

router = APIRouter(prefix="/api/v2/artifacts", tags=["artifacts"])
MAX_ARTIFACT_BYTES = 100 * 1024 * 1024


@router.post("", status_code=201)
async def publish_artifact(
    request: Request,
    response: Response,
    session: Annotated[Session, Depends(db_session)],
    principal: Annotated[Principal, Depends(require("archive:write"))],
    file: Annotated[UploadFile, File()],
    parent_id: str,
    kind: Literal["result_json", "markdown", "docx", "preview", "crop", "table", "dataset_export"],
) -> dict[str, Any]:
    service = service_for(session, principal, request)
    parent = session.scalar(
        service._query().where(Resource.resource_id == parent_id)  # noqa: SLF001
    )
    if parent is None:
        raise HTTPException(
            status_code=404,
            detail={"code": "parent_not_found", "message": "Artifact parent was not found"},
        )
    data = await file.read(MAX_ARTIFACT_BYTES + 1)
    if len(data) > MAX_ARTIFACT_BYTES:
        raise HTTPException(status_code=413, detail={"code": "artifact_too_large", "message": "Artifact exceeds the 100 MiB API limit"})
    digest = hashlib.sha256(data).hexdigest()
    storage_key = f"{principal.tenant_id}/artifacts/{digest[:2]}/{digest}"
    stored = create_storage().put(
        storage_key,
        data,
        content_type=file.content_type or "application/octet-stream",
    )
    create = ResourceCreate(
        title=file.filename or f"{kind} artifact",
        status="published",
        parent_id=parent_id,
        content_sha256=digest,
        payload={"kind": kind, **stored},
    )
    resource = service.create("artifact", create, immutable=True)
    response.headers["ETag"] = f'W/"{resource.version}"'
    return serialize_resource(resource)


@router.get("/{artifact_id}")
async def download_artifact(
    artifact_id: str,
    request: Request,
    session: Annotated[Session, Depends(db_session)],
    principal: Annotated[Principal, Depends(require("archive:read"))],
):
    resource = service_for(session, principal, request).get("artifact", artifact_id)
    key = str(resource.payload.get("key", ""))
    try:
        data = create_storage().read(key)
    except (OSError, ValueError) as exc:
        raise HTTPException(status_code=404, detail={"code": "artifact_missing", "message": "Artifact content is unavailable"}) from exc
    digest = hashlib.sha256(data).hexdigest()
    if digest != resource.content_sha256:
        raise HTTPException(status_code=409, detail={"code": "artifact_corrupt", "message": "Artifact checksum verification failed"})
    return StreamingResponse(
        iter([data]),
        media_type=str(resource.payload.get("content_type", "application/octet-stream")),
        headers={"Content-Disposition": "attachment", "X-Content-SHA256": digest},
    )
