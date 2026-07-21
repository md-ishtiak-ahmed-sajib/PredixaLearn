"""One-time OCR-worker enrollment and signed job-envelope APIs."""

from __future__ import annotations

import base64
import hashlib
import io
import json
import logging
import secrets
import zipfile
from datetime import datetime, timedelta, timezone
from typing import Annotated, Any, Literal

import jwt
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)
from fastapi import (
    APIRouter,
    Depends,
    File,
    Header,
    HTTPException,
    Query,
    Request,
    Response,
    UploadFile,
)
from fastapi.responses import StreamingResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from predixalearn_protocol import ArtifactDescriptor, WorkerJobEnvelope
from sqlalchemy import select
from sqlalchemy.orm import Session

from .api import db_session, service_for
from .config import Settings, get_settings
from .database import set_tenant_context
from .models import Resource, WorkerDevice, WorkerEnrollmentToken
from .schemas import (
    ResourceCreate,
    StrictModel,
    WorkerEnrollmentCreate,
    WorkerJobCreate,
    WorkerStateUpdate,
)
from .security import Principal, require
from .service import serialize_resource
from .storage import create_storage, validate_key

router = APIRouter(prefix="/api/v2", tags=["workers"])
worker_bearer = HTTPBearer(auto_error=False)
logger = logging.getLogger(__name__)
MAX_WORKER_SOURCE_BYTES = 100 * 1024 * 1024
MAX_WORKER_OUTPUT_BYTES = 100 * 1024 * 1024
SOURCE_MEDIA_TYPES = {
    "application/pdf",
    "image/png",
    "image/jpeg",
    "image/bmp",
    "image/tiff",
    "image/webp",
}


def _source_media_type(data: bytes) -> str | None:
    if data.startswith(b"%PDF-"):
        return "application/pdf"
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if data.startswith(b"BM"):
        return "image/bmp"
    if data.startswith((b"II*\x00", b"MM\x00*")):
        return "image/tiff"
    if len(data) >= 12 and data.startswith(b"RIFF") and data[8:12] == b"WEBP":
        return "image/webp"
    return None


def _validate_worker_output(kind: str, data: bytes, media_type: str) -> None:
    try:
        if kind == "result_json":
            if media_type != "application/json" or not isinstance(
                json.loads(data.decode("utf-8")), dict
            ):
                raise ValueError("result JSON must be an object")
        elif kind == "markdown":
            if media_type not in {"text/markdown", "text/plain"}:
                raise ValueError("Markdown media type is invalid")
            data.decode("utf-8")
        elif kind == "docx":
            expected = (
                "application/vnd.openxmlformats-officedocument."
                "wordprocessingml.document"
            )
            if media_type != expected or not zipfile.is_zipfile(io.BytesIO(data)):
                raise ValueError("DOCX is invalid")
        elif kind in {"preview", "crop"}:
            if media_type != "image/png" or not data.startswith(b"\x89PNG\r\n\x1a\n"):
                raise ValueError("Preview must be PNG")
        elif kind == "table":
            allowed_table_media = {
                "text/csv",
                "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            }
            if media_type not in allowed_table_media:
                raise ValueError("Table media type is invalid")
    except (UnicodeDecodeError, ValueError, json.JSONDecodeError) as exc:
        raise HTTPException(
            status_code=422,
            detail={
                "code": "artifact_content_invalid",
                "message": "Worker output content does not match its approved type",
            },
        ) from exc


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def _expire_staged_sources(session: Session, tenant_id: str) -> None:
    now = datetime.now(timezone.utc)
    changed = False
    staged_sources = list(
        session.scalars(
            select(Resource).where(
                Resource.tenant_id == tenant_id,
                Resource.resource_type == "worker_source",
                Resource.status == "staged",
            ).limit(100)
        )
    )
    for source in staged_sources:
        try:
            expires_at = datetime.fromisoformat(
                str(source.payload.get("expires_at", "")).replace("Z", "+00:00")
            )
        except ValueError:
            expires_at = now - timedelta(seconds=1)
        if expires_at.tzinfo is not None and expires_at > now:
            continue
        storage = source.payload.get("storage", {})
        key = storage.get("key") if isinstance(storage, dict) else None
        try:
            if isinstance(key, str):
                create_storage().delete(key)
        except Exception:
            logger.exception("Expired worker source cleanup failed for %s", source.resource_id)
            continue
        source.status = "expired"
        source.version += 1
        changed = True
    if changed:
        session.commit()


class EnrollmentTokenRequest(StrictModel):
    expires_minutes: int = 15


class EnrollmentRedeem(WorkerEnrollmentCreate):
    enrollment_token: str


class WorkerResult(StrictModel):
    status: str
    artifacts: list[dict[str, Any]] = []
    result_summary: dict[str, Any] = {}


def _worker_secret(settings: Settings) -> str:
    value = settings.worker_signing_key or settings.test_jwt_secret
    if not value:
        raise HTTPException(status_code=503, detail={"code": "worker_channel_unavailable", "message": "Worker signing is not configured"})
    return value


def _ed25519_key(settings: Settings) -> Ed25519PrivateKey:
    value = _worker_secret(settings)
    if "BEGIN PRIVATE KEY" in value:
        key = serialization.load_pem_private_key(value.encode(), password=None)
        if not isinstance(key, Ed25519PrivateKey):
            raise ValueError("Worker signing key must be Ed25519")
        return key
    try:
        raw = base64.b64decode(value, validate=True)
    except ValueError:
        raw = hashlib.sha256(value.encode()).digest()
    if len(raw) != 32:
        raw = hashlib.sha256(raw).digest()
    return Ed25519PrivateKey.from_private_bytes(raw)


def _sign_envelope(payload: dict[str, Any], settings: Settings) -> dict[str, Any]:
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str).encode()
    key = _ed25519_key(settings)
    signature = base64.urlsafe_b64encode(key.sign(encoded)).decode().rstrip("=")
    public_key = base64.urlsafe_b64encode(
        key.public_key().public_bytes(
            encoding=serialization.Encoding.Raw,
            format=serialization.PublicFormat.Raw,
        )
    ).decode().rstrip("=")
    return {
        "algorithm": "Ed25519",
        "payload": payload,
        "signature": signature,
        "public_key": public_key,
    }


