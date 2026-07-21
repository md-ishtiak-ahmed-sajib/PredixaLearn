"""Validated institution-service configuration."""

from __future__ import annotations

import os
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from urllib.parse import urlsplit

ALL_FEATURES = frozenset(
    {
        "archive_review",
        "curriculum",
        "answer_scripts",
        "accessibility",
        "datasets",
        "workers",
        "integrations",
        "lti",
        "teaching_workspace",
    }
)


def _bool(name: str, default: bool = False) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    if value.casefold() in {"1", "true", "yes", "on"}:
        return True
    if value.casefold() in {"0", "false", "no", "off"}:
        return False
    raise ValueError(f"{name} must be a boolean")


def _choice(name: str, default: str, values: set[str]) -> str:
    value = os.getenv(name, default).strip().casefold()
    if value not in values:
        raise ValueError(f"{name} must be one of: {', '.join(sorted(values))}")
    return value


@dataclass(frozen=True, slots=True)
class Settings:
    environment: str
    database_url: str
    redis_url: str | None
    auth_mode: str
    oidc_issuer: str | None
    oidc_audience: str | None
    oidc_jwks_url: str | None
    oidc_authorization_endpoint: str | None
    oidc_token_endpoint: str | None
    oidc_client_id: str | None
    oidc_client_secret: str | None
    session_secret: str | None
    test_jwt_secret: str | None
    storage_provider: str
    storage_root: Path
    s3_bucket: str | None
    s3_endpoint_url: str | None
    azure_container: str | None
    semantic_search_enabled: bool
    external_ai_enabled: bool
    accessibility_ai_endpoint: str | None
    accessibility_ai_token: str | None
    public_base_url: str
    worker_signing_key: str | None
    webhook_signing_secret: str | None
    api_token_secret: str | None
    enabled_features: frozenset[str]
    identity_encryption_key: str | None

    @classmethod
    def from_env(cls) -> "Settings":
        environment = _choice(
            "PREDIXALEARN_INSTITUTION_ENV", "development", {"development", "test", "production"}
        )
        database_url = os.getenv(
            "PREDIXALEARN_DATABASE_URL", "sqlite:///./predixalearn-institution.sqlite3"
        ).strip()
        auth_mode = _choice("PREDIXALEARN_AUTH_MODE", "test", {"test", "oidc"})
        storage_provider = _choice(
            "PREDIXALEARN_STORAGE_PROVIDER", "local", {"local", "s3", "azure"}
        )
        public_base_url = os.getenv(
            "PREDIXALEARN_PUBLIC_BASE_URL", "http://127.0.0.1:8100"
        ).rstrip("/")
        parsed = urlsplit(public_base_url)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise ValueError("PREDIXALEARN_PUBLIC_BASE_URL must be an absolute HTTP(S) URL")
        feature_value = os.getenv("PREDIXALEARN_FEATURES", ",".join(sorted(ALL_FEATURES)))
        enabled_features = frozenset(
            item.strip().casefold() for item in feature_value.split(",") if item.strip()
        )
        unknown_features = enabled_features - ALL_FEATURES
        if unknown_features:
            raise ValueError(
                "PREDIXALEARN_FEATURES contains unknown values: "
                + ", ".join(sorted(unknown_features))
            )
        settings = cls(
            environment=environment,
            database_url=database_url,
            redis_url=os.getenv("PREDIXALEARN_REDIS_URL") or None,
            auth_mode=auth_mode,
            oidc_issuer=os.getenv("PREDIXALEARN_OIDC_ISSUER") or None,
            oidc_audience=os.getenv("PREDIXALEARN_OIDC_AUDIENCE") or None,
            oidc_jwks_url=os.getenv("PREDIXALEARN_OIDC_JWKS_URL") or None,
            oidc_authorization_endpoint=os.getenv("PREDIXALEARN_OIDC_AUTHORIZATION_ENDPOINT")
            or None,
            oidc_token_endpoint=os.getenv("PREDIXALEARN_OIDC_TOKEN_ENDPOINT") or None,
            oidc_client_id=os.getenv("PREDIXALEARN_OIDC_CLIENT_ID") or None,
            oidc_client_secret=os.getenv("PREDIXALEARN_OIDC_CLIENT_SECRET") or None,
            session_secret=os.getenv("PREDIXALEARN_SESSION_SECRET") or None,
            test_jwt_secret=os.getenv("PREDIXALEARN_TEST_JWT_SECRET", "development-only-secret")
            or None,
            storage_provider=storage_provider,
            storage_root=Path(
                os.getenv("PREDIXALEARN_STORAGE_ROOT", "./institution-storage")
            ).resolve(),
            s3_bucket=os.getenv("PREDIXALEARN_S3_BUCKET") or None,
            s3_endpoint_url=os.getenv("PREDIXALEARN_S3_ENDPOINT_URL") or None,
            azure_container=os.getenv("PREDIXALEARN_AZURE_CONTAINER") or None,
            semantic_search_enabled=_bool("PREDIXALEARN_SEMANTIC_SEARCH", False),
            external_ai_enabled=_bool("PREDIXALEARN_EXTERNAL_AI", False),
            accessibility_ai_endpoint=os.getenv("PREDIXALEARN_ACCESSIBILITY_AI_ENDPOINT") or None,
            accessibility_ai_token=os.getenv("PREDIXALEARN_ACCESSIBILITY_AI_TOKEN") or None,
            public_base_url=public_base_url,
            worker_signing_key=os.getenv("PREDIXALEARN_WORKER_SIGNING_KEY") or None,
            webhook_signing_secret=os.getenv("PREDIXALEARN_WEBHOOK_SIGNING_SECRET") or None,
            api_token_secret=os.getenv("PREDIXALEARN_API_TOKEN_SECRET") or None,
            enabled_features=enabled_features,
            identity_encryption_key=os.getenv("PREDIXALEARN_IDENTITY_ENCRYPTION_KEY") or None,
        )
        settings.validate()
        return settings

    def validate(self) -> None:
        if self.auth_mode == "oidc" and not all(
            (self.oidc_issuer, self.oidc_audience, self.oidc_jwks_url)
        ):
            raise ValueError("OIDC mode requires issuer, audience, and JWKS URL")
        if self.storage_provider == "s3" and not self.s3_bucket:
            raise ValueError("S3 storage requires PREDIXALEARN_S3_BUCKET")
        if self.storage_provider == "azure" and not self.azure_container:
            raise ValueError("Azure storage requires PREDIXALEARN_AZURE_CONTAINER")
        if self.environment == "production":
            if not self.database_url.startswith(("postgresql://", "postgresql+psycopg://")):
                raise ValueError("Production requires PostgreSQL")
            if not self.redis_url:
                raise ValueError("Production requires Redis for event and worker wake-ups")
            if self.auth_mode != "oidc":
                raise ValueError("Production requires OIDC authentication")
            if self.storage_provider == "local":
                raise ValueError("Production requires S3-compatible or Azure Blob storage")
            if not all(
                (
                    self.worker_signing_key,
                    self.webhook_signing_secret,
                    self.api_token_secret,
                )
            ):
                raise ValueError(
                    "Production requires worker, webhook, and API-token signing secrets"
                )
            if not self.identity_encryption_key:
                raise ValueError("Production requires separate learner-identity encryption")
            if not all(
                (
                    self.oidc_authorization_endpoint,
                    self.oidc_token_endpoint,
                    self.oidc_client_id,
                    self.oidc_client_secret,
                    self.session_secret,
                )
            ):
                raise ValueError("Production teacher portal requires OIDC client and session settings")
            if not self.public_base_url.startswith("https://"):
                raise ValueError("Production public URL must use HTTPS")
        if self.external_ai_enabled:
            endpoint = urlsplit(self.accessibility_ai_endpoint or "")
            if endpoint.scheme != "https" or not endpoint.hostname or not self.accessibility_ai_token:
                raise ValueError(
                    "External accessibility AI requires an HTTPS endpoint and protected token"
                )


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings.from_env()
