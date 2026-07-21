"""Pydantic wire models shared across the local and institutional editions."""

from __future__ import annotations

import secrets
import time
import uuid
from datetime import datetime, timezone
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

PROTOCOL_VERSION = "2026-07-01"


def new_uuid7() -> uuid.UUID:
    """Return an RFC 9562 UUIDv7 without requiring Python 3.14."""

    timestamp_ms = int(time.time() * 1000) & ((1 << 48) - 1)
    random_bits = secrets.randbits(74)
    value = timestamp_ms << 80
    value |= 0x7 << 76
    value |= ((random_bits >> 62) & 0xFFF) << 64
    value |= 0b10 << 62
    value |= random_bits & ((1 << 62) - 1)
    return uuid.UUID(int=value)


class ContractModel(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class EvidenceReference(ContractModel):
    document_id: str = Field(min_length=1, max_length=128)
    evidence_version_id: str = Field(min_length=1, max_length=128)
    page: int | None = Field(default=None, ge=1)
    line_ids: list[str] = Field(default_factory=list, max_length=500)
    geometry: list[float] | None = Field(default=None, min_length=4, max_length=8)
    sha256: str = Field(pattern=r"^[a-f0-9]{64}$")


class ArtifactDescriptor(ContractModel):
    artifact_id: str = Field(default_factory=lambda: str(new_uuid7()))
    kind: Literal[
        "source_document",
        "result_json",
        "markdown",
        "docx",
        "preview",
        "crop",
        "table",
        "dataset_export",
    ]
    sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    size_bytes: int = Field(ge=0)
    media_type: str = Field(min_length=1, max_length=255)
    storage_key: str | None = Field(default=None, max_length=1024)

    @field_validator("storage_key")
    @classmethod
    def reject_storage_traversal(cls, value: str | None) -> str | None:
        if value is None:
            return value
        normalized = value.replace("\\", "/")
        if normalized.startswith("/") or "../" in f"/{normalized}/":
            raise ValueError("storage_key must be a relative object key")
        return normalized


class SyncEvent(ContractModel):
    event_id: str = Field(default_factory=lambda: str(new_uuid7()))
    event_type: Literal[
        "archive.upsert",
        "analysis.upsert",
        "review.upsert",
        "artifact.publish",
        "archive.delete_requested",
    ]
    occurred_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    resource_id: str = Field(min_length=1, max_length=128)
    resource_version: int = Field(ge=1)
    idempotency_key: str = Field(min_length=16, max_length=200)
    payload: dict[str, Any]


class SyncEnvelope(ContractModel):
    protocol_version: Literal[PROTOCOL_VERSION] = PROTOCOL_VERSION
    tenant_id: str = Field(min_length=1, max_length=128)
    device_id: str = Field(min_length=1, max_length=128)
    events: list[SyncEvent] = Field(min_length=1, max_length=100)
    artifacts: list[ArtifactDescriptor] = Field(default_factory=list, max_length=100)


class WorkerJobEnvelope(ContractModel):
    protocol_version: Literal[PROTOCOL_VERSION] = PROTOCOL_VERSION
    job_id: str = Field(default_factory=lambda: str(new_uuid7()))
    tenant_id: str = Field(min_length=1, max_length=128)
    workflow: Literal["text_recognition", "layout_parsing", "table_extraction", "vl_processing"]
    source: ArtifactDescriptor
    settings: dict[str, Any] = Field(default_factory=dict)
    allowed_outputs: list[str] = Field(default_factory=list, max_length=20)
    expires_at: datetime


class WebhookEvent(ContractModel):
    protocol_version: Literal[PROTOCOL_VERSION] = PROTOCOL_VERSION
    event_id: str = Field(default_factory=lambda: str(new_uuid7()))
    tenant_id: str = Field(min_length=1, max_length=128)
    event_type: str = Field(min_length=3, max_length=100)
    occurred_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    resource_id: str = Field(min_length=1, max_length=128)
    payload: dict[str, Any] = Field(default_factory=dict)