def _server_public_key(settings: Settings) -> str:
    return base64.urlsafe_b64encode(
        _ed25519_key(settings).public_key().public_bytes(
            encoding=serialization.Encoding.Raw,
            format=serialization.PublicFormat.Raw,
        )
    ).decode().rstrip("=")


async def current_worker(
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(worker_bearer)],
    settings: Annotated[Settings, Depends(get_settings)],
    session: Annotated[Session, Depends(db_session)],
) -> WorkerDevice:
    if credentials is None:
        raise HTTPException(status_code=401, detail={"code": "worker_auth_required", "message": "Worker credential required"})
    try:
        claims = jwt.decode(
            credentials.credentials,
            _worker_secret(settings),
            algorithms=["HS256"],
            audience="predixalearn-worker",
            options={"require": ["exp", "sub", "tenant_id"]},
        )
    except jwt.PyJWTError as exc:
        raise HTTPException(status_code=401, detail={"code": "invalid_worker_token", "message": "Worker credential is invalid"}) from exc
    set_tenant_context(session, str(claims["tenant_id"]))
    worker = session.get(WorkerDevice, str(claims["sub"]))
    if (
        worker is None
        or worker.tenant_id != claims["tenant_id"]
        or worker.revoked
        or worker.status == "revoked"
    ):
        raise HTTPException(status_code=403, detail={"code": "worker_revoked", "message": "Worker is unavailable"})
    worker.last_seen_at = datetime.now(timezone.utc)
    session.commit()
    return worker


def _serialize_worker(worker: WorkerDevice) -> dict[str, Any]:
    return {
        "worker_id": worker.worker_id,
        "name": worker.name,
        "status": worker.status,
        "version": worker.version,
        "etag": f'W/"{worker.version}"',
        "capabilities": worker.capabilities,
        "last_seen_at": worker.last_seen_at.isoformat() if worker.last_seen_at else None,
        "created_at": worker.created_at.isoformat(),
    }


@router.get("/workers")
async def list_workers(
    session: Annotated[Session, Depends(db_session)],
    principal: Annotated[Principal, Depends(require("worker:read"))],
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
) -> dict[str, Any]:
    set_tenant_context(session, principal.tenant_id)
    workers = list(
        session.scalars(
            select(WorkerDevice)
            .where(WorkerDevice.tenant_id == principal.tenant_id)
            .order_by(WorkerDevice.created_at.desc())
            .limit(limit)
        )
    )
    return {"items": [_serialize_worker(worker) for worker in workers]}


