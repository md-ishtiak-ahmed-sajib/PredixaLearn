from __future__ import annotations

import base64
import hashlib
import json
import time
from datetime import datetime, timedelta, timezone

import jwt
from conftest import token
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.asymmetric.rsa import generate_private_key
from predixalearn_protocol import SyncEnvelope
from sqlalchemy import select

from institution_server.database import SessionLocal
from institution_server.models import Resource


def device_keypair() -> tuple[Ed25519PrivateKey, str]:
    private_key = Ed25519PrivateKey.generate()
    public_key = private_key.public_key().public_bytes(
        serialization.Encoding.Raw, serialization.PublicFormat.Raw
    )
    return private_key, base64.urlsafe_b64encode(public_key).decode().rstrip("=")


def worker_headers(
    private_key: Ed25519PrivateKey,
    credential: str,
    *,
    method: str,
    path: str,
    query: str = "",
) -> dict[str, str]:
    timestamp = str(int(time.time()))
    target = f"{path}?{query}" if query else path
    message = f"{timestamp}\n{method.upper()}\n{target}".encode()
    return {
        "Authorization": f"Bearer {credential}",
        "X-PredixaLearn-Device-Timestamp": timestamp,
        "X-PredixaLearn-Device-Signature": base64.urlsafe_b64encode(
            private_key.sign(message)
        )
        .decode()
        .rstrip("="),
    }


def document_payload(name: str = "Physics paper") -> dict:
    return {
        "title": name,
        "status": "indexed",
        "content_sha256": hashlib.sha256(name.encode()).hexdigest(),
        "search_text": "Newton force acceleration examination question",
        "payload": {"subject": "Physics", "academic_level": "Secondary"},
    }


def test_health_security_and_authentication(client):
    health = client.get("/health")
    assert health.status_code == 200
    assert health.json()["ready"] is True
    assert health.headers["cache-control"] == "no-store"
    assert health.headers["x-content-type-options"] == "nosniff"
    assert client.get("/api/v2/me").status_code == 401


def test_portal_cookie_mutations_require_launch_scoped_csrf(client):
    now = datetime.now(timezone.utc)
    session = jwt.encode(
        {
            "sub": "teacher-cookie",
            "tenant_id": "tenant-a",
            "roles": ["teacher"],
            "csrf": "portal-csrf-token",
            "aud": "predixalearn-portal",
            "iat": now,
            "exp": now + timedelta(hours=1),
        },
        "institution-test-secret-at-least-32-bytes",
        algorithm="HS256",
    )
    client.cookies.set("predixalearn_session", session)
    assert client.post("/api/v2/search", json={"query": "physics"}).status_code == 403
    accepted = client.post(
        "/api/v2/search",
        headers={"X-CSRF-Token": "portal-csrf-token"},
        json={"query": "physics"},
    )
    assert accepted.status_code == 200
    client.cookies.clear()


def test_archive_search_is_tenant_scoped_and_semantic_is_opt_in(client):
    tenant_a = {"Authorization": f"Bearer {token(tenant_id='tenant-a')}"}
    tenant_b = {"Authorization": f"Bearer {token(tenant_id='tenant-b')}"}
    created = client.post("/api/v2/archive/documents", headers=tenant_a, json=document_payload())
    assert created.status_code == 201
    assert created.headers["etag"] == 'W/"1"'

    visible = client.post("/api/v2/search", headers=tenant_a, json={"query": "acceleration"})
    hidden = client.post("/api/v2/search", headers=tenant_b, json={"query": "acceleration"})
    assert [item["title"] for item in visible.json()["items"]] == ["Physics paper"]
    assert hidden.json()["items"] == []
    semantic = client.post(
        "/api/v2/search", headers=tenant_a, json={"query": "motion", "semantic": True}
    )
    assert semantic.status_code == 409
    assert semantic.json()["detail"]["code"] == "semantic_search_disabled"


def test_evidence_is_immutable_and_patch_requires_current_etag(client, admin_headers):
    document = client.post(
        "/api/v2/archive/documents", headers=admin_headers, json=document_payload()
    ).json()
    evidence = client.post(
        f"/api/v2/archive/documents/{document['id']}/evidence",
        headers=admin_headers,
        json={
            "title": "OCR evidence v1",
            "content_sha256": "a" * 64,
            "search_text": "immutable text",
            "payload": {"source_line_ids": ["p1-l1"]},
        },
    )
    assert evidence.status_code == 201

    missing_etag = client.patch(
        f"/api/v2/archive/documents/{document['id']}",
        headers=admin_headers,
        json={"title": "Changed"},
    )
    assert missing_etag.status_code == 412
    patched = client.patch(
        f"/api/v2/archive/documents/{document['id']}",
        headers={**admin_headers, "If-Match": 'W/"1"'},
        json={"title": "Changed"},
    )
    assert patched.status_code == 200
    stale = client.patch(
        f"/api/v2/archive/documents/{document['id']}",
        headers={**admin_headers, "If-Match": 'W/"1"'},
        json={"title": "Lost update"},
    )
    assert stale.status_code == 412


