"""Idempotent ingestion of explicitly selected local OCR evidence."""

from __future__ import annotations

import base64
import hashlib
import json
import re
import time
from typing import Annotated, Any

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from fastapi import APIRouter, Depends, Header, HTTPException, Request
from predixalearn_protocol import SyncEnvelope
from sqlalchemy import select
from sqlalchemy.orm import Session

from .api import db_session
from .models import IdempotencyRecord, Resource, WorkerDevice
from .schemas import ResourceCreate
from .security import Principal
from .service import ResourceService, serialize_resource
from .storage import create_storage
from .workers import current_worker

router = APIRouter(prefix="/api/v2", tags=["synchronization"])
MAX_SYNC_BODY_BYTES = 10 * 1024 * 1024
MAX_APPROVED_PREVIEW_BYTES = 4 * 1024 * 1024
MAX_APPROVED_PREVIEWS = 5
PREVIEW_NAME = re.compile(r"^page-[0-9]{3}\.png$")


def _worker_principal(worker: WorkerDevice) -> Principal:
    return Principal(
        subject=f"worker:{worker.worker_id}",
        tenant_id=worker.tenant_id,
        roles=frozenset({"worker"}),
        permissions=frozenset({"archive:write"}),
        academic_unit_ids=frozenset(),
        course_ids=frozenset(),
        enabled_features=frozenset({"archive_review", "workers"}),
        csrf_token=None,
    )


