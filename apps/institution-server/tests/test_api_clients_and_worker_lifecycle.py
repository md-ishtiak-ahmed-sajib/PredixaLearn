from __future__ import annotations

import base64
import time

from conftest import token
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey


def _device_keypair() -> tuple[Ed25519PrivateKey, str]:
    private_key = Ed25519PrivateKey.generate()
    public_key = private_key.public_key().public_bytes(
        serialization.Encoding.Raw,
        serialization.PublicFormat.Raw,
    )
    return private_key, base64.urlsafe_b64encode(public_key).decode().rstrip("=")


def _worker_headers(
    private_key: Ed25519PrivateKey,
    credential: str,
    *,
    path: str,
) -> dict[str, str]:
    timestamp = str(int(time.time()))
    signature = private_key.sign(f"{timestamp}\nGET\n{path}".encode())
    return {
        "Authorization": f"Bearer {credential}",
        "X-PredixaLearn-Device-Timestamp": timestamp,
        "X-PredixaLearn-Device-Signature": base64.urlsafe_b64encode(signature)
        .decode()
        .rstrip("="),
    }


def test_scoped_oauth_client_credentials_rotate_and_revoke(client, admin_headers):
    created = client.post(
        "/api/v2/api-clients",
        headers=admin_headers,
        json={
            "name": "Institution archive connector",
            "permissions": ["archive:read", "review:read"],
            "course_ids": ["course-physics"],
            "enabled_features": ["archive_review"],
            "expires_days": 90,
        },
    )
    assert created.status_code == 201
    provisioned = created.json()
    assert provisioned["secret_returned_once"] is True
    assert "secret_hash" not in provisioned

    tenant_b = {"Authorization": f"Bearer {token(tenant_id='tenant-b')}"}
    assert client.get("/api/v2/api-clients", headers=tenant_b).json()["items"] == []
    unsafe = client.post(
        "/api/v2/api-clients",
        headers=admin_headers,
        json={"name": "Unsafe", "permissions": ["identity:resolve"]},
    )
    assert unsafe.status_code == 422
    assert unsafe.json()["detail"]["code"] == "unsafe_client_permission"

    token_response = client.post(
        "/api/v2/oauth/token",
        auth=(provisioned["client_id"], provisioned["client_secret"]),
        data={"grant_type": "client_credentials", "scope": "archive:read"},
    )
    assert token_response.status_code == 200
    access_token = token_response.json()["access_token"]
    machine_headers = {"Authorization": f"Bearer {access_token}"}
    identity = client.get("/api/v2/me", headers=machine_headers)
    assert identity.status_code == 200
    assert identity.json()["tenant_id"] == "tenant-a"
    assert identity.json()["roles"] == []
    assert identity.json()["permissions"] == ["archive:read"]
    assert (
        client.post(
            "/api/v2/archive/documents",
            headers=machine_headers,
            json={"title": "Denied", "content_sha256": "0" * 64},
        ).status_code
        == 403
    )

    missing_etag = client.post(
        f"/api/v2/api-clients/{provisioned['client_id']}/rotate",
        headers=admin_headers,
    )
    assert missing_etag.status_code == 412
    rotated = client.post(
        f"/api/v2/api-clients/{provisioned['client_id']}/rotate",
        headers={**admin_headers, "If-Match": provisioned["etag"]},
    )
    assert rotated.status_code == 200
    assert rotated.json()["client_secret"] != provisioned["client_secret"]
    assert (
        client.post(
            "/api/v2/oauth/token",
            auth=(provisioned["client_id"], provisioned["client_secret"]),
            data={"grant_type": "client_credentials"},
        ).status_code
        == 401
    )
    assert (
        client.post(
            "/api/v2/oauth/token",
            auth=(provisioned["client_id"], rotated.json()["client_secret"]),
            data={"grant_type": "client_credentials", "scope": "archive:write"},
        ).status_code
        == 400
    )

    revoked = client.delete(
        f"/api/v2/api-clients/{provisioned['client_id']}",
        headers={**admin_headers, "If-Match": rotated.json()["etag"]},
    )
    assert revoked.status_code == 200
    assert revoked.json()["active"] is False
    assert (
        client.post(
            "/api/v2/oauth/token",
            auth=(provisioned["client_id"], rotated.json()["client_secret"]),
            data={"grant_type": "client_credentials"},
        ).status_code
        == 401
    )
    audit = client.get("/api/v2/audit", headers=admin_headers).json()["items"]
    actions = {event["action"] for event in audit}
    assert {
        "api_client.created",
        "api_client.token_issued",
        "api_client.rotated",
        "api_client.revoked",
    }.issubset(actions)


