"""OIDC authentication and scoped institutional authorization."""

from __future__ import annotations

import secrets
from dataclasses import dataclass
from typing import Annotated

import jwt
from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from .config import Settings, get_settings

bearer = HTTPBearer(auto_error=False)

ROLE_PERMISSIONS: dict[str, set[str]] = {
    "institution_admin": {"*"},
    "curriculum_manager": {
        "archive:read",
        "curriculum:read",
        "curriculum:write",
        "rubric:read",
        "rubric:write",
        "review:read",
        "review:write",
        "teaching:read",
        "teaching:write",
        "revision:read",
        "revision:write",
    },
    "teacher": {
        "archive:read",
        "archive:write",
        "curriculum:read",
        "rubric:read",
        "review:read",
        "review:write",
        "answer:read",
        "answer:write",
        "accessibility:read",
        "dataset:read",
        "dataset:contribute",
        "teaching:read",
        "teaching:write",
        "revision:read",
        "revision:write",
    },
    "reviewer": {
        "archive:read",
        "curriculum:read",
        "rubric:read",
        "review:read",
        "review:write",
        "answer:read",
        "answer:moderate",
        "teaching:read",
        "teaching:write",
        "revision:read",
    },
    "accessibility_reviewer": {
        "archive:read",
        "accessibility:read",
        "accessibility:verify",
    },
    "data_steward": {
        "archive:read",
        "dataset:read",
        "dataset:write",
        "dataset:release",
        "audit:read",
        "identity:write",
        "identity:resolve",
    },
    "integration_admin": {
        "integration:read",
        "integration:write",
        "worker:read",
        "worker:write",
        "audit:read",
        "identity:resolve",
    },
    "student": {
        "answer:read:self",
        "archive:read:assigned",
        "revision:read",
        "revision:progress",
    },
    "worker": {"worker:claim", "worker:publish"},
}


@dataclass(frozen=True, slots=True)
class Principal:
    subject: str
    tenant_id: str
    roles: frozenset[str]
    permissions: frozenset[str]
    academic_unit_ids: frozenset[str]
    course_ids: frozenset[str]
    enabled_features: frozenset[str]
    csrf_token: str | None = None
    learner_id: str | None = None

    def allows(self, permission: str) -> bool:
        return "*" in self.permissions or permission in self.permissions

    def can_access_scope(self, academic_unit_id: str | None, course_id: str | None) -> bool:
        if "*" in self.permissions:
            return True
        if self.course_ids and course_id and course_id not in self.course_ids:
            return False
        if self.academic_unit_ids and academic_unit_id and academic_unit_id not in self.academic_unit_ids:
            return False
        return True


def _decode_token(token: str, settings: Settings) -> dict:
    try:
        unverified = jwt.decode(
            token,
            options={"verify_signature": False},
            algorithms=["HS256", "RS256", "ES256"],
        )
        if unverified.get("token_use") == "client_credentials":
            secret = settings.api_token_secret or settings.test_jwt_secret
            if not secret:
                raise jwt.InvalidTokenError("API token signing is unavailable")
            return jwt.decode(
                token,
                secret,
                algorithms=["HS256"],
                audience="predixalearn-institution-api",
                options={"require": ["exp", "sub", "tenant_id", "token_use"]},
            )
        if settings.auth_mode == "test":
            return jwt.decode(
                token,
                settings.test_jwt_secret,
                algorithms=["HS256"],
                audience="predixalearn-institution",
                options={"require": ["exp", "sub", "tenant_id"]},
            )
        client = jwt.PyJWKClient(str(settings.oidc_jwks_url), cache_jwk_set=True)
        key = client.get_signing_key_from_jwt(token).key
        return jwt.decode(
            token,
            key,
            algorithms=["RS256", "ES256"],
            audience=settings.oidc_audience,
            issuer=settings.oidc_issuer,
            options={"require": ["exp", "iss", "aud", "sub"]},
        )
    except jwt.PyJWTError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={"code": "invalid_token", "message": "Authentication token is invalid"},
        ) from exc


