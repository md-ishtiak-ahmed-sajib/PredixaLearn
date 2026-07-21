"""LTI 1.3 registration, OIDC launch, and teacher-approved AGS return."""

from __future__ import annotations

import os
import secrets
from datetime import datetime, timedelta, timezone
from typing import Annotated, Any
from urllib.parse import urlencode, urlsplit

import httpx
import jwt
from fastapi import APIRouter, Depends, Form, HTTPException, Request, Response
from fastapi.responses import RedirectResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from .api import db_session, service_for
from .config import Settings, get_settings
from .identities import decrypt_identity
from .models import LearnerIdentityMapping, LtiNonce, LtiPlatform, Resource
from .schemas import LtiRegistrationCreate, StrictModel
from .security import Principal, require
from .service import serialize_resource

router = APIRouter(prefix="/api/v2/lti", tags=["lti"])


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


class GradeReturn(StrictModel):
    answer_script_id: str
    lineitem_url: str
    identity_mapping_id: str
    score_maximum: float


class DeepLinkResponse(StrictModel):
    registration_id: str
    data: str = ""
    content_items: list[dict[str, Any]]


def _service_url(registration: LtiPlatform, value: str) -> str:
    parsed = urlsplit(value)
    if parsed.scheme != "https" or not parsed.hostname:
        raise HTTPException(status_code=422, detail={"code": "invalid_lti_service_url", "message": "LTI service URL must use HTTPS"})
    if parsed.hostname.casefold() not in {host.casefold() for host in registration.allowed_service_hosts}:
        raise HTTPException(status_code=403, detail={"code": "lti_service_host_denied", "message": "LTI service host is not approved for this registration"})
    return value


def _registration(session: Session, *, tenant_id: str | None = None, issuer: str | None = None, registration_id: str | None = None) -> LtiPlatform:
    statement = select(LtiPlatform).where(LtiPlatform.active.is_(True))
    if tenant_id:
        statement = statement.where(LtiPlatform.tenant_id == tenant_id)
    if registration_id:
        statement = statement.where(LtiPlatform.registration_id == registration_id)
    candidates = list(session.scalars(statement))
    if issuer:
        candidates = [item for item in candidates if item.issuer == issuer]
    if len(candidates) != 1:
        raise HTTPException(status_code=404, detail={"code": "lti_registration_not_found", "message": "LTI registration was not found"})
    return candidates[0]


@router.post("/registrations", status_code=201)
async def register_lti_platform(
    body: LtiRegistrationCreate,
    response: Response,
    request: Request,
    session: Annotated[Session, Depends(db_session)],
    principal: Annotated[Principal, Depends(require("integration:write"))],
) -> dict[str, Any]:
    resource = service_for(session, principal, request).create(
        "lti_registration", body, immutable=True
    )
    session.add(
        LtiPlatform(
            registration_id=resource.resource_id,
            tenant_id=principal.tenant_id,
            issuer=body.issuer,
            client_id=body.client_id,
            deployment_id=body.deployment_id,
            authorization_endpoint=body.authorization_endpoint,
            token_endpoint=body.token_endpoint,
            jwks_url=body.jwks_url,
            enabled_services=body.enabled_services,
            allowed_service_hosts=body.payload["allowed_service_hosts"],
        )
    )
    session.commit()
    response.headers["ETag"] = f'W/"{resource.version}"'
    return serialize_resource(resource)


@router.get("/login")
async def initiate_lti_login(
    iss: str,
    login_hint: str,
    target_link_uri: str,
    session: Annotated[Session, Depends(db_session)],
    settings: Annotated[Settings, Depends(get_settings)],
    lti_message_hint: str | None = None,
    client_id: str | None = None,
) -> RedirectResponse:
    registration = _registration(session, issuer=iss)
    if client_id and registration.client_id != client_id:
        raise HTTPException(status_code=400, detail={"code": "client_mismatch", "message": "LTI client ID does not match"})
    state = secrets.token_urlsafe(32)
    nonce = secrets.token_urlsafe(32)
    session.add(
        LtiNonce(
            nonce=nonce,
            tenant_id=registration.tenant_id,
            state=state,
            expires_at=datetime.now(timezone.utc) + timedelta(minutes=10),
        )
    )
    session.commit()
    query = {
        "scope": "openid",
        "response_type": "id_token",
        "response_mode": "form_post",
        "prompt": "none",
        "client_id": registration.client_id,
        "redirect_uri": f"{settings.public_base_url}/api/v2/lti/launch",
        "login_hint": login_hint,
        "state": state,
        "nonce": nonce,
    }
    if lti_message_hint:
        query["lti_message_hint"] = lti_message_hint
    if target_link_uri:
        query["target_link_uri"] = target_link_uri
    return RedirectResponse(
        f"{registration.authorization_endpoint}?{urlencode(query)}", status_code=302
    )