def test_worker_can_drain_resume_and_be_irreversibly_revoked(client, admin_headers):
    enrollment_token = client.post(
        "/api/v2/workers/enrollment-tokens",
        headers=admin_headers,
        json={"expires_minutes": 10},
    ).json()["enrollment_token"]
    private_key, public_key = _device_keypair()
    enrolled = client.post(
        "/api/v2/workers/enroll",
        json={
            "enrollment_token": enrollment_token,
            "name": "Drainable on-prem worker",
            "device_public_key": public_key,
            "capabilities": {"workflows": ["text_recognition"]},
        },
    ).json()
    listed = client.get("/api/v2/workers", headers=admin_headers).json()["items"]
    assert listed[0]["status"] == "active"

    draining = client.patch(
        f"/api/v2/workers/{enrolled['worker_id']}",
        headers={**admin_headers, "If-Match": listed[0]["etag"]},
        json={"status": "draining", "reason": "Runtime profile update"},
    )
    assert draining.status_code == 200
    assert draining.json()["status"] == "draining"

    source = client.post(
        "/api/v2/worker-jobs/sources",
        headers=admin_headers,
        files={"file": ("paper.pdf", b"%PDF-1.7\nfixture", "application/pdf")},
    ).json()
    job = client.post(
        "/api/v2/worker-jobs",
        headers=admin_headers,
        json={
            "workflow": "text_recognition",
            "source_artifact": source,
            "allowed_outputs": ["result_json"],
        },
    ).json()
    claim_path = "/api/v2/workers/jobs/next"
    drain_claim = client.get(
        claim_path,
        headers=_worker_headers(private_key, enrolled["credential"], path=claim_path),
    )
    assert drain_claim.status_code == 204
    assert drain_claim.headers["x-predixalearn-worker-state"] == "draining"

    active = client.patch(
        f"/api/v2/workers/{enrolled['worker_id']}",
        headers={**admin_headers, "If-Match": draining.json()["etag"]},
        json={"status": "active", "reason": "Maintenance complete"},
    )
    assert active.json()["status"] == "active"
    claimed = client.get(
        claim_path,
        headers=_worker_headers(private_key, enrolled["credential"], path=claim_path),
    )
    assert claimed.status_code == 200
    assert claimed.json()["payload"]["job_id"] == job["id"]

    revoked = client.patch(
        f"/api/v2/workers/{enrolled['worker_id']}",
        headers={**admin_headers, "If-Match": active.json()["etag"]},
        json={"status": "revoked", "reason": "Device retired"},
    )
    assert revoked.json()["status"] == "revoked"
    assert (
        client.get(
            claim_path,
            headers=_worker_headers(private_key, enrolled["credential"], path=claim_path),
        ).status_code
        == 403
    )
    restore = client.patch(
        f"/api/v2/workers/{enrolled['worker_id']}",
        headers={**admin_headers, "If-Match": revoked.json()["etag"]},
        json={"status": "active", "reason": "Should not be permitted"},
    )
    assert restore.status_code == 409


def test_saved_filters_and_review_assignments_are_tenant_scoped_and_versioned(
    client, admin_headers
):
    saved = client.post(
        "/api/v2/saved-filters",
        headers=admin_headers,
        json={
            "title": "Physics warnings needing review",
            "search": {
                "query": "mechanics",
                "course_id": "physics-101",
                "warning_max": 5,
                "statuses": ["indexed"],
            },
        },
    )
    assert saved.status_code == 201
    assert saved.json()["access_classification"] == "private"
    assert saved.json()["payload"]["search"]["course_id"] == "physics-101"
    tenant_b = {"Authorization": f"Bearer {token(tenant_id='tenant-b')}"}
    assert client.get("/api/v2/saved-filters", headers=tenant_b).json()["items"] == []

    review = client.post(
        "/api/v2/reviews",
        headers=admin_headers,
        json={
            "title": "Review mechanics evidence",
            "assigned_to": ["teacher-b", "teacher-a", "teacher-a"],
            "due_at": "2027-01-15T12:00:00Z",
            "moderation_required": True,
            "saved_filter_id": saved.json()["id"],
        },
    )
    assert review.status_code == 201
    assert review.json()["payload"]["assigned_to"] == ["teacher-a", "teacher-b"]
    assert review.json()["payload"]["moderation_required"] is True
    endpoint = f"/api/v2/reviews/{review.json()['id']}/assignment"
    assert (
        client.patch(
            endpoint,
            headers=admin_headers,
            json={"assigned_to": ["teacher-c"], "moderation_required": False},
        ).status_code
        == 412
    )
    reassigned = client.patch(
        endpoint,
        headers={**admin_headers, "If-Match": review.json()["etag"]},
        json={
            "assigned_to": ["teacher-c"],
            "due_at": "2027-02-01T09:00:00Z",
            "moderation_required": False,
        },
    )
    assert reassigned.status_code == 200
    assert reassigned.json()["payload"]["assigned_to"] == ["teacher-c"]
    assert reassigned.json()["version"] == review.json()["version"] + 1
