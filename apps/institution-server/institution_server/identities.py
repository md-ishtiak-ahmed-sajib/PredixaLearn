"""Encrypted learner-identity vault kept separate from OCR and answer evidence."""

from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
from typing import Annotated, Any

from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from .api import db_session, service_for
from .config import Settings, get_settings
from .models import LearnerIdentityMapping, Resource
from .schemas import StrictModel
from .security import Principal, require

router = APIRouter(prefix="/api/v2/identity-mappings", tags=["identity-vault"])


class IdentityMappingCreate(StrictModel):
    learner_identifier: str = Field(min_length=1, max_length=1000)
    key_version: str = Field(default="v1", min_length=1, max_length=64)


def _key(settings: Settings) -> bytes:
    value = settings.identity_encryption_key or settings.test_jwt_secret
    if not value:
        raise HTTPException(
            status_code=503,
            detail={
                "code": "identity_vault_unavailable",
                "message": "Learner identity encryption is not configured",
            },
        )
    try:
        decoded = base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))
    except (ValueError, TypeError):
        decoded = b""
    return decoded if len(decoded) == 32 else hashlib.sha256(value.encode()).digest()


def _pseudonym(key: bytes, tenant_id: str, identifier: str) -> str:
    digest = hmac.new(key, f"{tenant_id}\0{identifier}".encode(), hashlib.sha256).digest()
    return "learner-" + base64.b32encode(digest[:12]).decode().rstrip("=").casefold()


def decrypt_identity(mapping: LearnerIdentityMapping, settings: Settings) -> str:
    nonce = base64.urlsafe_b64decode(mapping.nonce + "=" * (-len(mapping.nonce) % 4))
    associated = f"{mapping.tenant_id}\0{mapping.pseudonym}\0{mapping.key_version}".encode()
    encrypted = base64.urlsafe_b64decode(
        mapping.ciphertext + "=" * (-len(mapping.ciphertext) % 4)
    )
    try:
        return AESGCM(_key(settings)).decrypt(nonce, encrypted, associated).decode()
    except (ValueError, UnicodeDecodeError) as exc:
        raise HTTPException(
            status_code=503,
            detail={
                "code": "identity_decryption_failed",
                "message": "Learner identity could not be decrypted with the active key",
            },
        ) from exc


@router.post("", status_code=201)
async def create_identity_mapping(
    body: IdentityMappingCreate,
    request: Request,
    session: Annotated[Session, Depends(db_session)],
    principal: Annotated[Principal, Depends(require("identity:write"))],
    settings: Annotated[Settings, Depends(get_settings)],
) -> dict[str, Any]:
    service = service_for(session, principal, request)
    key = _key(settings)
    pseudonym = _pseudonym(key, principal.tenant_id, body.learner_identifier)
    existing = session.scalar(
        select(LearnerIdentityMapping).where(
            LearnerIdentityMapping.tenant_id == principal.tenant_id,
            LearnerIdentityMapping.pseudonym == pseudonym,
        )
    )
    if existing:
        return {"id": existing.mapping_id, "pseudonym": existing.pseudonym, "created": False}
    nonce = secrets.token_bytes(12)
    associated = f"{principal.tenant_id}\0{pseudonym}\0{body.key_version}".encode()
    ciphertext = AESGCM(key).encrypt(nonce, body.learner_identifier.encode(), associated)
    mapping = LearnerIdentityMapping(
        tenant_id=principal.tenant_id,
        pseudonym=pseudonym,
        ciphertext=base64.urlsafe_b64encode(ciphertext).decode().rstrip("="),
        nonce=base64.urlsafe_b64encode(nonce).decode().rstrip("="),
        key_version=body.key_version,
        created_by=principal.subject,
    )
    session.add(mapping)
    session.flush()
    audit_resource = Resource(
        tenant_id=principal.tenant_id,
        resource_type="identity_mapping_audit",
        resource_id=mapping.mapping_id,
        title="Encrypted learner identity mapping",
        status="active",
        access_classification="private",
        payload={"pseudonym": pseudonym, "key_version": body.key_version},
        immutable=True,
        created_by=principal.subject,
        updated_by=principal.subject,
    )
    session.add(audit_resource)
    session.flush()
    service.audit("identity_mapping.created", audit_resource, {"key_version": body.key_version})
    session.commit()
    return {"id": mapping.mapping_id, "pseudonym": pseudonym, "created": True}


@router.get("/{mapping_id}/resolve")
async def resolve_identity_mapping(
    mapping_id: str,
    request: Request,
    session: Annotated[Session, Depends(db_session)],
    principal: Annotated[Principal, Depends(require("identity:resolve"))],
    settings: Annotated[Settings, Depends(get_settings)],
) -> dict[str, str]:
    service = service_for(session, principal, request)
    mapping = session.scalar(
        select(LearnerIdentityMapping).where(
            LearnerIdentityMapping.tenant_id == principal.tenant_id,
            LearnerIdentityMapping.mapping_id == mapping_id,
        )
    )
    if mapping is None:
        raise HTTPException(
            status_code=404,
            detail={"code": "identity_mapping_not_found", "message": "Identity mapping was not found"},
        )
    audit_resource = service.get("identity_mapping_audit", mapping_id)
    service.audit(
        "identity_mapping.resolved", audit_resource, {"purpose": "authorized resolution"}
    )
    session.commit()
    return {
        "id": mapping.mapping_id,
        "pseudonym": mapping.pseudonym,
        "learner_identifier": decrypt_identity(mapping, settings),
    }