@router.patch("/workers/{worker_id}")
async def update_worker_state(
    worker_id: str,
    body: WorkerStateUpdate,
    request: Request,
    response: Response,
    session: Annotated[Session, Depends(db_session)],
    principal: Annotated[Principal, Depends(require("worker:write"))],
    if_match: Annotated[str | None, Header(alias="If-Match")] = None,
) -> dict[str, Any]:
    set_tenant_context(session, principal.tenant_id)
    worker = session.get(WorkerDevice, worker_id)
    if worker is None or worker.tenant_id != principal.tenant_id:
        raise HTTPException(
            status_code=404,
            detail={"code": "worker_not_found", "message": "Worker was not found"},
        )
    expected = f'W/"{worker.version}"'
    if if_match != expected:
        raise HTTPException(
            status_code=412,
            detail={
                "code": "etag_mismatch",
                "message": "A current If-Match ETag is required",
                "current_etag": expected,
            },
        )
    if worker.status == "revoked" and body.status != "revoked":
        raise HTTPException(
            status_code=409,
            detail={"code": "worker_revoked", "message": "Revoked workers cannot be restored"},
        )
    prior_status = worker.status
    if prior_status != body.status:
        worker.status = body.status
        worker.revoked = body.status == "revoked"
        worker.version += 1
        if body.status == "revoked":
            claimed_jobs = list(
                session.scalars(
                    select(Resource).where(
                        Resource.tenant_id == principal.tenant_id,
                        Resource.resource_type == "worker_job",
                        Resource.status == "claimed",
                    )
                )
            )
            for job in claimed_jobs:
                if job.payload.get("worker_id") != worker.worker_id:
                    continue
                payload = dict(job.payload)
                payload.pop("worker_id", None)
                payload.pop("claimed_at", None)
                payload.pop("lease_expires_at", None)
                job.payload = payload
                job.status = "queued"
                job.version += 1
        service_for(session, principal, request).audit_subject(
            "worker.state_changed",
            "worker_device",
            worker.worker_id,
            {
                "before": prior_status,
                "after": body.status,
                "reason": body.reason,
            },
        )
        session.commit()
        session.refresh(worker)
    response.headers["ETag"] = f'W/"{worker.version}"'
    return _serialize_worker(worker)


async def current_signed_worker(
    request: Request,
    worker: Annotated[WorkerDevice, Depends(current_worker)],
) -> WorkerDevice:
    timestamp_value = request.headers.get("X-PredixaLearn-Device-Timestamp", "")
    signature_value = request.headers.get("X-PredixaLearn-Device-Signature", "")
    try:
        timestamp = int(timestamp_value)
        if abs(int(datetime.now(timezone.utc).timestamp()) - timestamp) > 300:
            raise ValueError("stale request")
        signature = base64.urlsafe_b64decode(
            signature_value + "=" * (-len(signature_value) % 4)
        )
        public_raw = base64.urlsafe_b64decode(
            worker.device_public_key + "=" * (-len(worker.device_public_key) % 4)
        )
        query = f"?{request.url.query}" if request.url.query else ""
        message = (
            f"{timestamp_value}\n{request.method.upper()}\n{request.url.path}{query}"
        ).encode()
        Ed25519PublicKey.from_public_bytes(public_raw).verify(signature, message)
    except (InvalidSignature, TypeError, ValueError) as exc:
        raise HTTPException(
            status_code=401,
            detail={
                "code": "invalid_device_signature",
                "message": "Worker request signature is invalid or expired",
            },
        ) from exc
    return worker


@router.post("/workers/enrollment-tokens", status_code=201)
async def create_enrollment_token(
    body: EnrollmentTokenRequest,
    session: Annotated[Session, Depends(db_session)],
    principal: Annotated[Principal, Depends(require("worker:write"))],
) -> dict[str, Any]:
    set_tenant_context(session, principal.tenant_id)
    if not 1 <= body.expires_minutes <= 60:
        raise HTTPException(status_code=422, detail={"code": "invalid_expiry", "message": "Enrollment token expiry must be 1-60 minutes"})
    token = secrets.token_urlsafe(32)
    expires_at = datetime.now(timezone.utc) + timedelta(minutes=body.expires_minutes)
    session.add(
        WorkerEnrollmentToken(
            token_hash=hashlib.sha256(token.encode()).hexdigest(),
            tenant_id=principal.tenant_id,
            created_by=principal.subject,
            expires_at=expires_at,
        )
    )
    session.commit()
    return {"enrollment_token": token, "expires_at": expires_at.isoformat()}


