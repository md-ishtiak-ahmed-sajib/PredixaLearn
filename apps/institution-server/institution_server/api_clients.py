"""Tenant-scoped OAuth2 client-credentials management and token issuance."""

from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
from datetime import datetime, timedelta, timezone
from typing import Annotated, Any

import jwt
from fastapi import APIRouter, Depends, Form, Header, HTTPException, Query, Request, Response
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from predixalearn_protocol import new_uuid7
from sqlalchemy import select
from sqlalchemy.orm import Session

from .api import db_session
from .config import Settings, get_settings
from .database import set_tenant_context
from .models import ApiClient
from .schemas import ApiClientCreate
from .security import Principal, require
from .service import ResourceService

router = APIRouter(prefix="/api/v2", tags=["integrations"])
basic = HTTPBasic(auto_error=False)

MACHINE_PERMISSIONS = frozenset(
    {
        "accessibility:read",
        "answer:read",
        "answer:write",
        "archive:read",
        "archive:write",
        "curriculum:read",
        "curriculum:write",
        "dataset:contribute",
        "dataset:read",
        "integration:read",
        "review:read",
        "review:write",
        "rubric:read",
        "rubric:write",
    }
)
TOKEN_SECONDS = 600


def _urlsafe(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode().rstrip("=")


def _decode_urlsafe(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def _new_client_id(tenant_id: str) -> str:
    return f"cs.{_urlsafe(tenant_id.encode())}.{new_uuid7()}"


def _tenant_from_client_id(client_id: str) -> str:
    try:
        prefix, encoded_tenant, _identifier = client_id.split(".", 2)
        tenant_id = _decode_urlsafe(encoded_tenant).decode()
        if prefix != "cs" or not tenant_id or len(tenant_id) > 128:
            raise ValueError("invalid client id")
        return tenant_id
    except (UnicodeDecodeError, ValueError) as exc:
        raise HTTPException(
            status_code=401,
            detail={"code": "invalid_client", "message": "Client authentication failed"},
            headers={"WWW-Authenticate": "Basic"},
        ) from exc


def _secret_digest(secret: str, salt: bytes) -> str:
    return _urlsafe(
        hashlib.scrypt(
            secret.encode(),
            salt=salt,
            n=2**14,
            r=8,
            p=1,
            dklen=32,
        )
    )


def _new_secret() -> tuple[str, str, str]:
    secret = secrets.token_urlsafe(48)
    salt = secrets.token_bytes(16)
    return secret, _urlsafe(salt), _secret_digest(secret, salt)


def _serialize(client: ApiClient) -> dict[str, Any]:
    return {
        "client_id": client.client_id,
        "name": client.name,
        "permissions": sorted(client.permissions),
        "academic_unit_ids": sorted(client.academic_unit_ids),
        "course_ids": sorted(client.course_ids),
        "enabled_features": sorted(client.enabled_features),
        "active": client.active,
        "version": client.version,
        "etag": f'W/"{client.version}"',
        "expires_at": client.expires_at.isoformat() if client.expires_at else None,
        "last_used_at": client.last_used_at.isoformat() if client.last_used_at else None,
        "created_by": client.created_by,
        "created_at": client.created_at.isoformat(),
        "updated_at": client.updated_at.isoformat(),
    }


def _require_match(client: ApiClient, if_match: str | None) -> None:
    expected = f'W/"{client.version}"'
    if if_match != expected:
        raise HTTPException(
            status_code=412,
            detail={
                "code": "etag_mismatch",
                "message": "A current If-Match ETag is required",
                "current_etag": expected,
            },
        )


def _client_for_tenant(session: Session, principal: Principal, client_id: str) -> ApiClient:
    set_tenant_context(session, principal.tenant_id)
    client = session.get(ApiClient, client_id)
    if client is None or client.tenant_id != principal.tenant_id:
        raise HTTPException(
            status_code=404,
            detail={"code": "not_found", "message": "API client was not found"},
        )
    return client


def _validate_grants(body: ApiClientCreate, settings: Settings) -> tuple[list[str], list[str]]:
    permissions = sorted(set(body.permissions))
    unsupported = set(permissions) - MACHINE_PERMISSIONS
    if unsupported:
        raise HTTPException(
            status_code=422,
            detail={
                "code": "unsafe_client_permission",
                "message": "API clients may receive only approved machine permissions",
                "unsupported": sorted(unsupported),
            },
        )
    features = sorted(set(body.enabled_features) or settings.enabled_features)
    unsupported_features = set(features) - settings.enabled_features
    if unsupported_features:
        raise HTTPException(
            status_code=422,
            detail={
                "code": "feature_disabled",
                "message": "API client requested a disabled rollout feature",
                "unsupported": sorted(unsupported_features),
            },
        )
    return permissions, features


@router.post("/api-clients", status_code=201)
async def create_api_client(
    body: ApiClientCreate,
    request: Request,
    response: Response,
    session: Annotated[Session, Depends(db_session)],
    settings: Annotated[Settings, Depends(get_settings)],
    principal: Annotated[Principal, Depends(require("integration:write"))],
) -> dict[str, Any]:
    permissions, features = _validate_grants(body, settings)
    set_tenant_context(session, principal.tenant_id)
    secret, salt, digest = _new_secret()
    now = datetime.now(timezone.utc)
    client = ApiClient(
        client_id=_new_client_id(principal.tenant_id),
        tenant_id=principal.tenant_id,
        name=body.name,
        secret_salt=salt,
        secret_hash=digest,
        permissions=permissions,
        academic_unit_ids=sorted(set(body.academic_unit_ids)),
        course_ids=sorted(set(body.course_ids)),
        enabled_features=features,
        expires_at=now + timedelta(days=body.expires_days),
        created_by=principal.subject,
    )
    session.add(client)
    session.flush()
    ResourceService(session, principal, str(request.state.request_id)).audit_subject(
        "api_client.created",
        "api_client",
        client.client_id,
        {"permissions": permissions, "expires_at": client.expires_at.isoformat()},
    )
    session.commit()
    session.refresh(client)
    response.headers["ETag"] = f'W/"{client.version}"'
    response.headers["Location"] = f"/api/v2/api-clients/{client.client_id}"
    return {**_serialize(client), "client_secret": secret, "secret_returned_once": True}


@router.get("/api-clients")
async def list_api_clients(
    session: Annotated[Session, Depends(db_session)],
    principal: Annotated[Principal, Depends(require("integration:read"))],
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
    cursor: str | None = None,
) -> dict[str, Any]:
    set_tenant_context(session, principal.tenant_id)
    statement = (
        select(ApiClient)
        .where(ApiClient.tenant_id == principal.tenant_id)
        .order_by(ApiClient.client_id)
        .limit(limit + 1)
    )
    if cursor:
        statement = statement.where(ApiClient.client_id > cursor)
    clients = list(session.scalars(statement))
    page = clients[:limit]
    return {
        "items": [_serialize(item) for item in page],
        "next_cursor": page[-1].client_id if len(clients) > limit and page else None,
    }


@router.post("/api-clients/{client_id}/rotate")
async def rotate_api_client_secret(
    client_id: str,
    request: Request,
    response: Response,
    session: Annotated[Session, Depends(db_session)],
    principal: Annotated[Principal, Depends(require("integration:write"))],
    if_match: Annotated[str | None, Header(alias="If-Match")] = None,
) -> dict[str, Any]:
    client = _client_for_tenant(session, principal, client_id)
    _require_match(client, if_match)
    if not client.active:
        raise HTTPException(
            status_code=409,
            detail={"code": "client_inactive", "message": "Revoked clients cannot rotate"},
        )
    secret, salt, digest = _new_secret()
    client.secret_salt = salt
    client.secret_hash = digest
    client.version += 1
    client.updated_at = datetime.now(timezone.utc)
    ResourceService(session, principal, str(request.state.request_id)).audit_subject(
        "api_client.rotated", "api_client", client.client_id, {"version": client.version}
    )
    session.commit()
    session.refresh(client)
    response.headers["ETag"] = f'W/"{client.version}"'
    return {**_serialize(client), "client_secret": secret, "secret_returned_once": True}


@router.delete("/api-clients/{client_id}")
async def revoke_api_client(
    client_id: str,
    request: Request,
    response: Response,
    session: Annotated[Session, Depends(db_session)],
    principal: Annotated[Principal, Depends(require("integration:write"))],
    if_match: Annotated[str | None, Header(alias="If-Match")] = None,
) -> dict[str, Any]:
    client = _client_for_tenant(session, principal, client_id)
    if client.active:
        _require_match(client, if_match)
        client.active = False
        client.version += 1
        client.updated_at = datetime.now(timezone.utc)
        ResourceService(session, principal, str(request.state.request_id)).audit_subject(
            "api_client.revoked", "api_client", client.client_id, {}
        )
        session.commit()
        session.refresh(client)
    response.headers["ETag"] = f'W/"{client.version}"'
    return _serialize(client)


@router.post("/oauth/token")
async def oauth_client_credentials_token(
    request: Request,
    grant_type: Annotated[str, Form()],
    session: Annotated[Session, Depends(db_session)],
    settings: Annotated[Settings, Depends(get_settings)],
    credentials: Annotated[HTTPBasicCredentials | None, Depends(basic)],
    scope: Annotated[str, Form()] = "",
    client_id: Annotated[str | None, Form()] = None,
    client_secret: Annotated[str | None, Form()] = None,
) -> dict[str, Any]:
    if grant_type != "client_credentials":
        raise HTTPException(
            status_code=400,
            detail={"code": "unsupported_grant_type", "message": "Use client_credentials"},
        )
    supplied_id = credentials.username if credentials else client_id
    supplied_secret = credentials.password if credentials else client_secret
    if not supplied_id or not supplied_secret:
        raise HTTPException(
            status_code=401,
            detail={"code": "invalid_client", "message": "Client authentication failed"},
            headers={"WWW-Authenticate": "Basic"},
        )
    tenant_id = _tenant_from_client_id(supplied_id)
    set_tenant_context(session, tenant_id)
    client = session.get(ApiClient, supplied_id)
    now = datetime.now(timezone.utc)
    if client is None or client.tenant_id != tenant_id or not client.active:
        raise HTTPException(
            status_code=401,
            detail={"code": "invalid_client", "message": "Client authentication failed"},
            headers={"WWW-Authenticate": "Basic"},
        )
    salt = _decode_urlsafe(client.secret_salt)
    if not hmac.compare_digest(client.secret_hash, _secret_digest(supplied_secret, salt)):
        raise HTTPException(
            status_code=401,
            detail={"code": "invalid_client", "message": "Client authentication failed"},
            headers={"WWW-Authenticate": "Basic"},
        )
    if client.expires_at and client.expires_at.replace(tzinfo=timezone.utc) <= now:
        raise HTTPException(
            status_code=401,
            detail={"code": "client_expired", "message": "API client has expired"},
        )
    requested = set(scope.split()) if scope else set(client.permissions)
    if not requested.issubset(set(client.permissions)):
        raise HTTPException(
            status_code=400,
            detail={"code": "invalid_scope", "message": "Requested scope is not granted"},
        )
    signing_secret = settings.api_token_secret or settings.test_jwt_secret
    if not signing_secret:
        raise HTTPException(
            status_code=503,
            detail={"code": "token_service_unavailable", "message": "API token service unavailable"},
        )
    expires_at = now + timedelta(seconds=TOKEN_SECONDS)
    access_token = jwt.encode(
        {
            "sub": f"api-client:{client.client_id}",
            "client_id": client.client_id,
            "tenant_id": client.tenant_id,
            "permissions": sorted(requested),
            "academic_unit_ids": client.academic_unit_ids,
            "course_ids": client.course_ids,
            "features": client.enabled_features,
            "client_version": client.version,
            "token_use": "client_credentials",
            "aud": "predixalearn-institution-api",
            "iat": now,
            "exp": expires_at,
            "jti": str(new_uuid7()),
        },
        signing_secret,
        algorithm="HS256",
    )
    client.last_used_at = now
    machine = Principal(
        subject=f"api-client:{client.client_id}",
        tenant_id=client.tenant_id,
        roles=frozenset(),
        permissions=frozenset(requested),
        academic_unit_ids=frozenset(client.academic_unit_ids),
        course_ids=frozenset(client.course_ids),
        enabled_features=frozenset(client.enabled_features),
    )
    ResourceService(session, machine, str(request.state.request_id)).audit_subject(
        "api_client.token_issued",
        "api_client",
        client.client_id,
        {"scope": sorted(requested), "expires_in": TOKEN_SECONDS},
    )
    session.commit()
    return {
        "access_token": access_token,
        "token_type": "Bearer",
        "expires_in": TOKEN_SECONDS,
        "scope": " ".join(sorted(requested)),
    }