def _string_set(value: object) -> frozenset[str]:
    if isinstance(value, str):
        return frozenset(item for item in value.split() if item)
    if isinstance(value, list):
        return frozenset(str(item) for item in value if str(item).strip())
    return frozenset()


async def current_principal(
    request: Request,
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> Principal:
    token = credentials.credentials if credentials else request.cookies.get("predixalearn_session")
    if not token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={"code": "authentication_required", "message": "Institution SSO is required"},
        )
    if credentials is None:
        try:
            claims = jwt.decode(
                token,
                settings.session_secret or settings.test_jwt_secret,
                algorithms=["HS256"],
                audience="predixalearn-portal",
                options={"require": ["exp", "sub", "tenant_id"]},
            )
        except jwt.PyJWTError as exc:
            raise HTTPException(
                status_code=401,
                detail={"code": "invalid_session", "message": "Portal session is invalid"},
            ) from exc
    else:
        claims = _decode_token(token, settings)
    csrf_token = str(claims.get("csrf", "")) if credentials is None else None
    if credentials is None and request.method in {"POST", "PUT", "PATCH", "DELETE"}:
        supplied_csrf = request.headers.get("X-CSRF-Token", "")
        if not csrf_token or not secrets.compare_digest(csrf_token, supplied_csrf):
            raise HTTPException(
                status_code=403,
                detail={"code": "csrf_failed", "message": "Portal request token is invalid"},
            )
    roles = _string_set(claims.get("roles"))
    permissions = set(_string_set(claims.get("permissions")))
    for role in roles:
        permissions.update(ROLE_PERMISSIONS.get(role, set()))
    tenant_id = str(claims.get("tenant_id", "")).strip()
    if not tenant_id:
        raise HTTPException(
            status_code=403,
            detail={"code": "tenant_required", "message": "Token has no institution tenant"},
        )
    request.state.tenant_id = tenant_id
    request.state.actor_id = str(claims["sub"])
    claimed_features = (
        _string_set(claims.get("features"))
        if "features" in claims
        else settings.enabled_features
    )
    enabled_features = settings.enabled_features & claimed_features
    feature_paths = {
        "archive_review": ("/api/v2/archive", "/api/v2/search", "/api/v2/reviews"),
        "curriculum": ("/api/v2/curricula", "/api/v2/rubrics", "/api/v2/objective"),
        "answer_scripts": ("/api/v2/answer-", "/api/v2/feedback", "/api/v2/moderation"),
        "accessibility": ("/api/v2/figures", "/api/v2/accessibility"),
        "datasets": ("/api/v2/datasets", "/api/v2/dataset-"),
        "workers": ("/api/v2/workers", "/api/v2/worker-jobs", "/api/v2/sync"),
        "integrations": ("/api/v2/webhooks",),
        "lti": ("/api/v2/lti",),
        "teaching_workspace": ("/api/v2/teacher", "/api/v2/student", "/api/v2/question-bank", "/api/v2/revision-packs", "/api/v2/revision-progress", "/api/v2/corrections", "/api/v2/taxonomies", "/api/v2/syllabi", "/api/v2/comparisons"),
    }
    for feature, prefixes in feature_paths.items():
        if request.url.path.startswith(prefixes) and feature not in enabled_features:
            raise HTTPException(
                status_code=404,
                detail={
                    "code": "feature_disabled",
                    "message": "This feature is not enabled for the institution rollout ring",
                },
            )
    return Principal(
        subject=str(claims["sub"]),
        tenant_id=tenant_id,
        roles=roles,
        permissions=frozenset(permissions),
        academic_unit_ids=_string_set(claims.get("academic_unit_ids")),
        course_ids=_string_set(claims.get("course_ids")),
        enabled_features=enabled_features,
        csrf_token=csrf_token,
        learner_id=str(claims["learner_id"]) if claims.get("learner_id") else None,
    )


def require(permission: str):
    async def dependency(
        principal: Annotated[Principal, Depends(current_principal)],
    ) -> Principal:
        if not principal.allows(permission):
            raise HTTPException(
                status_code=403,
                detail={
                    "code": "permission_denied",
                    "message": f"Permission {permission} is required",
                },
            )
        return principal

    return dependency
