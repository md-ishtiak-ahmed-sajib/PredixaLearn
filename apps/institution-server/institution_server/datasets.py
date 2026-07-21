"""Versioned JSONL export for human-verified institution datasets."""

from __future__ import annotations

import hashlib
import json
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import Response
from sqlalchemy.orm import Session

from .api import db_session, service_for
from .security import Principal, require

router = APIRouter(prefix="/api/v2", tags=["datasets"])


@router.get("/datasets/releases/{release_id}/download")
async def download_dataset_release(
    release_id: str,
    request: Request,
    session: Annotated[Session, Depends(db_session)],
    principal: Annotated[Principal, Depends(require("dataset:read"))],
) -> Response:
    release = service_for(session, principal, request).get("dataset_release", release_id)
    if release.status != "published" or not release.immutable:
        raise HTTPException(status_code=409, detail={"code": "dataset_not_released", "message": "Dataset release is not immutable and published"})
    items = release.payload.get("items")
    if not isinstance(items, list):
        raise HTTPException(status_code=409, detail={"code": "dataset_manifest_invalid", "message": "Dataset release manifest is invalid"})
    manifest = {
        "record_type": "manifest",
        "schema_version": release.payload.get("schema_version"),
        "release_id": release.resource_id,
        "manifest_sha256": release.payload.get("manifest_sha256"),
        "split_policy": release.payload.get("split_policy"),
        "item_count": len(items),
        "tenant_isolated": True,
    }
    lines = [json.dumps(manifest, ensure_ascii=False, sort_keys=True)]
    lines.extend(
        json.dumps({"record_type": "item", **item}, ensure_ascii=False, sort_keys=True)
        for item in items
    )
    content = ("\n".join(lines) + "\n").encode()
    digest = hashlib.sha256(content).hexdigest()
    return Response(
        content,
        media_type="application/x-ndjson",
        headers={
            "Content-Disposition": f'attachment; filename="dataset-{release_id}.jsonl"',
            "X-Content-SHA256": digest,
        },
    )