def test_collaborative_review_decision_and_hash_chained_audit(client, admin_headers):
    review = client.post(
        "/api/v2/reviews",
        headers=admin_headers,
        json={"title": "Moderate paper", "status": "in_review", "payload": {}},
    ).json()
    comment = client.post(
        f"/api/v2/reviews/{review['id']}/comments",
        headers=admin_headers,
        json={"text": "Please check question 4", "mentions": ["reviewer-2"]},
    )
    assert comment.status_code == 201
    decision = client.post(
        f"/api/v2/reviews/{review['id']}/decision",
        headers={**admin_headers, "If-Match": review["etag"]},
        json={"decision": "approved", "rationale": "Evidence verified"},
    )
    assert decision.json()["status"] == "approved"
    events = client.get("/api/v2/audit", headers=admin_headers).json()["items"]
    assert len(events) >= 3
    chronological = list(reversed(events))
    assert chronological[0]["previous_hash"] is None
    for previous, current in zip(chronological, chronological[1:], strict=False):
        assert current["previous_hash"] == previous["event_hash"]


def test_published_curriculum_and_rubric_versions_are_immutable(client, admin_headers):
    curriculum = client.post(
        "/api/v2/curricula/versions",
        headers=admin_headers,
        json={
            "title": "National science",
            "version_label": "2026",
            "subject": "Science",
            "academic_level": "Grade 10",
            "payload": {},
        },
    ).json()
    objective = client.post(
        f"/api/v2/curricula/versions/{curriculum['id']}/objectives",
        headers=admin_headers,
        json={
            "title": "Explain motion",
            "stable_code": "SCI-MOTION-01",
            "description": "Explain force and acceleration",
            "payload": {},
        },
    )
    assert objective.status_code == 201
    published = client.post(
        f"/api/v2/curricula/versions/{curriculum['id']}/publish",
        headers={**admin_headers, "If-Match": curriculum["etag"]},
    )
    assert published.json()["status"] == "published"
    late = client.post(
        f"/api/v2/curricula/versions/{curriculum['id']}/objectives",
        headers=admin_headers,
        json={
            "title": "Late objective",
            "stable_code": "LATE",
            "description": "Must not mutate published version",
            "payload": {},
        },
    )
    assert late.status_code == 409

    rubric = client.post(
        "/api/v2/rubrics/versions",
        headers=admin_headers,
        json={
            "title": "Physics rubric",
            "version_label": "1",
            "maximum_marks": 10,
            "criteria": [{"id": "reasoning", "weight": 100, "levels": ["clear", "partial"]}],
            "payload": {},
        },
    )
    assert rubric.status_code == 201
    assert client.post(
        f"/api/v2/rubrics/versions/{rubric.json()['id']}/publish",
        headers={**admin_headers, "If-Match": rubric.json()["etag"]},
    ).json()["status"] == "published"


def test_answer_script_requires_teacher_approval_and_never_auto_publishes(client, admin_headers):
    rubric = client.post(
        "/api/v2/rubrics/versions",
        headers=admin_headers,
        json={
            "title": "Essay rubric",
            "version_label": "1",
            "maximum_marks": 20,
            "criteria": [{"id": "evidence", "weight": 100}],
            "payload": {},
        },
    ).json()
    assert client.post(
        f"/api/v2/rubrics/versions/{rubric['id']}/publish",
        headers={**admin_headers, "If-Match": rubric["etag"]},
    ).status_code == 200
    assessment = client.post(
        "/api/v2/assessments",
        headers=admin_headers,
        json={"title": "Assessment 1", "status": "published", "payload": {"version": "1"}},
    ).json()
    script = client.post(
        "/api/v2/answer-scripts",
        headers=admin_headers,
        json={
            "title": "Pseudonymous script",
            "assessment_id": assessment["id"],
            "learner_pseudonym": "learner-0001",
            "rubric_version_id": rubric["id"],
            "status": "ready_for_review",
            "payload": {"evidence_summary": "Student cites source A"},
        },
    ).json()
    approved = client.post(
        f"/api/v2/answer-scripts/{script['id']}/approve",
        headers={**admin_headers, "If-Match": script["etag"]},
        json={"teacher_mark": 16, "rationale": "Teacher checked evidence", "approve_for_lms": False},
    )
    assert approved.status_code == 200
    assert approved.json()["payload"]["teacher_mark"] == 16
    assert approved.json()["payload"]["grade_published"] is False