@router.post("/workers/enroll", status_code=201)
async def enroll_worker(
    body: EnrollmentRedeem,
    session: Annotated[Session, Depends(db_session)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> dict[str, Any]:
    try:
        public_bytes = base64.urlsafe_b64decode(
            body.device_public_key + "=" * (-len(body.device_public_key) % 4)
        )
        Ed25519PublicKey.from_public_bytes(public_bytes)
    except (ValueError, TypeError) as exc:
        raise HTTPException(
            status_code=422,
            detail={
                "code": "invalid_device_public_key",
                "message": "Device public key must be a base64url Ed25519 public key",
            },
        ) from exc
    token_hash = hashlib.sha256(body.enrollment_token.encode()).hexdigest()
    token = session.get(WorkerEnrollmentToken, token_hash)
    now = datetime.now(timezone.utc)
    if token is None or token.used_at is not None or _aware(token.expires_at) < now:
        raise HTTPException(status_code=403, detail={"code": "invalid_enrollment", "message": "Enrollment token is invalid or expired"})
    set_tenant_context(session, token.tenant_id)
    key_hash = hashlib.sha256(public_bytes).hexdigest()
    existing = session.scalar(
        select(WorkerDevice).where(
            WorkerDevice.tenant_id == token.tenant_id,
            WorkerDevice.device_public_key_sha256 == key_hash,
        )
    )
    if existing is not None:
        raise HTTPException(status_code=409, detail={"code": "worker_exists", "message": "This worker is already enrolled"})
    worker = WorkerDevice(
        tenant_id=token.tenant_id,
        name=body.name,
        device_public_key=body.device_public_key,
        device_public_key_sha256=key_hash,
        capabilities=body.capabilities,
    )
    token.used_at = now
    session.add(worker)
    session.commit()
    credential = jwt.encode(
        {
            "sub": worker.worker_id,
            "tenant_id": worker.tenant_id,
            "aud": "predixalearn-worker",
            "iat": now,
            "exp": now + timedelta(days=30),
        },
        _worker_secret(settings),
        algorithm="HS256",
    )
    return {
        "worker_id": worker.worker_id,
        "tenant_id": worker.tenant_id,
        "status": worker.status,
        "credential": credential,
        "server_signing_public_key": _server_public_key(settings),
        "expires_in_days": 30,
    }


@router.post("/worker-jobs/sources", status_code=201)
async def stage_worker_source(
    file: Annotated[UploadFile, File()],
    request: Request,
    session: Annotated[Session, Depends(db_session)],
    principal: Annotated[Principal, Depends(require("worker:write"))],
) -> dict[str, Any]:
    """Stage an institution-approved source for a worker without exposing its storage key."""

    _expire_staged_sources(session, principal.tenant_id)
    media_type = (file.content_type or "application/octet-stream").lower()
    if media_type not in SOURCE_MEDIA_TYPES:
        raise HTTPException(
            status_code=415,
            detail={"code": "unsupported_source", "message": "Worker source must be a supported PDF or image"},
        )
    data = await file.read(MAX_WORKER_SOURCE_BYTES + 1)
    if not data or len(data) > MAX_WORKER_SOURCE_BYTES:
        raise HTTPException(
            status_code=413,
            detail={"code": "source_too_large", "message": "Worker source must be between 1 byte and 100 MiB"},
        )
    if _source_media_type(data) != media_type:
        raise HTTPException(
            status_code=422,
            detail={
                "code": "source_content_invalid",
                "message": "Worker source content does not match its declared type",
            },
        )
    digest = hashlib.sha256(data).hexdigest()
    key = f"{principal.tenant_id}/workers/sources/{digest[:2]}/{digest}"
    stored = create_storage().put(key, data, content_type=media_type)
    descriptor = ArtifactDescriptor(
        kind="source_document",
        sha256=digest,
        size_bytes=len(data),
        media_type=media_type,
        storage_key=str(stored["key"]),
    )
    try:
        service_for(session, principal, request).create(
            "worker_source",
            ResourceCreate(
                title="Staged OCR worker source",
                status="staged",
                access_classification="private",
                content_sha256=digest,
                payload={
                    "storage": stored,
                    "expires_at": (
                        datetime.now(timezone.utc) + timedelta(hours=24)
                    ).isoformat(),
                },
            ),
            immutable=True,
            resource_id=descriptor.artifact_id,
        )
    except Exception:
        create_storage().delete(key)
        raise
    return descriptor.model_dump(mode="json")


def _verified_job_source(
    descriptor: ArtifactDescriptor,
    tenant_id: str,
    session: Session,
    *,
    allow_assigned: bool = False,
) -> tuple[bytes, Resource]:
    if descriptor.kind != "source_document" or not descriptor.storage_key:
        raise HTTPException(
            status_code=422,
            detail={"code": "invalid_source", "message": "Worker job requires a staged source document"},
        )
    key = validate_key(descriptor.storage_key)
    if not key.startswith(f"{tenant_id}/workers/sources/"):
        raise HTTPException(
            status_code=422,
            detail={"code": "invalid_source", "message": "Worker source does not belong to this tenant"},
        )
    source_resource = session.scalar(
        select(Resource).where(
            Resource.tenant_id == tenant_id,
            Resource.resource_type == "worker_source",
            Resource.resource_id == descriptor.artifact_id,
        )
    )
    storage = source_resource.payload.get("storage", {}) if source_resource else {}
    if (
        source_resource is None
        or source_resource.status
        not in ({"staged", "assigned"} if allow_assigned else {"staged"})
        or source_resource.content_sha256 != descriptor.sha256
        or not isinstance(storage, dict)
        or storage.get("key") != key
        or storage.get("size_bytes") != descriptor.size_bytes
        or storage.get("content_type") != descriptor.media_type
    ):
        raise HTTPException(
            status_code=422,
            detail={
                "code": "invalid_source",
                "message": "Worker source staging record is invalid or expired",
            },
        )
    try:
        data = create_storage().read(key)
    except (OSError, ValueError) as exc:
        raise HTTPException(
            status_code=422,
            detail={"code": "source_missing", "message": "Worker source is unavailable"},
        ) from exc
    if len(data) != descriptor.size_bytes or hashlib.sha256(data).hexdigest() != descriptor.sha256:
        raise HTTPException(
            status_code=409,
            detail={"code": "source_corrupt", "message": "Worker source checksum verification failed"},
        )
    return data, source_resource


@router.post("/worker-jobs", status_code=201)
async def create_worker_job(
    body: WorkerJobCreate,
    response: Response,
    request: Request,
    session: Annotated[Session, Depends(db_session)],
    principal: Annotated[Principal, Depends(require("worker:write"))],
) -> dict[str, Any]:
    descriptor = ArtifactDescriptor.model_validate(body.source_artifact)
    _data, source_resource = _verified_job_source(
        descriptor, principal.tenant_id, session
    )
    create = __import__("institution_server.schemas", fromlist=["ResourceCreate"]).ResourceCreate(
        title=f"OCR worker job: {body.workflow}",
        status="queued",
        payload=body.model_dump(),
    )
    resource = service_for(session, principal, request).create("worker_job", create)
    source_payload = dict(source_resource.payload)
    source_payload["assigned_job_id"] = resource.resource_id
    source_resource.payload = source_payload
    source_resource.status = "assigned"
    source_resource.version += 1
    session.commit()
    response.headers["ETag"] = f'W/"{resource.version}"'
    return serialize_resource(resource)


@router.get("/workers/jobs/next", response_model=None)
async def claim_worker_job(
    session: Annotated[Session, Depends(db_session)],
    worker: Annotated[WorkerDevice, Depends(current_signed_worker)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> Response | dict[str, Any]:
    _expire_staged_sources(session, worker.tenant_id)
    if worker.status == "draining":
        return Response(status_code=204, headers={"X-PredixaLearn-Worker-State": "draining"})
    now = datetime.now(timezone.utc)
    stale_claims = list(
        session.scalars(
            select(Resource).where(
                Resource.tenant_id == worker.tenant_id,
                Resource.resource_type == "worker_job",
                Resource.status == "claimed",
            ).limit(100)
        )
    )
    reset_stale = False
    for stale in stale_claims:
        try:
            lease = datetime.fromisoformat(
                str(stale.payload.get("lease_expires_at", "")).replace("Z", "+00:00")
            )
        except ValueError:
            lease = now - timedelta(seconds=1)
        if lease.tzinfo is None or lease <= now:
            stale_payload = dict(stale.payload)
            stale_payload.pop("worker_id", None)
            stale_payload.pop("claimed_at", None)
            stale_payload.pop("lease_expires_at", None)
            stale.payload = stale_payload
            stale.status = "queued"
            stale.version += 1
            reset_stale = True
    if reset_stale:
        session.commit()
    statement = (
        select(Resource)
        .where(
            Resource.tenant_id == worker.tenant_id,
            Resource.resource_type == "worker_job",
            Resource.status == "queued",
        )
        .order_by(Resource.created_at)
        .limit(1)
    )
    if session.bind and session.bind.dialect.name == "postgresql":
        statement = statement.with_for_update(skip_locked=True)
    job = session.scalar(statement)
    if job is None:
        return Response(status_code=204)
    workflow = str(job.payload.get("workflow"))
    supported = worker.capabilities.get("workflows", [])
    if supported and workflow not in supported:
        return Response(status_code=204)
    job.status = "claimed"
    job.version += 1
    expires_at = now + timedelta(hours=2)
    payload = dict(job.payload)
    payload.update(
        {
            "worker_id": worker.worker_id,
            "claimed_at": now.isoformat(),
            "lease_expires_at": expires_at.isoformat(),
        }
    )
    job.payload = payload
    session.commit()
    envelope = WorkerJobEnvelope(
        job_id=job.resource_id,
        tenant_id=job.tenant_id,
        workflow=workflow,
        source=ArtifactDescriptor.model_validate(job.payload["source_artifact"]),
        settings=job.payload.get("settings", {}),
        allowed_outputs=job.payload.get("allowed_outputs", []),
        expires_at=expires_at,
    )
    return _sign_envelope(envelope.model_dump(mode="json"), settings)


@router.get("/workers/jobs/{job_id}/source")
async def download_worker_source(
    job_id: str,
    session: Annotated[Session, Depends(db_session)],
    worker: Annotated[WorkerDevice, Depends(current_signed_worker)],
):
    job = session.scalar(
        select(Resource).where(
            Resource.tenant_id == worker.tenant_id,
            Resource.resource_type == "worker_job",
            Resource.resource_id == job_id,
        )
    )
    if job is None or job.status != "claimed" or job.payload.get("worker_id") != worker.worker_id:
        raise HTTPException(status_code=404, detail={"code": "job_not_found", "message": "Worker job was not found"})
    descriptor = ArtifactDescriptor.model_validate(job.payload["source_artifact"])
    data, source_resource = _verified_job_source(
        descriptor, worker.tenant_id, session, allow_assigned=True
    )
    if source_resource.payload.get("assigned_job_id") != job_id:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "source_assignment_mismatch",
                "message": "Worker source is assigned to a different job",
            },
        )
    return StreamingResponse(
        iter([data]),
        media_type=descriptor.media_type,
        headers={
            "Content-Disposition": f'attachment; filename="source-{job_id}"',
            "X-Content-SHA256": descriptor.sha256,
        },
    )


@router.post("/workers/jobs/{job_id}/artifacts", status_code=201)
async def upload_worker_artifact(
    job_id: str,
    kind: Literal["result_json", "markdown", "docx", "preview", "crop", "table"],
    file: Annotated[UploadFile, File()],
    session: Annotated[Session, Depends(db_session)],
    worker: Annotated[WorkerDevice, Depends(current_signed_worker)],
) -> dict[str, Any]:
    job = session.scalar(
        select(Resource).where(
            Resource.tenant_id == worker.tenant_id,
            Resource.resource_type == "worker_job",
            Resource.resource_id == job_id,
        )
    )
    if job is None or job.status != "claimed" or job.payload.get("worker_id") != worker.worker_id:
        raise HTTPException(status_code=404, detail={"code": "job_not_found", "message": "Worker job was not found"})
    allowed = set(job.payload.get("allowed_outputs", []))
    if kind not in allowed:
        raise HTTPException(status_code=422, detail={"code": "artifact_not_allowed", "message": "Worker output type was not approved"})
    data = await file.read(MAX_WORKER_OUTPUT_BYTES + 1)
    if not data or len(data) > MAX_WORKER_OUTPUT_BYTES:
        raise HTTPException(status_code=413, detail={"code": "artifact_too_large", "message": "Worker output must be between 1 byte and 100 MiB"})
    digest = hashlib.sha256(data).hexdigest()
    key = f"{worker.tenant_id}/workers/jobs/{job_id}/{digest}"
    media_type = file.content_type or "application/octet-stream"
    _validate_worker_output(kind, data, media_type)
    stored = create_storage().put(key, data, content_type=media_type)
    descriptor = ArtifactDescriptor(
        kind=kind,
        sha256=digest,
        size_bytes=len(data),
        media_type=media_type,
        storage_key=str(stored["key"]),
    )
    payload = dict(job.payload)
    uploaded = list(payload.get("uploaded_artifacts", []))
    serialized_descriptor = descriptor.model_dump(mode="json")
    if serialized_descriptor not in uploaded:
        uploaded.append(serialized_descriptor)
    payload["uploaded_artifacts"] = uploaded
    job.payload = payload
    job.version += 1
    session.commit()
    return descriptor.model_dump(mode="json")


@router.post("/workers/jobs/{job_id}/result")
async def publish_worker_result(
    job_id: str,
    body: WorkerResult,
    session: Annotated[Session, Depends(db_session)],
    worker: Annotated[WorkerDevice, Depends(current_signed_worker)],
) -> dict[str, Any]:
    job = session.scalar(
        select(Resource).where(
            Resource.tenant_id == worker.tenant_id,
            Resource.resource_type == "worker_job",
            Resource.resource_id == job_id,
        )
    )
    if job is None or job.payload.get("worker_id") != worker.worker_id:
        raise HTTPException(status_code=404, detail={"code": "job_not_found", "message": "Worker job was not found"})
    if body.status not in {"completed", "failed", "cancelled"}:
        raise HTTPException(status_code=422, detail={"code": "invalid_status", "message": "Worker result status is invalid"})
    if job.status in {"completed", "failed", "cancelled"}:
        return {"accepted": True, "job_id": job_id, "status": job.status}
    if job.status != "claimed":
        raise HTTPException(status_code=409, detail={"code": "job_not_claimed", "message": "Worker job is not active"})
    allowed = set(job.payload.get("allowed_outputs", []))
    uploaded = {
        (item.get("sha256"), item.get("storage_key")): item
        for item in job.payload.get("uploaded_artifacts", [])
        if isinstance(item, dict)
    }
    for artifact in body.artifacts:
        descriptor = ArtifactDescriptor.model_validate(artifact)
        if allowed and descriptor.kind not in allowed:
            raise HTTPException(status_code=422, detail={"code": "artifact_not_allowed", "message": "Worker returned an unapproved artifact type"})
        if (descriptor.sha256, descriptor.storage_key) not in uploaded:
            raise HTTPException(status_code=422, detail={"code": "artifact_not_uploaded", "message": "Worker result references an unknown artifact"})
        if not descriptor.storage_key or not validate_key(descriptor.storage_key).startswith(
            f"{worker.tenant_id}/workers/jobs/{job_id}/"
        ):
            raise HTTPException(status_code=422, detail={"code": "artifact_invalid", "message": "Worker artifact storage scope is invalid"})
        try:
            stored_data = create_storage().read(descriptor.storage_key)
        except (OSError, ValueError) as exc:
            raise HTTPException(status_code=422, detail={"code": "artifact_missing", "message": "Worker artifact is unavailable"}) from exc
        if (
            len(stored_data) != descriptor.size_bytes
            or hashlib.sha256(stored_data).hexdigest() != descriptor.sha256
        ):
            raise HTTPException(status_code=409, detail={"code": "artifact_corrupt", "message": "Worker artifact checksum verification failed"})
    payload = dict(job.payload)
    payload.update(
        {
            "artifacts": body.artifacts,
            "result_summary": body.result_summary,
            "completed_at": datetime.now(timezone.utc).isoformat(),
        }
    )
    job.payload = payload
    job.status = body.status
    job.version += 1
    source = ArtifactDescriptor.model_validate(job.payload["source_artifact"])
    source_resource = session.scalar(
        select(Resource).where(
            Resource.tenant_id == worker.tenant_id,
            Resource.resource_type == "worker_source",
            Resource.resource_id == source.artifact_id,
        )
    )
    if source_resource is not None:
        source_resource.status = "consumed"
        source_resource.version += 1
    session.commit()
    if source.storage_key:
        try:
            create_storage().delete(source.storage_key)
        except Exception:
            logger.exception("Temporary source cleanup failed for worker job %s", job_id)
    return {"accepted": True, "job_id": job_id, "status": body.status}