@router.post("/launch")
async def receive_lti_launch(
    session: Annotated[Session, Depends(db_session)],
    id_token: Annotated[str, Form()],
    state: Annotated[str, Form()],
) -> dict[str, Any]:
    nonce_record = session.scalar(select(LtiNonce).where(LtiNonce.state == state))
    now = datetime.now(timezone.utc)
    if nonce_record is None or nonce_record.used or _aware(nonce_record.expires_at) < now:
        raise HTTPException(status_code=400, detail={"code": "invalid_lti_state", "message": "LTI launch state is invalid or expired"})
    unverified = jwt.decode(id_token, options={"verify_signature": False})
    registration = _registration(session, tenant_id=nonce_record.tenant_id, issuer=str(unverified.get("iss", "")))
    try:
        key = jwt.PyJWKClient(str(registration.jwks_url)).get_signing_key_from_jwt(id_token).key
        claims = jwt.decode(
            id_token,
            key,
            algorithms=["RS256", "ES256"],
            audience=registration.client_id,
            issuer=registration.issuer,
            options={"require": ["exp", "iat", "iss", "aud", "nonce", "sub"]},
        )
    except jwt.PyJWTError as exc:
        raise HTTPException(status_code=401, detail={"code": "invalid_lti_launch", "message": "LTI launch token is invalid"}) from exc
    if claims.get("nonce") != nonce_record.nonce:
        raise HTTPException(status_code=401, detail={"code": "invalid_lti_nonce", "message": "LTI launch nonce is invalid"})
    deployment = claims.get("https://purl.imsglobal.org/spec/lti/claim/deployment_id")
    if deployment != registration.deployment_id:
        raise HTTPException(status_code=401, detail={"code": "invalid_deployment", "message": "LTI deployment does not match"})
    nonce_record.used = True
    session.commit()
    return {
        "tenant_id": registration.tenant_id,
        "subject": claims["sub"],
        "roles": claims.get("https://purl.imsglobal.org/spec/lti/claim/roles", []),
        "context": claims.get("https://purl.imsglobal.org/spec/lti/claim/context", {}),
        "resource_link": claims.get("https://purl.imsglobal.org/spec/lti/claim/resource_link", {}),
        "services": claims.get("https://purl.imsglobal.org/spec/lti-ags/claim/endpoint", {}),
    }


def _lti_client_assertion(registration: LtiPlatform, settings: Settings) -> str:
    private_key = os.getenv("PREDIXALEARN_LTI_PRIVATE_KEY")
    key_id = os.getenv("PREDIXALEARN_LTI_KEY_ID")
    if not private_key or not key_id:
        raise HTTPException(status_code=503, detail={"code": "lti_signing_unavailable", "message": "LTI signing key is not configured"})
    now = datetime.now(timezone.utc)
    return jwt.encode(
        {
            "iss": registration.client_id,
            "sub": registration.client_id,
            "aud": registration.token_endpoint,
            "iat": now,
            "exp": now + timedelta(minutes=5),
            "jti": secrets.token_urlsafe(24),
        },
        private_key,
        algorithm="RS256",
        headers={"kid": key_id},
    )


async def _lti_access_token(
    client: httpx.AsyncClient,
    registration: LtiPlatform,
    settings: Settings,
    scope: str,
) -> str:
    assertion = _lti_client_assertion(registration, settings)
    response = await client.post(
        registration.token_endpoint,
        data={
            "grant_type": "client_credentials",
            "client_assertion_type": "urn:ietf:params:oauth:client-assertion-type:jwt-bearer",
            "client_assertion": assertion,
            "scope": scope,
        },
    )
    response.raise_for_status()
    token = str(response.json().get("access_token", ""))
    if not token:
        raise HTTPException(status_code=502, detail={"code": "lti_token_invalid", "message": "LMS returned no service token"})
    return token


@router.get("/roster")
async def read_lti_roster(
    registration_id: str,
    memberships_url: str,
    session: Annotated[Session, Depends(db_session)],
    principal: Annotated[Principal, Depends(require("integration:read"))],
    settings: Annotated[Settings, Depends(get_settings)],
) -> dict[str, Any]:
    registration = _registration(
        session, tenant_id=principal.tenant_id, registration_id=registration_id
    )
    if "nrps" not in registration.enabled_services:
        raise HTTPException(status_code=409, detail={"code": "nrps_disabled", "message": "NRPS is not enabled for this registration"})
    url = _service_url(registration, memberships_url)
    async with httpx.AsyncClient(timeout=20) as client:
        access_token = await _lti_access_token(
            client,
            registration,
            settings,
            "https://purl.imsglobal.org/spec/lti-nrps/scope/contextmembership.readonly",
        )
        response = await client.get(url, headers={"Authorization": f"Bearer {access_token}"})
        response.raise_for_status()
    payload = response.json()
    return {
        "context": payload.get("context", {}),
        "members": payload.get("members", []),
        "learner_identifiers_persisted": False,
    }