def test_accessibility_requires_policy_and_human_verification(client, admin_headers):
    blocked = client.post(
        "/api/v2/accessibility/descriptions",
        headers=admin_headers,
        json={
            "title": "Figure 1 description",
            "figure_id": "figure-1",
            "description": "A rising line on a time graph.",
            "description_kind": "short",
            "generation_mode": "institution_ai",
            "payload": {},
        },
    )
    assert blocked.status_code == 409
    figure = client.post(
        "/api/v2/figures",
        headers=admin_headers,
        json={"title": "Figure 1", "content_sha256": "f" * 64, "payload": {}},
    ).json()
    draft = client.post(
        "/api/v2/accessibility/descriptions",
        headers=admin_headers,
        json={
            "title": "Figure 1 description",
            "figure_id": figure["id"],
            "description": "A rising line on a time graph.",
            "description_kind": "short",
            "generation_mode": "local_model",
            "model_name": "approved-local-captioner",
            "payload": {},
        },
    ).json()
    reviewed = client.post(
        f"/api/v2/accessibility/descriptions/{draft['id']}/verify",
        headers={**admin_headers, "If-Match": draft["etag"]},
        json={"decision": "approved", "notes": "Compared with source figure"},
    )
    assert reviewed.json()["status"] == "approved"
    assert reviewed.json()["payload"]["verification"]["reviewer"] == "teacher-1"


def test_dataset_requires_deidentification_and_two_independent_reviews(client, admin_headers):
    dataset = client.post(
        "/api/v2/datasets",
        headers=admin_headers,
        json={
            "title": "Verified OCR corrections",
            "purpose": "Improve institution OCR quality",
            "schema_version": "1",
            "allowed_uses": ["internal-evaluation"],
            "license_name": "Institution internal",
            "retention_days": 365,
            "payload": {},
        },
    ).json()
    source_document = client.post(
        "/api/v2/archive/documents",
        headers=admin_headers,
        json=document_payload("Dataset source"),
    ).json()
    unsafe = client.post(
        f"/api/v2/datasets/{dataset['id']}/items",
        headers=admin_headers,
        json={
            "title": "Unsafe student sample",
            "source_document_id": source_document["id"],
            "provenance": {"method": "teacher opt-in"},
            "labels": {"text": "answer"},
            "contains_student_data": True,
            "deidentified": False,
            "payload": {},
        },
    )
    assert unsafe.status_code == 422
    item = client.post(
        f"/api/v2/datasets/{dataset['id']}/items",
        headers=admin_headers,
        json={
            "title": "Verified line",
            "source_document_id": source_document["id"],
            "content_sha256": "b" * 64,
            "provenance": {"method": "explicit teacher opt-in"},
            "labels": {"text": "verified answer"},
            "contains_student_data": True,
            "deidentified": True,
            "payload": {},
        },
    ).json()
    reviewer_a = {"Authorization": f"Bearer {token('steward-a', roles=['data_steward'])}"}
    reviewer_b = {"Authorization": f"Bearer {token('steward-b', roles=['data_steward'])}"}
    first = client.post(
        f"/api/v2/datasets/items/{item['id']}/reviews",
        headers={**reviewer_a, "If-Match": item["etag"]},
        json={"decision": "verified", "notes": "Identity removed"},
    )
    assert first.json()["status"] == "silver"
    duplicate = client.post(
        f"/api/v2/datasets/items/{item['id']}/reviews",
        headers={**reviewer_a, "If-Match": first.json()["etag"]},
        json={"decision": "verified", "notes": "Again"},
    )
    assert duplicate.status_code == 409
    second = client.post(
        f"/api/v2/datasets/items/{item['id']}/reviews",
        headers={**reviewer_b, "If-Match": first.json()["etag"]},
        json={"decision": "verified", "notes": "Provenance checked"},
    )
    assert second.json()["status"] == "gold"
    release = client.post(
        f"/api/v2/datasets/{dataset['id']}/release", headers=reviewer_b
    )
    assert release.status_code == 201
    assert release.json()["content_sha256"]
    exported = client.get(
        f"/api/v2/datasets/releases/{release.json()['id']}/download",
        headers=reviewer_b,
    )
    assert exported.status_code == 200
    records = [json.loads(line) for line in exported.text.splitlines()]
    assert records[0]["tenant_isolated"] is True
    assert records[1]["split"] in {"train", "validation", "test"}


