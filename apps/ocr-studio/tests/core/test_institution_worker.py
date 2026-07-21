from __future__ import annotations

import base64
import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from app.institution.worker import InstitutionWorkerError, verify_worker_envelope


def _signed_envelope(
    *, tenant_id: str = "tenant-a", expires_in: timedelta = timedelta(minutes=5)
) -> tuple[dict, str]:
    key = Ed25519PrivateKey.generate()
    public = base64.urlsafe_b64encode(
        key.public_key().public_bytes(
            serialization.Encoding.Raw,
            serialization.PublicFormat.Raw,
        )
    ).decode().rstrip("=")
    payload = {
        "protocol_version": "2026-07-01",
        "job_id": "job-1",
        "tenant_id": tenant_id,
        "workflow": "text_recognition",
        "source": {
            "kind": "source_document",
            "sha256": "a" * 64,
            "size_bytes": 128,
            "media_type": "application/pdf",
            "storage_key": "tenant-a/workers/sources/aa/value",
        },
        "settings": {},
        "allowed_outputs": ["result_json"],
        "expires_at": (datetime.now(timezone.utc) + expires_in).isoformat(),
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str).encode()
    signature = base64.urlsafe_b64encode(key.sign(encoded)).decode().rstrip("=")
    return {
        "algorithm": "Ed25519",
        "payload": payload,
        "signature": signature,
        "public_key": public,
    }, public


def test_worker_verifies_pinned_server_signature(monkeypatch):
    monkeypatch.setattr(
        "app.institution.worker.get_settings",
        lambda: SimpleNamespace(max_upload_bytes=1024, runtime_profile="full"),
    )
    signed, public = _signed_envelope()
    verified = verify_worker_envelope(
        signed, pinned_public_key=public, tenant_id="tenant-a"
    )
    assert verified["job_id"] == "job-1"


def test_worker_rejects_tampered_or_unpinned_envelope(monkeypatch):
    monkeypatch.setattr(
        "app.institution.worker.get_settings",
        lambda: SimpleNamespace(max_upload_bytes=1024, runtime_profile="full"),
    )
    signed, public = _signed_envelope()
    signed["payload"]["workflow"] = "layout_parsing"
    with pytest.raises(InstitutionWorkerError, match="signature"):
        verify_worker_envelope(signed, pinned_public_key=public, tenant_id="tenant-a")

    other_public = base64.urlsafe_b64encode(b"x" * 32).decode().rstrip("=")
    signed, _ = _signed_envelope()
    with pytest.raises(InstitutionWorkerError, match="enrolled institution"):
        verify_worker_envelope(
            signed, pinned_public_key=other_public, tenant_id="tenant-a"
        )


def test_worker_rejects_cross_tenant_or_expired_job(monkeypatch):
    monkeypatch.setattr(
        "app.institution.worker.get_settings",
        lambda: SimpleNamespace(max_upload_bytes=1024, runtime_profile="full"),
    )
    signed, public = _signed_envelope(tenant_id="tenant-b")
    with pytest.raises(InstitutionWorkerError, match="tenant"):
        verify_worker_envelope(signed, pinned_public_key=public, tenant_id="tenant-a")

    signed, public = _signed_envelope(expires_in=timedelta(seconds=-1))
    with pytest.raises(InstitutionWorkerError, match="expired"):
        verify_worker_envelope(signed, pinned_public_key=public, tenant_id="tenant-a")