@router.post("/sync")
async def ingest_sync_envelope(
    envelope: SyncEnvelope,
    request: Request,
    session: Annotated[Session, Depends(db_session)],
    worker: Annotated[WorkerDevice, Depends(current_worker)],
    idempotency_header: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
    device_timestamp: Annotated[
        str | None, Header(alias="X-PredixaLearn-Device-Timestamp")
    ] = None,
    device_signature: Annotated[
        str | None, Header(alias="X-PredixaLearn-Device-Signature")
    ] = None,
) -> dict[str, Any]:
    if envelope.tenant_id != worker.tenant_id or envelope.device_id != worker.worker_id:
        raise HTTPException(status_code=403, detail={"code": "sync_scope_mismatch", "message": "Sync envelope does not belong to this device"})
    serialized_envelope = envelope.model_dump(mode="json")
    canonical = json.dumps(
        serialized_envelope, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode()
    if len(canonical) > MAX_SYNC_BODY_BYTES:
        raise HTTPException(status_code=413, detail={"code": "sync_payload_too_large", "message": "Sync envelope exceeds 10 MiB"})
    try:
        signed_at = int(device_timestamp or "")
        if abs(int(time.time()) - signed_at) > 300:
            raise ValueError("signature expired")
        public_raw = base64.urlsafe_b64decode(
            worker.device_public_key + "=" * (-len(worker.device_public_key) % 4)
        )
        signature_raw = base64.urlsafe_b64decode(
            str(device_signature or "") + "=" * (-len(str(device_signature or "")) % 4)
        )
        Ed25519PublicKey.from_public_bytes(public_raw).verify(
            signature_raw, str(signed_at).encode() + b"." + canonical
        )
    except (ValueError, TypeError, InvalidSignature) as exc:
        raise HTTPException(
            status_code=401,
            detail={
                "code": "invalid_device_signature",
                "message": "Sync evidence is not signed by the enrolled device",
            },
        ) from exc
    if idempotency_header and idempotency_header not in {
        event.idempotency_key for event in envelope.events
    }:
        raise HTTPException(status_code=409, detail={"code": "idempotency_mismatch", "message": "Header and event idempotency keys do not match"})

    principal = _worker_principal(worker)
    service = ResourceService(session, principal, str(request.state.request_id))
    storage = create_storage()
    accepted: list[dict[str, Any]] = []
    for event in envelope.events:
        event_payload = {
            "event_type": event.event_type,
            "resource_id": event.resource_id,
            "resource_version": event.resource_version,
            "payload": event.payload,
        }
        request_hash = hashlib.sha256(
            json.dumps(event_payload, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        existing_idempotency = session.scalar(
            select(IdempotencyRecord).where(
                IdempotencyRecord.tenant_id == worker.tenant_id,
                IdempotencyRecord.endpoint == "/api/v2/sync",
                IdempotencyRecord.idempotency_key == event.idempotency_key,
            )
        )
        if existing_idempotency:
            if existing_idempotency.request_hash != request_hash:
                raise HTTPException(status_code=409, detail={"code": "idempotency_conflict", "message": "Idempotency key was reused with different evidence"})
            accepted.append({**existing_idempotency.response_body, "replayed": True})
            continue
        if event.event_type != "archive.upsert":
            raise HTTPException(status_code=422, detail={"code": "unsupported_sync_event", "message": "This sync event type is not accepted yet"})
        payload = event.payload
        result = payload.get("result")
        evidence_hash = str(payload.get("ocr_result_sha256", ""))
        if len(evidence_hash) != 64 or any(value not in "0123456789abcdef" for value in evidence_hash):
            raise HTTPException(status_code=422, detail={"code": "invalid_evidence_hash", "message": "OCR evidence hash is invalid"})
        result_bytes = json.dumps(
            result, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode()
        if hashlib.sha256(result_bytes).hexdigest() != evidence_hash:
            raise HTTPException(status_code=422, detail={"code": "evidence_hash_mismatch", "message": "OCR evidence does not match its immutable hash"})
        overlay = payload.get("analysis_overlay")
        analysis: dict[str, Any] | None = None
        analysis_bytes: bytes | None = None
        analysis_hash: str | None = None
        if isinstance(overlay, dict) and isinstance(overlay.get("analysis"), dict):
            analysis = overlay["analysis"]
            if analysis.get("ocr_result_sha256") != evidence_hash:
                raise HTTPException(
                    status_code=422,
                    detail={
                        "code": "analysis_evidence_mismatch",
                        "message": "Analysis overlay does not reference the synchronized OCR evidence",
                    },
                )
            analysis_bytes = json.dumps(
                overlay, ensure_ascii=False, sort_keys=True, separators=(",", ":")
            ).encode()
            analysis_hash = hashlib.sha256(analysis_bytes).hexdigest()
        approved_previews: list[dict[str, Any]] = []
        raw_previews = payload.get("approved_previews", [])
        if not isinstance(raw_previews, list) or len(raw_previews) > MAX_APPROVED_PREVIEWS:
            raise HTTPException(
                status_code=422,
                detail={
                    "code": "invalid_approved_previews",
                    "message": "At most five explicitly approved previews are accepted",
                },
            )
        preview_total = 0
        for preview in raw_previews:
            try:
                filename = str(preview["filename"])
                media_type = str(preview["media_type"])
                expected_hash = str(preview["sha256"])
                content = base64.b64decode(str(preview["content_base64"]), validate=True)
                explicitly_approved = preview["explicitly_approved"] is True
            except (KeyError, TypeError, ValueError) as exc:
                raise HTTPException(
                    status_code=422,
                    detail={
                        "code": "invalid_approved_previews",
                        "message": "Approved preview metadata is malformed",
                    },
                ) from exc
            preview_total += len(content)
            if (
                not PREVIEW_NAME.fullmatch(filename)
                or media_type != "image/png"
                or not explicitly_approved
                or not content.startswith(b"\x89PNG\r\n\x1a\n")
                or hashlib.sha256(content).hexdigest() != expected_hash
                or preview_total > MAX_APPROVED_PREVIEW_BYTES
            ):
                raise HTTPException(
                    status_code=422,
                    detail={
                        "code": "invalid_approved_previews",
                        "message": "Approved preview failed type, checksum, or size validation",
                    },
                )
            approved_previews.append(
                {
                    "filename": filename,
                    "media_type": media_type,
                    "sha256": expected_hash,
                    "content": content,
                }
            )
        object_key = f"{worker.tenant_id}/evidence/{evidence_hash[:2]}/{evidence_hash}.json"
        storage_metadata = storage.put(
            object_key, result_bytes, content_type="application/json"
        )
        document = session.scalar(
            service._query("archive_document").where(  # noqa: SLF001
                Resource.resource_id == event.resource_id
            )
        )
        if document is None:
            document = service.create(
                "archive_document",
                ResourceCreate(
                    title=str(payload.get("title") or "Synced OCR document")[:500],
                    status="indexed",
                    content_sha256=evidence_hash,
                    search_text=str(payload.get("search_text", ""))[:2_000_000],
                    payload={
                        "local_history_summary": payload.get("summary", {}),
                        "current_evidence_sha256": evidence_hash,
                        "source_retained": False,
                        "synced_by_device": worker.worker_id,
                    },
                ),
                resource_id=event.resource_id,
            )
        elif document.content_sha256 != evidence_hash:
            document.content_sha256 = evidence_hash
            document.search_text = str(payload.get("search_text", ""))[:2_000_000]
            document.payload = {
                **document.payload,
                "current_evidence_sha256": evidence_hash,
                "synced_by_device": worker.worker_id,
            }
            document.version += 1
            service.audit("archive.new_evidence", document, {"sha256": evidence_hash})
            session.commit()
        evidence_id = f"{event.resource_id}-{evidence_hash[:16]}"
        evidence = session.scalar(
            service._query("evidence_version").where(  # noqa: SLF001
                Resource.resource_id == evidence_id
            )
        )
        if evidence is None:
            evidence = service.create(
                "evidence_version",
                ResourceCreate(
                    title=f"Evidence for {document.title}",
                    status="published",
                    parent_id=document.resource_id,
                    content_sha256=evidence_hash,
                    payload={
                        "storage": storage_metadata,
                        "source_retained": False,
                        "protocol_version": envelope.protocol_version,
                    },
                ),
                immutable=True,
                resource_id=evidence_id,
            )
        analysis_id = None
        if analysis is not None and analysis_bytes is not None and analysis_hash is not None:
            analysis_id = f"{event.resource_id}-analysis-{analysis_hash[:16]}"
            existing_analysis = session.scalar(
                service._query("analysis_overlay").where(  # noqa: SLF001
                    Resource.resource_id == analysis_id
                )
            )
            if existing_analysis is None:
                analysis_storage = storage.put(
                    f"{worker.tenant_id}/analysis/{analysis_hash[:2]}/{analysis_hash}.json",
                    analysis_bytes,
                    content_type="application/json",
                )
                service.create(
                    "analysis_overlay",
                    ResourceCreate(
                        title=f"Analysis overlay for {document.title}",
                        status="published",
                        parent_id=document.resource_id,
                        content_sha256=analysis_hash,
                        payload={
                            "storage": analysis_storage,
                            "ocr_result_sha256": evidence_hash,
                            "model": analysis.get("model", {}),
                            "audit_event_count": len(overlay.get("audit_events", [])),
                        },
                    ),
                    immutable=True,
                    resource_id=analysis_id,
                )
        preview_ids: list[str] = []
        for preview in approved_previews:
            preview_id = f"{event.resource_id}-preview-{preview['sha256'][:16]}"
            existing_preview = session.scalar(
                service._query("artifact").where(  # noqa: SLF001
                    Resource.resource_id == preview_id
                )
            )
            if existing_preview is None:
                preview_storage = storage.put(
                    f"{worker.tenant_id}/previews/{preview['sha256'][:2]}/{preview['sha256']}.png",
                    preview["content"],
                    content_type="image/png",
                )
                service.create(
                    "artifact",
                    ResourceCreate(
                        title=preview["filename"],
                        status="published",
                        parent_id=document.resource_id,
                        content_sha256=preview["sha256"],
                        payload={
                            "kind": "preview",
                            "explicitly_approved": True,
                            "watermarked": True,
                            **preview_storage,
                        },
                    ),
                    immutable=True,
                    resource_id=preview_id,
                )
            preview_ids.append(preview_id)
        response_body = {
            "event_id": event.event_id,
            "resource": serialize_resource(document),
            "evidence_id": evidence.resource_id,
            "analysis_id": analysis_id,
            "approved_preview_ids": preview_ids,
            "replayed": False,
        }
        session.add(
            IdempotencyRecord(
                tenant_id=worker.tenant_id,
                endpoint="/api/v2/sync",
                idempotency_key=event.idempotency_key,
                request_hash=request_hash,
                response_status=200,
                response_body=response_body,
            )
        )
        session.commit()
        accepted.append(response_body)
    return {"accepted": accepted, "uploaded_existing_history_automatically": False}
