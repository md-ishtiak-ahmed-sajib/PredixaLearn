"""OIDC Authorization Code + PKCE flow for the browser portal."""

from __future__ import annotations

import base64
import hashlib
import secrets
from datetime import datetime, timedelta, timezone
from typing import Annotated
from urllib.parse import urlencode

import httpx
import jwt
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import RedirectResponse

from .config import Settings, get_settings

router = APIRouter(prefix="/auth", tags=["identity"])


def _required(settings: Settings, name: str) -> str:
    value = getattr(settings, name)
    if not value:
        raise HTTPException(status_code=503, detail="Institution OIDC login is not configured")
    return str(value)


@router.get("/login")
async def login(settings: Annotated[Settings, Depends(get_settings)]) -> RedirectResponse:
    authorization_endpoint = _required(settings, "oidc_authorization_endpoint")
    client_id = _required(settings, "oidc_client_id")
    secret = _required(settings, "session_secret")
    state = secrets.token_urlsafe(24)
    nonce = secrets.token_urlsafe(24)
    verifier = secrets.token_urlsafe(48)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
    now = datetime.now(timezone.utc)
    flow_cookie = jwt.encode(
        {
            "state": state,
            "nonce": nonce,
            "verifier": verifier,
            "aud": "predixalearn-oidc-flow",
            "iat": now,
            "exp": now + timedelta(minutes=10),
        },
        secret,
        algorithm="HS256",
    )
    query = urlencode(
        {
            "client_id": client_id,
            "response_type": "code",
            "scope": "openid profile email",
            "redirect_uri": f"{settings.public_base_url}/auth/callback",
            "state": state,
            "nonce": nonce,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
        }
    )
    response = RedirectResponse(f"{authorization_endpoint}?{query}", status_code=302)
    response.set_cookie(
        "predixalearn_oidc_flow",
        flow_cookie,
        httponly=True,
        secure=settings.public_base_url.startswith("https://"),
        samesite="lax",
        max_age=600,
    )
    return response


@router.get("/callback")
async def callback(
    request: Request,
    code: str,
    state: str,
    settings: Annotated[Settings, Depends(get_settings)],
) -> RedirectResponse:
    secret = _required(settings, "session_secret")
    try:
        flow = jwt.decode(
            request.cookies.get("predixalearn_oidc_flow", ""),
            secret,
            algorithms=["HS256"],
            audience="predixalearn-oidc-flow",
        )
    except jwt.PyJWTError as exc:
        raise HTTPException(status_code=400, detail="OIDC login flow expired") from exc
    if not secrets.compare_digest(str(flow["state"]), state):
        raise HTTPException(status_code=400, detail="OIDC state is invalid")
    async with httpx.AsyncClient(timeout=20) as client:
        token_response = await client.post(
            _required(settings, "oidc_token_endpoint"),
            data={
                "grant_type": "authorization_code",
                "client_id": _required(settings, "oidc_client_id"),
                "client_secret": _required(settings, "oidc_client_secret"),
                "code": code,
                "redirect_uri": f"{settings.public_base_url}/auth/callback",
                "code_verifier": flow["verifier"],
            },
        )
        token_response.raise_for_status()
    id_token = token_response.json().get("id_token", "")
    try:
        key = jwt.PyJWKClient(_required(settings, "oidc_jwks_url")).get_signing_key_from_jwt(id_token).key
        claims = jwt.decode(
            id_token,
            key,
            algorithms=["RS256", "ES256"],
            audience=_required(settings, "oidc_client_id"),
            issuer=_required(settings, "oidc_issuer"),
            options={"require": ["exp", "iss", "aud", "sub", "nonce"]},
        )
    except jwt.PyJWTError as exc:
        raise HTTPException(status_code=401, detail="OIDC identity token is invalid") from exc
    if claims.get("nonce") != flow["nonce"] or not claims.get("tenant_id"):
        raise HTTPException(status_code=403, detail="OIDC identity is missing institution context")
    now = datetime.now(timezone.utc)
    session_token = jwt.encode(
        {
            "sub": claims["sub"],
            "tenant_id": claims["tenant_id"],
            "roles": claims.get("roles", []),
            "permissions": claims.get("permissions", []),
            "academic_unit_ids": claims.get("academic_unit_ids", []),
            "course_ids": claims.get("course_ids", []),
            "features": claims.get("features", sorted(settings.enabled_features)),
            "csrf": secrets.token_urlsafe(32),
            "aud": "predixalearn-portal",
            "iat": now,
            "exp": now + timedelta(hours=8),
        },
        secret,
        algorithm="HS256",
    )
    response = RedirectResponse("/portal", status_code=303)
    response.delete_cookie("predixalearn_oidc_flow")
    response.set_cookie(
        "predixalearn_session",
        session_token,
        httponly=True,
        secure=settings.public_base_url.startswith("https://"),
        samesite="lax",
        max_age=8 * 60 * 60,
    )
    return response


@router.post("/logout")
async def logout() -> RedirectResponse:
    response = RedirectResponse("/", status_code=303)
    response.delete_cookie("predixalearn_session")
    return response
