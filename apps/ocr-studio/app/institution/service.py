"""Explicit local-to-institution enrollment, outbox, and synchronization."""

from __future__ import annotations

import base64
import hashlib
import json
import platform
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import httpx
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)

from app.core.config import Settings, get_settings
from app.core.resource_profiles import profile_allows_workflow
from app.core.serialization import atomic_write_json, json_safe
from app.institution.credentials import (
    CredentialStoreError,
    DeviceCredential,
    load_device_credential,
    save_device_credential,
)
from app.storage.history import HistoryStore, SyncOutboxRecord, get_history_store

MAX_SYNC_PAYLOAD_BYTES = 10 * 1024 * 1024
MAX_APPROVED_PREVIEW_BYTES = 4 * 1024 * 1024
MAX_APPROVED_PREVIEWS = 5


class InstitutionSyncError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class InstitutionConnection:
    base_url: str
    tenant_id: str
    worker_id: str
    enrolled_at: str
    server_signing_public_key: str | None = None


def _connection_path(settings: Settings) -> Path:
    return settings.institution_dir / "connection.json"


def _validate_base_url(value: str) -> str:
    parsed = urlsplit(value.rstrip("/"))
    local_development = parsed.scheme == "http" and parsed.hostname in {"127.0.0.1", "localhost"}
    if parsed.scheme != "https" and not local_development:
        raise InstitutionSyncError("Institution service URL must use HTTPS")
    if not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise InstitutionSyncError("Institution service URL is invalid")
    return value.rstrip("/")


def read_connection(settings: Settings | None = None) -> InstitutionConnection | None:
    selected = settings or get_settings()
    path = _connection_path(selected)
    if not path.is_file():
        return None
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return InstitutionConnection(
            base_url=_validate_base_url(str(value["base_url"])),
            tenant_id=str(value["tenant_id"]),
            worker_id=str(value["worker_id"]),
            enrolled_at=str(value["enrolled_at"]),
            server_signing_public_key=(
                str(value["server_signing_public_key"])
                if value.get("server_signing_public_key")
                else None
            ),
        )
    except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise InstitutionSyncError("Institution connection metadata is corrupted") from exc


async def enroll(base_url: str, enrollment_token: str) -> InstitutionConnection:
    settings = get_settings()
    validated_url = _validate_base_url(base_url)
    private_key = Ed25519PrivateKey.generate()
    private_raw = private_key.private_bytes(
        serialization.Encoding.Raw,
        serialization.PrivateFormat.Raw,
        serialization.NoEncryption(),
    )
    public_raw = private_key.public_key().public_bytes(
        serialization.Encoding.Raw,
        serialization.PublicFormat.Raw,
    )
    workflows = [
        name
        for name in (
            "text_recognition",
            "layout_parsing",
            "table_extraction",
            "vl_processing",
        )
        if profile_allows_workflow(settings.runtime_profile, name)
    ]
    payload = {
        "enrollment_token": enrollment_token,
        "name": f"{platform.node() or 'Windows'} PredixaLearn",
        "device_public_key": base64.urlsafe_b64encode(public_raw).decode().rstrip("="),
        "capabilities": {
            "workflows": workflows,
            "runtime_profile": settings.runtime_profile,
            "platform": platform.system(),
            "architecture": platform.machine(),
        },
    }
    try:
        async with httpx.AsyncClient(timeout=20) as client:
            response = await client.post(f"{validated_url}/api/v2/workers/enroll", json=payload)
            response.raise_for_status()
            result = response.json()
    except (httpx.HTTPError, ValueError) as exc:
        raise InstitutionSyncError("Institution enrollment failed; no local History was uploaded") from exc
    worker_id = str(result.get("worker_id", ""))
    tenant_id = str(result.get("tenant_id", ""))
    credential = str(result.get("credential", ""))
    server_signing_public_key = str(result.get("server_signing_public_key", ""))
    try:
        signing_public_raw = base64.urlsafe_b64decode(
            server_signing_public_key + "=" * (-len(server_signing_public_key) % 4)
        )
        Ed25519PublicKey.from_public_bytes(signing_public_raw)
    except (TypeError, ValueError) as exc:
        raise InstitutionSyncError(
            "Institution enrollment did not provide a valid server signing identity"
        ) from exc
    if not worker_id or not tenant_id or not credential:
        raise InstitutionSyncError("Institution enrollment returned an invalid device credential")
    save_device_credential(
        DeviceCredential(
            worker_id=worker_id,
            credential=credential,
            private_key=base64.urlsafe_b64encode(private_raw).decode().rstrip("="),
        )
    )
    connection = InstitutionConnection(
        base_url=validated_url,
        tenant_id=tenant_id,
        worker_id=worker_id,
        enrolled_at=datetime.now(timezone.utc).isoformat(),
        server_signing_public_key=server_signing_public_key,
    )
    settings.institution_dir.mkdir(parents=True, exist_ok=True)
    atomic_write_json(_connection_path(settings), asdict(connection))
    return connection


