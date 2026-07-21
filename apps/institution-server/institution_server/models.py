"""Institutional persistence models.

The resource envelope deliberately keeps evidence payloads versioned while
tenant, access, status, and search fields stay first-class and enforceable.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from predixalearn_protocol import new_uuid7
from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from .database import Base


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


class Resource(Base):
    __tablename__ = "resources"
    __table_args__ = (
        UniqueConstraint("tenant_id", "resource_type", "resource_id"),
        Index("ix_resource_tenant_type_updated", "tenant_id", "resource_type", "updated_at"),
        Index("ix_resource_tenant_course", "tenant_id", "course_id"),
    )

    row_id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    tenant_id: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    resource_type: Mapped[str] = mapped_column(String(64), nullable=False)
    resource_id: Mapped[str] = mapped_column(
        String(128), nullable=False, default=lambda: str(new_uuid7())
    )
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    title: Mapped[str] = mapped_column(String(500), nullable=False)
    status: Mapped[str] = mapped_column(String(64), nullable=False, default="draft")
    parent_id: Mapped[str | None] = mapped_column(String(128))
    academic_unit_id: Mapped[str | None] = mapped_column(String(128))
    course_id: Mapped[str | None] = mapped_column(String(128))
    access_classification: Mapped[str] = mapped_column(
        String(32), nullable=False, default="institution"
    )
    content_sha256: Mapped[str | None] = mapped_column(String(64))
    search_text: Mapped[str] = mapped_column(Text, nullable=False, default="")
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    immutable: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    created_by: Mapped[str] = mapped_column(String(255), nullable=False)
    updated_by: Mapped[str] = mapped_column(String(255), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=now_utc, onupdate=now_utc
    )


class AuditEvent(Base):
    __tablename__ = "audit_events"
    __table_args__ = (Index("ix_audit_tenant_created", "tenant_id", "created_at"),)

    sequence: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    event_id: Mapped[str] = mapped_column(
        String(128), unique=True, nullable=False, default=lambda: str(new_uuid7())
    )
    tenant_id: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    actor_id: Mapped[str] = mapped_column(String(255), nullable=False)
    action: Mapped[str] = mapped_column(String(100), nullable=False)
    resource_type: Mapped[str] = mapped_column(String(64), nullable=False)
    resource_id: Mapped[str] = mapped_column(String(128), nullable=False)
    request_id: Mapped[str] = mapped_column(String(128), nullable=False)
    details: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    previous_hash: Mapped[str | None] = mapped_column(String(64))
    event_hash: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc)


class IdempotencyRecord(Base):
    __tablename__ = "idempotency_records"
    __table_args__ = (UniqueConstraint("tenant_id", "endpoint", "idempotency_key"),)

    row_id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    tenant_id: Mapped[str] = mapped_column(String(128), nullable=False)
    endpoint: Mapped[str] = mapped_column(String(255), nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(200), nullable=False)
    request_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    response_status: Mapped[int] = mapped_column(Integer, nullable=False)
    response_body: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc)


class LtiNonce(Base):
    __tablename__ = "lti_nonces"

    nonce: Mapped[str] = mapped_column(String(255), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(128), nullable=False)
    state: Mapped[str] = mapped_column(String(255), nullable=False, unique=True)
    used: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class WebhookDelivery(Base):
    __tablename__ = "webhook_deliveries"

    delivery_id: Mapped[str] = mapped_column(
        String(128), primary_key=True, default=lambda: str(new_uuid7())
    )
    tenant_id: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    subscription_id: Mapped[str] = mapped_column(String(128), nullable=False)
    event_id: Mapped[str] = mapped_column(String(128), nullable=False)
    attempt: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="pending")
    last_error: Mapped[str | None] = mapped_column(String(500))
    next_attempt_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc)


class WorkerEnrollmentToken(Base):
    __tablename__ = "worker_enrollment_tokens"

    token_hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    created_by: Mapped[str] = mapped_column(String(255), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class WorkerDevice(Base):
    __tablename__ = "worker_devices"
    __table_args__ = (UniqueConstraint("tenant_id", "device_public_key_sha256"),)

    worker_id: Mapped[str] = mapped_column(
        String(128), primary_key=True, default=lambda: str(new_uuid7())
    )
    tenant_id: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    device_public_key: Mapped[str] = mapped_column(Text, nullable=False)
    device_public_key_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    capabilities: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="active")
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    revoked: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    last_seen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc)


class ApiClient(Base):
    """Tenant-owned OAuth2 client whose plaintext secret is never persisted."""

    __tablename__ = "api_clients"
    __table_args__ = (Index("ix_api_client_tenant_active", "tenant_id", "active"),)

    client_id: Mapped[str] = mapped_column(String(300), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    secret_salt: Mapped[str] = mapped_column(String(64), nullable=False)
    secret_hash: Mapped[str] = mapped_column(String(128), nullable=False)
    permissions: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    academic_unit_ids: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    course_ids: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    enabled_features: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_by: Mapped[str] = mapped_column(String(255), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=now_utc, onupdate=now_utc
    )


class LtiPlatform(Base):
    """Non-secret discovery data needed before an LTI tenant session exists."""

    __tablename__ = "lti_platforms"
    __table_args__ = (UniqueConstraint("issuer", "client_id"),)

    registration_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    issuer: Mapped[str] = mapped_column(String(1000), nullable=False)
    client_id: Mapped[str] = mapped_column(String(500), nullable=False)
    deployment_id: Mapped[str] = mapped_column(String(500), nullable=False)
    authorization_endpoint: Mapped[str] = mapped_column(String(2000), nullable=False)
    token_endpoint: Mapped[str] = mapped_column(String(2000), nullable=False)
    jwks_url: Mapped[str] = mapped_column(String(2000), nullable=False)
    enabled_services: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    allowed_service_hosts: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)


class LearnerIdentityMapping(Base):
    """Separately encrypted learner identity; never included in searchable resources."""

    __tablename__ = "learner_identity_mappings"
    __table_args__ = (UniqueConstraint("tenant_id", "pseudonym"),)

    mapping_id: Mapped[str] = mapped_column(
        String(128), primary_key=True, default=lambda: str(new_uuid7())
    )
    tenant_id: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    pseudonym: Mapped[str] = mapped_column(String(80), nullable=False)
    ciphertext: Mapped[str] = mapped_column(Text, nullable=False)
    nonce: Mapped[str] = mapped_column(String(64), nullable=False)
    key_version: Mapped[str] = mapped_column(String(64), nullable=False, default="v1")
    created_by: Mapped[str] = mapped_column(String(255), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc)