def test_worker_enrollment_is_one_time_and_job_envelope_is_signed(client, admin_headers):
    issued = client.post(
        "/api/v2/workers/enrollment-tokens",
        headers=admin_headers,
        json={"expires_minutes": 10},
    ).json()
    device_private_key, device_public_key = device_keypair()
    enrollment = {
        "enrollment_token": issued["enrollment_token"],
        "name": "On-prem GPU worker",
        "device_public_key": device_public_key,
        "capabilities": {"workflows": ["text_recognition"]},
    }
    worker = client.post("/api/v2/workers/enroll", json=enrollment)
    assert worker.status_code == 201
    signing_public_key = worker.json()["server_signing_public_key"]
    assert len(base64.urlsafe_b64decode(signing_public_key + "==")) == 32
    assert client.post("/api/v2/workers/enroll", json=enrollment).status_code == 403
    source_data = b"%PDF-1.7\noriginal synthetic worker fixture"
    source_response = client.post(
        "/api/v2/worker-jobs/sources",
        headers=admin_headers,
        files={"file": ("paper.pdf", source_data, "application/pdf")},
    )
    assert source_response.status_code == 201
    source = source_response.json()
    assert source["kind"] == "source_document"
    job = client.post(
        "/api/v2/worker-jobs",
        headers=admin_headers,
        json={
            "workflow": "text_recognition",
            "source_artifact": source,
            "allowed_outputs": ["result_json"],
        },
    )
    assert job.status_code == 201
    assert (
        client.get(
            "/api/v2/workers/jobs/next",
            headers={"Authorization": f"Bearer {worker.json()['credential']}"},
        ).status_code
        == 401
    )
    claimed = client.get(
        "/api/v2/workers/jobs/next",
        headers=worker_headers(
            device_private_key,
            worker.json()["credential"],
            method="GET",
            path="/api/v2/workers/jobs/next",
        ),
    )
    assert claimed.status_code == 200
    signed = claimed.json()
    assert signed["algorithm"] == "Ed25519"
    assert signed["public_key"] == signing_public_key
    assert signed["payload"]["job_id"] == job.json()["id"]
    assert len(base64.urlsafe_b64decode(signed["signature"] + "==")) == 64
    downloaded = client.get(
        f"/api/v2/workers/jobs/{job.json()['id']}/source",
        headers=worker_headers(
            device_private_key,
            worker.json()["credential"],
            method="GET",
            path=f"/api/v2/workers/jobs/{job.json()['id']}/source",
        ),
    )
    assert downloaded.content == source_data
    output = b'{"full_text":"verified worker output"}'
    artifact = client.post(
        f"/api/v2/workers/jobs/{job.json()['id']}/artifacts",
        params={"kind": "result_json"},
        headers=worker_headers(
            device_private_key,
            worker.json()["credential"],
            method="POST",
            path=f"/api/v2/workers/jobs/{job.json()['id']}/artifacts",
            query="kind=result_json",
        ),
        files={"file": ("result.json", output, "application/json")},
    )
    assert artifact.status_code == 201
    published = client.post(
        f"/api/v2/workers/jobs/{job.json()['id']}/result",
        headers=worker_headers(
            device_private_key,
            worker.json()["credential"],
            method="POST",
            path=f"/api/v2/workers/jobs/{job.json()['id']}/result",
        ),
        json={
            "status": "completed",
            "artifacts": [artifact.json()],
            "result_summary": {"page_count": 1},
        },
    )
    assert published.status_code == 200
    assert published.json()["status"] == "completed"
    assert (
        client.get(
            f"/api/v2/workers/jobs/{job.json()['id']}/source",
            headers=worker_headers(
                device_private_key,
                worker.json()["credential"],
                method="GET",
                path=f"/api/v2/workers/jobs/{job.json()['id']}/source",
            ),
        ).status_code
        == 404
    )
    repeated = client.post(
        f"/api/v2/workers/jobs/{job.json()['id']}/result",
        headers=worker_headers(
            device_private_key,
            worker.json()["credential"],
            method="POST",
            path=f"/api/v2/workers/jobs/{job.json()['id']}/result",
        ),
        json={
            "status": "completed",
            "artifacts": [artifact.json()],
            "result_summary": {"page_count": 1},
        },
    )
    assert repeated.json()["status"] == "completed"