def _search_text(result: Any) -> str:
    if isinstance(result, dict):
        for key in ("full_text", "markdown", "text"):
            value = result.get(key)
            if isinstance(value, str) and value.strip():
                return value[:2_000_000]
    if isinstance(result, str):
        return result[:2_000_000]
    return ""


def _analysis_overlay(store: HistoryStore, job_id: str, result_hash: str) -> dict[str, Any] | None:
    root = store.settings.history_dir / "jobs" / job_id / "education"
    path = root / "analysis.json"
    if not path.is_file():
        return None
    try:
        analysis = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, json.JSONDecodeError):
        return None
    if not isinstance(analysis, dict) or analysis.get("ocr_result_sha256") != result_hash:
        return None
    audit_events: list[dict[str, Any]] = []
    audit_path = root / "audit.jsonl"
    if audit_path.is_file():
        try:
            for line in audit_path.read_text(encoding="utf-8").splitlines()[-1000:]:
                value = json.loads(line)
                if isinstance(value, dict):
                    audit_events.append(json_safe(value))
        except (OSError, ValueError, json.JSONDecodeError):
            audit_events = []
    return {"analysis": json_safe(analysis), "audit_events": audit_events}


def _approved_previews(store: HistoryStore, record) -> list[dict[str, Any]]:
    manifest = store.read_manifest(record)
    archive_relative = manifest.get("archive_root") if isinstance(manifest, dict) else None
    if not isinstance(archive_relative, str):
        return []
    pages_root = store._resolve_history_path(archive_relative) / "source-pages"  # noqa: SLF001
    if not pages_root.is_dir():
        return []
    previews: list[dict[str, Any]] = []
    total = 0
    for path in sorted(pages_root.glob("page-*.png"))[:MAX_APPROVED_PREVIEWS]:
        try:
            data = path.read_bytes()
        except OSError as exc:
            raise InstitutionSyncError("An approved preview could not be read") from exc
        if not data.startswith(b"\x89PNG\r\n\x1a\n"):
            continue
        total += len(data)
        if total > MAX_APPROVED_PREVIEW_BYTES:
            raise InstitutionSyncError("Approved previews exceed the 4 MiB sync limit")
        previews.append(
            {
                "filename": path.name,
                "media_type": "image/png",
                "sha256": hashlib.sha256(data).hexdigest(),
                "content_base64": base64.b64encode(data).decode(),
                "explicitly_approved": True,
            }
        )
    return previews