@router.post("/deep-links/response")
async def create_deep_link_response(
    body: DeepLinkResponse,
    session: Annotated[Session, Depends(db_session)],
    principal: Annotated[Principal, Depends(require("integration:write"))],
    settings: Annotated[Settings, Depends(get_settings)],
) -> dict[str, str]:
    registration = _registration(
        session, tenant_id=principal.tenant_id, registration_id=body.registration_id
    )
    if "deep_linking" not in registration.enabled_services:
        raise HTTPException(status_code=409, detail={"code": "deep_linking_disabled", "message": "Deep Linking is not enabled for this registration"})
    private_key = os.getenv("PREDIXALEARN_LTI_PRIVATE_KEY")
    key_id = os.getenv("PREDIXALEARN_LTI_KEY_ID")
    if not private_key or not key_id:
        raise HTTPException(status_code=503, detail={"code": "lti_signing_unavailable", "message": "LTI signing key is not configured"})
    now = datetime.now(timezone.utc)
    token = jwt.encode(
        {
            "iss": registration.client_id,
            "aud": registration.issuer,
            "iat": now,
            "exp": now + timedelta(minutes=5),
            "nonce": secrets.token_urlsafe(24),
            "https://purl.imsglobal.org/spec/lti/claim/deployment_id": registration.deployment_id,
            "https://purl.imsglobal.org/spec/lti/claim/message_type": "LtiDeepLinkingResponse",
            "https://purl.imsglobal.org/spec/lti/claim/version": "1.3.0",
            "https://purl.imsglobal.org/spec/lti-dl/claim/data": body.data,
            "https://purl.imsglobal.org/spec/lti-dl/claim/content_items": body.content_items,
        },
        private_key,
        algorithm="RS256",
        headers={"kid": key_id},
    )
    return {"JWT": token}


@router.post("/grades/return")
async def return_teacher_approved_grade(
    body: GradeReturn,
    request: Request,
    session: Annotated[Session, Depends(db_session)],
    principal: Annotated[Principal, Depends(require("integration:write"))],
    settings: Annotated[Settings, Depends(get_settings)],
) -> dict[str, Any]:
    service = service_for(session, principal, request)
    statement = service._query("answer_script").where(  # noqa: SLF001
        Resource.resource_id == body.answer_script_id
    )
    if session.bind and session.bind.dialect.name == "postgresql":
        statement = statement.with_for_update()
    script = session.scalar(statement)
    if script is None:
        raise HTTPException(
            status_code=404,
            detail={"code": "not_found", "message": "Answer script was not found"},
        )
    if script.status != "approved" or not script.payload.get("approved_for_lms"):
        raise HTTPException(status_code=409, detail={"code": "teacher_approval_required", "message": "A teacher must approve this grade for LMS return"})
    if script.payload.get("grade_published"):
        return {"published": True, "idempotent": True, "answer_script_id": script.resource_id}
    registration_id = script.payload.get("lti_registration_id")
    if not registration_id:
        raise HTTPException(status_code=409, detail={"code": "lti_context_missing", "message": "Answer script has no LTI registration context"})
    registration = _registration(session, tenant_id=principal.tenant_id, registration_id=str(registration_id))
    identity_mapping = session.scalar(
        select(LearnerIdentityMapping).where(
            LearnerIdentityMapping.tenant_id == principal.tenant_id,
            LearnerIdentityMapping.mapping_id == body.identity_mapping_id,
        )
    )
    if identity_mapping is None:
        raise HTTPException(
            status_code=404,
            detail={
                "code": "identity_mapping_not_found",
                "message": "Learner identity mapping was not found",
            },
        )
    learner_identifier = decrypt_identity(identity_mapping, settings)
    async with httpx.AsyncClient(timeout=20) as client:
        access_token = await _lti_access_token(
            client,
            registration,
            settings,
            "https://purl.imsglobal.org/spec/lti-ags/scope/score",
        )
        score_url = _service_url(registration, f"{body.lineitem_url.rstrip('/')}/scores")
        grade_response = await client.post(
            score_url,
            headers={"Authorization": f"Bearer {access_token}"},
            json={
                "userId": learner_identifier,
                "scoreGiven": script.payload["teacher_mark"],
                "scoreMaximum": body.score_maximum,
                "activityProgress": "Completed",
                "gradingProgress": "FullyGraded",
                "timestamp": datetime.now(timezone.utc).isoformat(),
            },
        )
        grade_response.raise_for_status()
    payload = dict(script.payload)
    payload.update({"grade_published": True, "grade_published_at": datetime.now(timezone.utc).isoformat(), "grade_published_by": principal.subject})
    script.payload = payload
    script.version += 1
    identity_audit = service.get("identity_mapping_audit", identity_mapping.mapping_id)
    service.audit(
        "identity_mapping.resolved",
        identity_audit,
        {"purpose": "teacher-approved LTI grade return"},
    )
    service.audit("lti.grade_published", script, {"lineitem_host": httpx.URL(body.lineitem_url).host})
    session.commit()
    return {"published": True, "idempotent": False, "answer_script_id": script.resource_id}