def test_explicit_sync_is_idempotent_and_never_claims_source_retention(client, admin_headers):
    issued = client.post(
        "/api/v2/workers/enrollment-tokens",
        headers=admin_headers,
        json={"expires_minutes": 10},
    ).json()
    device_private_key, device_public_key = device_keypair()
    worker = client.post(
        "/api/v2/workers/enroll",
        json={
            "enrollment_token": issued["enrollment_token"],
            "name": "Local OCR",
            "device_public_key": device_public_key,
            "capabilities": {"workflows": ["text_recognition"]},
        },
    ).json()
    result = {"full_text": "Selected institutional archive evidence"}
    encoded = json.dumps(
        result, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode()
    evidence_hash = hashlib.sha256(encoded).hexdigest()
    preview_data = b"\x89PNG\r\n\x1a\nsynthetic-watermarked-preview"
    idempotency_key = hashlib.sha256(b"explicit-sync-1").hexdigest()
    envelope = {
        "tenant_id": "tenant-a",
        "device_id": worker["worker_id"],
        "events": [
            {
                "event_type": "archive.upsert",
                "resource_id": "local-job-1",
                "resource_version": 1,
                "idempotency_key": idempotency_key,
                "payload": {
                    "title": "Selected paper",
                    "summary": {"document_profile": "exam"},
                    "ocr_result_sha256": evidence_hash,
                    "search_text": result["full_text"],
                    "result": result,
                    "analysis_overlay": {
                        "analysis": {
                            "ocr_result_sha256": evidence_hash,
                            "model": {"status": "local_only"},
                            "questions": [],
                        },
                        "audit_events": [{"event": "teacher_reviewed"}],
                    },
                    "approved_previews": [
                        {
                            "filename": "page-001.png",
                            "media_type": "image/png",
                            "sha256": hashlib.sha256(preview_data).hexdigest(),
                            "content_base64": base64.b64encode(preview_data).decode(),
                            "explicitly_approved": True,
                        }
                    ],
                },
            }
        ],
    }
    envelope = SyncEnvelope.model_validate(envelope).model_dump(mode="json")
    canonical = json.dumps(
        envelope, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode()
    signed_at = str(int(time.time()))
    signature = device_private_key.sign(signed_at.encode() + b"." + canonical)
    unsigned_headers = {
        "Authorization": f"Bearer {worker['credential']}",
        "Idempotency-Key": idempotency_key,
        "Content-Type": "application/json",
    }
    assert client.post("/api/v2/sync", headers=unsigned_headers, content=canonical).status_code == 401
    headers = {
        **unsigned_headers,
        "X-PredixaLearn-Device-Timestamp": signed_at,
        "X-PredixaLearn-Device-Signature": base64.urlsafe_b64encode(signature).decode().rstrip("="),
    }
    first = client.post("/api/v2/sync", headers=headers, content=canonical)
    assert first.status_code == 200
    assert first.json()["uploaded_existing_history_automatically"] is False
    assert first.json()["accepted"][0]["resource"]["payload"]["source_retained"] is False
    assert first.json()["accepted"][0]["analysis_id"].startswith("local-job-1-analysis-")
    assert first.json()["accepted"][0]["approved_preview_ids"][0].startswith(
        "local-job-1-preview-"
    )
    replay = client.post("/api/v2/sync", headers=headers, content=canonical)
    assert replay.status_code == 200
    assert replay.json()["accepted"][0]["resource"]["id"] == "local-job-1"
    search = client.post(
        "/api/v2/search",
        headers=admin_headers,
        json={"query": "institutional archive"},
    )
    assert [item["id"] for item in search.json()["items"]] == ["local-job-1"]


def test_curriculum_import_preview_commit_case_export_and_coverage(client, admin_headers):
    curriculum = client.post(
        "/api/v2/curricula/versions",
        headers=admin_headers,
        json={
            "title": "Computing curriculum",
            "version_label": "2026.1",
            "subject": "Computing",
            "academic_level": "Mixed",
            "payload": {},
        },
    ).json()
    csv_data = (
        "stable_code,title,description,parent_code,prerequisites,replaces\n"
        "CS-1,Algorithms,Explain an algorithm,,,\n"
        "CS-2,Complexity,Compare algorithm complexity,CS-1,CS-1,\n"
    ).encode()
    preview = client.post(
        f"/api/v2/curricula/versions/{curriculum['id']}/import",
        headers=admin_headers,
        files={"file": ("curriculum.csv", csv_data, "text/csv")},
    )
    assert preview.json() == {
        "valid": True,
        "committed": False,
        "objective_count": 2,
        "errors": [],
    }
    committed = client.post(
        f"/api/v2/curricula/versions/{curriculum['id']}/import?commit=true",
        headers=admin_headers,
        files={"file": ("curriculum.csv", csv_data, "text/csv")},
    )
    assert committed.status_code == 200
    assert committed.json()["objective_count"] == 2
    case = client.get(
        f"/api/v2/curricula/versions/{curriculum['id']}/case", headers=admin_headers
    )
    assert len(case.json()["CFItems"]) == 2
    coverage = client.get(
        f"/api/v2/curricula/versions/{curriculum['id']}/coverage",
        headers=admin_headers,
    )
    assert coverage.json()["unmapped_count"] == 2
    source = client.post(
        "/api/v2/archive/documents",
        headers=admin_headers,
        json={
            **document_payload("Algorithm complexity question"),
            "search_text": "Compare algorithm complexity and data structures",
        },
    ).json()
    suggestions = client.post(
        f"/api/v2/curricula/versions/{curriculum['id']}/mapping-suggestions",
        headers=admin_headers,
        json={"source_resource_ids": [source["id"]]},
    )
    assert suggestions.status_code == 200
    assert suggestions.json()["items"][0]["mapping_status"] == "draft"
    assert suggestions.json()["external_ai_used"] is False
    suggested = suggestions.json()["items"][0]
    mapping = client.post(
        "/api/v2/objective-mappings",
        headers=admin_headers,
        json={
            "title": "Teacher-approved mapping",
            "source_resource_id": source["id"],
            "objective_id": suggested["objective_id"],
            "curriculum_version_id": curriculum["id"],
            "mapping_status": "approved",
            "rationale": "Teacher reviewed deterministic overlap",
            "payload": {},
        },
    )
    assert mapping.status_code == 201


def test_answer_alignment_preserves_source_confidences_and_never_marks(client, admin_headers):
    rubric = client.post(
        "/api/v2/rubrics/versions",
        headers=admin_headers,
        json={
            "title": "Short response rubric",
            "version_label": "1",
            "maximum_marks": 5,
            "criteria": [{"id": "reasoning", "weight": 100}],
            "payload": {},
        },
    ).json()
    assert client.post(
        f"/api/v2/rubrics/versions/{rubric['id']}/publish",
        headers={**admin_headers, "If-Match": rubric["etag"]},
    ).status_code == 200
    assessment = client.post(
        "/api/v2/assessments",
        headers=admin_headers,
        json={"title": "Alignment assessment", "status": "published", "payload": {}},
    ).json()
    script = client.post(
        "/api/v2/answer-scripts",
        headers=admin_headers,
        json={
            "title": "Script for alignment",
            "assessment_id": assessment["id"],
            "learner_pseudonym": "learner-9999",
            "rubric_version_id": rubric["id"],
            "status": "draft",
            "payload": {},
        },
    ).json()
    analyzed = client.post(
        f"/api/v2/answer-scripts/{script['id']}/analyze",
        headers=admin_headers,
        json={
            "questions": [
                {
                    "question_id": "q-1",
                    "display_number": "1",
                    "text": "Explain inertia.",
                    "source_page": 1,
                    "source_line_ids": ["question-line-1"],
                    "rubric_criteria": ["reasoning"],
                }
            ],
            "response_lines": [
                {
                    "line_id": "answer-line-1",
                    "page": 2,
                    "text": "1. Inertia is resistance to a change in motion.",
                    "geometry": [0.1, 0.2, 0.8, 0.3],
                    "ocr_confidence": 0.91,
                }
            ],
        },
    )
    assert analyzed.status_code == 200
    response = analyzed.json()["responses"][0]
    assert response["response_line_ids"] == ["answer-line-1"]
    assert response["ocr_confidence"] == 0.91
    assert response["alignment_confidence"] == 0.95
    assert response["ai_confidence"] is None
    assert response["teacher_mark"] is None
    assert analyzed.json()["answer_script"]["payload"]["deterministic_analysis"]["automatic_mark"] is None


def test_automatic_accessibility_draft_cannot_export_until_human_verified(client, admin_headers):
    figure = client.post(
        "/api/v2/figures",
        headers=admin_headers,
        json={
            "title": "Velocity chart",
            "content_sha256": "d" * 64,
            "payload": {"page": 2, "geometry": [0.1, 0.1, 0.9, 0.8]},
        },
    ).json()
    generated = client.post(
        f"/api/v2/figures/{figure['id']}/accessibility-description",
        headers=admin_headers,
        json={
            "generation_mode": "local_model",
            "figure_kind": "chart",
            "extracted_text": "Time Velocity",
            "surrounding_context": "An object accelerates over five seconds.",
            "structured_data": {"chart_type": "line chart", "axes": ["time", "velocity"], "trend": "velocity increases"},
        },
    )
    assert generated.status_code == 201
    description_id = generated.json()["id"]
    assert generated.json()["status"] == "draft"
    assert generated.json()["payload"]["human_verification_required"] is True
    assert client.get(
        f"/api/v2/accessibility/descriptions/{description_id}/export",
        headers=admin_headers,
    ).status_code == 409
    assert client.post(
        f"/api/v2/accessibility/descriptions/{description_id}/verify",
        headers={**admin_headers, "If-Match": generated.json()["etag"]},
        json={"decision": "approved", "notes": "Chart checked against source"},
    ).status_code == 200
    exported = client.get(
        f"/api/v2/accessibility/descriptions/{description_id}/export",
        headers=admin_headers,
    )
    assert exported.status_code == 200
    assert "velocity increases" in exported.text


def test_lti_registration_keeps_only_non_secret_platform_metadata(client, admin_headers):
    response = client.post(
        "/api/v2/lti/registrations",
        headers=admin_headers,
        json={
            "title": "Institution LMS",
            "issuer": "https://lms.example.edu",
            "client_id": "client-1",
            "deployment_id": "deployment-1",
            "authorization_endpoint": "https://lms.example.edu/oidc/auth",
            "token_endpoint": "https://lms.example.edu/oauth/token",
            "jwks_url": "https://lms.example.edu/.well-known/jwks.json",
            "enabled_services": ["deep_linking", "nrps", "ags"],
            "payload": {},
        },
    )
    assert response.status_code == 201
    serialized = json.dumps(response.json())
    assert "secret" not in serialized.casefold()
    assert response.json()["payload"]["allowed_service_hosts"] == ["lms.example.edu"]


def test_lti_grade_return_requires_teacher_approval_and_uses_identity_vault(
    client, admin_headers, monkeypatch
):
    registration = client.post(
        "/api/v2/lti/registrations",
        headers=admin_headers,
        json={
            "title": "Grade return LMS",
            "issuer": "https://lms.example.edu",
            "client_id": "client-grade",
            "deployment_id": "deployment-grade",
            "authorization_endpoint": "https://lms.example.edu/oidc/auth",
            "token_endpoint": "https://lms.example.edu/oauth/token",
            "jwks_url": "https://lms.example.edu/.well-known/jwks.json",
            "enabled_services": ["ags"],
            "payload": {},
        },
    ).json()
    identity = client.post(
        "/api/v2/identity-mappings",
        headers=admin_headers,
        json={"learner_identifier": "lms-student-42"},
    ).json()
    rubric = client.post(
        "/api/v2/rubrics/versions",
        headers=admin_headers,
        json={
            "title": "LMS rubric",
            "version_label": "1",
            "maximum_marks": 20,
            "criteria": [{"id": "evidence", "weight": 100}],
            "payload": {},
        },
    ).json()
    assert client.post(
        f"/api/v2/rubrics/versions/{rubric['id']}/publish",
        headers={**admin_headers, "If-Match": rubric["etag"]},
    ).status_code == 200
    assessment = client.post(
        "/api/v2/assessments",
        headers=admin_headers,
        json={"title": "LTI assessment", "status": "published", "payload": {}},
    ).json()
    script = client.post(
        "/api/v2/answer-scripts",
        headers=admin_headers,
        json={
            "title": "Approved script",
            "assessment_id": assessment["id"],
            "learner_pseudonym": identity["pseudonym"],
            "rubric_version_id": rubric["id"],
            "status": "ready_for_review",
            "payload": {"lti_registration_id": registration["id"]},
        },
    ).json()
    approved = client.post(
        f"/api/v2/answer-scripts/{script['id']}/approve",
        headers={**admin_headers, "If-Match": script["etag"]},
        json={
            "teacher_mark": 17,
            "rationale": "Teacher verified the rubric evidence",
            "approve_for_lms": True,
        },
    )
    assert approved.status_code == 200

    private_key = generate_private_key(public_exponent=65537, key_size=2048)
    private_pem = private_key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ).decode()
    monkeypatch.setenv("PREDIXALEARN_LTI_PRIVATE_KEY", private_pem)
    monkeypatch.setenv("PREDIXALEARN_LTI_KEY_ID", "test-key")

    class FakeResponse:
        def __init__(self, payload=None):
            self.payload = payload or {}

        def raise_for_status(self):
            return None

        def json(self):
            return self.payload

    class FakeClient:
        grade_payload = None

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def post(self, url, *, data=None, headers=None, json=None):
            del headers
            if data:
                return FakeResponse({"access_token": "lms-access-token"})
            self.grade_payload = json
            assert url == "https://lms.example.edu/lineitems/1/scores"
            return FakeResponse()

    fake_client = FakeClient()
    monkeypatch.setattr(
        "institution_server.lti.httpx.AsyncClient", lambda **_kwargs: fake_client
    )
    returned = client.post(
        "/api/v2/lti/grades/return",
        headers=admin_headers,
        json={
            "answer_script_id": script["id"],
            "identity_mapping_id": identity["id"],
            "lineitem_url": "https://lms.example.edu/lineitems/1",
            "score_maximum": 20,
        },
    )
    assert returned.status_code == 200
    assert fake_client.grade_payload["userId"] == "lms-student-42"
    assert fake_client.grade_payload["scoreGiven"] == 17
    assert "lms-student-42" not in json.dumps(returned.json())


def test_primary_create_endpoints_support_idempotent_replay(client, admin_headers):
    headers = {**admin_headers, "Idempotency-Key": "archive-create-0001"}
    first = client.post(
        "/api/v2/archive/documents", headers=headers, json=document_payload("Idempotent paper")
    )
    replay = client.post(
        "/api/v2/archive/documents", headers=headers, json=document_payload("Idempotent paper")
    )
    assert first.status_code == 201
    assert replay.status_code == 201
    assert replay.headers["x-idempotent-replay"] == "true"
    assert replay.json()["id"] == first.json()["id"]
    conflict = client.post(
        "/api/v2/archive/documents", headers=headers, json=document_payload("Different paper")
    )
    assert conflict.status_code == 409
    assert conflict.json()["detail"]["code"] == "idempotency_conflict"


def test_private_resources_are_visible_only_to_the_creator(client):
    owner = {"Authorization": f"Bearer {token(subject='teacher-owner')}"}
    colleague = {"Authorization": f"Bearer {token(subject='teacher-colleague')}"}
    payload = document_payload("Private paper")
    payload["access_classification"] = "private"
    created = client.post("/api/v2/archive/documents", headers=owner, json=payload)
    assert created.status_code == 201
    assert client.get(
        f"/api/v2/archive/documents/{created.json()['id']}", headers=colleague
    ).status_code == 404
    owner_search = client.post(
        "/api/v2/search", headers=owner, json={"query": "acceleration"}
    )
    colleague_search = client.post(
        "/api/v2/search", headers=colleague, json={"query": "acceleration"}
    )
    assert len(owner_search.json()["items"]) == 1
    assert colleague_search.json()["items"] == []


def test_feature_inventory_is_exposed_without_authentication(client):
    response = client.get("/api/v2/features")
    assert response.status_code == 200
    assert {"archive_review", "curriculum", "workers"}.issubset(response.json()["enabled"])

    archive_only = {
        "Authorization": f"Bearer {token(features=['archive_review'])}"
    }
    assert client.get("/api/v2/archive/documents", headers=archive_only).status_code == 200
    denied = client.get("/api/v2/curricula/versions", headers=archive_only)
    assert denied.status_code == 404
    assert denied.json()["detail"]["code"] == "feature_disabled"


def test_learner_identity_is_encrypted_separately_and_tenant_scoped(client):
    steward = {
        "Authorization": f"Bearer {token(subject='steward-a', roles=['data_steward'])}"
    }
    other_tenant = {
        "Authorization": f"Bearer {token(subject='steward-b', tenant_id='tenant-b', roles=['data_steward'])}"
    }
    created = client.post(
        "/api/v2/identity-mappings",
        headers=steward,
        json={"learner_identifier": "student-legal-id-247", "key_version": "test-v1"},
    )
    assert created.status_code == 201
    assert "student-legal-id-247" not in json.dumps(created.json())
    resolved = client.get(
        f"/api/v2/identity-mappings/{created.json()['id']}/resolve", headers=steward
    )
    assert resolved.json()["learner_identifier"] == "student-legal-id-247"
    assert client.get(
        f"/api/v2/identity-mappings/{created.json()['id']}/resolve", headers=other_tenant
    ).status_code == 404


def test_retention_requires_confirmation_and_preserves_legal_holds(client, admin_headers):
    removable = document_payload("Archived removable")
    removable["status"] = "archived"
    held = document_payload("Archived held")
    held["status"] = "archived"
    held["payload"]["legal_hold"] = True
    removable_id = client.post(
        "/api/v2/archive/documents", headers=admin_headers, json=removable
    ).json()["id"]
    held_id = client.post(
        "/api/v2/archive/documents", headers=admin_headers, json=held
    ).json()["id"]
    with SessionLocal() as session:
        records = list(
            session.scalars(
                select(Resource).where(Resource.resource_id.in_([removable_id, held_id]))
            )
        )
        for record in records:
            record.updated_at = datetime.now(timezone.utc) - timedelta(days=10)
        session.commit()
    request = {"older_than_days": 5, "resource_types": ["archive_document"]}
    preview = client.post("/api/v2/retention/preview", headers=admin_headers, json=request)
    assert [item["id"] for item in preview.json()["items"]] == [removable_id]
    assert client.post(
        "/api/v2/retention/apply", headers=admin_headers, json=request
    ).status_code == 409
    applied = client.post(
        "/api/v2/retention/apply",
        headers=admin_headers,
        json={**request, "confirm": True},
    )
    assert applied.json()["deleted_count"] == 1
    assert client.get(
        f"/api/v2/archive/documents/{removable_id}", headers=admin_headers
    ).status_code == 404
    assert client.get(
        f"/api/v2/archive/documents/{held_id}", headers=admin_headers
    ).status_code == 200


def test_artifact_publication_is_checksum_verified_and_tenant_scoped(client, admin_headers):
    document = client.post(
        "/api/v2/archive/documents", headers=admin_headers, json=document_payload("Artifacts")
    ).json()
    content = b"# Approved reconstructed evidence\n"
    published = client.post(
        "/api/v2/artifacts",
        headers=admin_headers,
        params={"parent_id": document["id"], "kind": "markdown"},
        files={"file": ("evidence.md", content, "text/markdown")},
    )
    assert published.status_code == 201
    downloaded = client.get(
        f"/api/v2/artifacts/{published.json()['id']}", headers=admin_headers
    )
    assert downloaded.content == content
    assert downloaded.headers["x-content-sha256"] == hashlib.sha256(content).hexdigest()
    tenant_b = {"Authorization": f"Bearer {token(tenant_id='tenant-b')}"}
    assert client.get(
        f"/api/v2/artifacts/{published.json()['id']}", headers=tenant_b
    ).status_code == 404