def queue_history(
    job_ids: list[str], *, include_approved_previews: bool = False
) -> list[SyncOutboxRecord]:
    connection = read_connection()
    if connection is None:
        raise InstitutionSyncError("Enroll this device with an institution before selecting History")
    store = get_history_store()
    queued: list[SyncOutboxRecord] = []
    for job_id in list(dict.fromkeys(job_ids)):
        record = store.get(job_id)
        result = store.read_result(record)
        result_json = json.dumps(
            json_safe(result), ensure_ascii=False, sort_keys=True, separators=(",", ":")
        )
        result_hash = hashlib.sha256(result_json.encode()).hexdigest()
        analysis_overlay = _analysis_overlay(store, job_id, result_hash)
        approved_previews = (
            _approved_previews(store, record) if include_approved_previews else []
        )
        selection_hash = hashlib.sha256(
            json.dumps(
                {
                    "result": result_hash,
                    "analysis": analysis_overlay,
                    "preview_hashes": [item["sha256"] for item in approved_previews],
                },
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode()
        ).hexdigest()
        idempotency_key = hashlib.sha256(
            f"archive.upsert\0{connection.tenant_id}\0{job_id}\0{selection_hash}".encode()
        ).hexdigest()
        payload = {
            "protocol_version": "2026-07-01",
            "tenant_id": connection.tenant_id,
            "device_id": connection.worker_id,
            "events": [
                {
                    "event_type": "archive.upsert",
                    "occurred_at": datetime.now(timezone.utc).isoformat(),
                    "resource_id": job_id,
                    "resource_version": 1,
                    "idempotency_key": idempotency_key,
                    "payload": {
                        "title": record.input_name,
                        "summary": record.summary(),
                        "ocr_result_sha256": result_hash,
                        "search_text": _search_text(result),
                        "result": json_safe(result),
                        "analysis_overlay": analysis_overlay,
                        "approved_previews": approved_previews,
                    },
                }
            ],
            "artifacts": [],
        }
        encoded = json.dumps(payload, ensure_ascii=False).encode()
        if len(encoded) > MAX_SYNC_PAYLOAD_BYTES:
            raise InstitutionSyncError(
                f"{record.input_name} exceeds the 10 MiB derived-evidence sync limit"
            )
        queued.append(
            store.queue_institution_sync(
                job_id,
                tenant_id=connection.tenant_id,
                device_id=connection.worker_id,
                payload=payload,
            )
        )
    return queued


async def sync_pending(store: HistoryStore | None = None) -> dict[str, Any]:
    selected_store = store or get_history_store()
    connection = read_connection()
    if connection is None:
        raise InstitutionSyncError("Institution enrollment is unavailable")
    try:
        credential = load_device_credential(connection.worker_id)
    except CredentialStoreError as exc:
        raise InstitutionSyncError(str(exc)) from exc
    completed = 0
    failed = 0
    for event in selected_store.list_sync_outbox(status="pending"):
        selected_store.update_sync_event(event.event_id, status="in_flight")
        try:
            payload = selected_store.read_sync_payload(event)
            canonical = json.dumps(
                payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
            ).encode()
            signed_at = str(int(time.time()))
            private_raw = base64.urlsafe_b64decode(
                credential.private_key + "=" * (-len(credential.private_key) % 4)
            )
            signature = Ed25519PrivateKey.from_private_bytes(private_raw).sign(
                signed_at.encode() + b"." + canonical
            )
            async with httpx.AsyncClient(timeout=30) as client:
                response = await client.post(
                    f"{connection.base_url}/api/v2/sync",
                    headers={
                        "Authorization": f"Bearer {credential.credential}",
                        "Idempotency-Key": str(payload["events"][0]["idempotency_key"]),
                        "Content-Type": "application/json",
                        "X-PredixaLearn-Device-Timestamp": signed_at,
                        "X-PredixaLearn-Device-Signature": base64.urlsafe_b64encode(
                            signature
                        ).decode().rstrip("="),
                    },
                    content=canonical,
                )
                response.raise_for_status()
            selected_store.update_sync_event(event.event_id, status="completed")
            completed += 1
        except (httpx.HTTPError, InstitutionSyncError, ValueError):
            delay = min(3600, 30 * (2 ** min(event.attempt_count, 7)))
            selected_store.update_sync_event(
                event.event_id,
                status="pending",
                error="Institution sync is temporarily unavailable",
                retry_after_seconds=delay,
            )
            failed += 1
    return {"completed": completed, "pending_after_error": failed}
