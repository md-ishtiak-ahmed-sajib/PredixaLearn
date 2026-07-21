from __future__ import annotations

# ruff: noqa: S106
import asyncio
import base64
import io
import json
import sys
import time
import zipfile
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from types import ModuleType, SimpleNamespace
from urllib.parse import parse_qs, urlsplit

import httpx
import jwt
import pytest
from conftest import token
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.asymmetric.rsa import generate_private_key
from fastapi import HTTPException
from sqlalchemy import select
from starlette.requests import Request

from institution_server.accessibility import (
    GenerateDescriptionRequest,
    institution_ai_description,
    local_description,
)
from institution_server.config import Settings, _bool, _choice, get_settings
from institution_server.curriculum import _csv_items, _json_items, validate_import
from institution_server.database import SessionLocal
from institution_server.events import event_stream
from institution_server.models import LtiNonce, Resource, WebhookDelivery
from institution_server.portal_auth import _required, callback, login, logout
from institution_server.security import Principal
from institution_server.storage import AzureObjectStorage, S3ObjectStorage, create_storage
from institution_server.webhooks import deliver_once, delivery_loop
from institution_server.workers import (
    _aware as worker_aware,
)
from institution_server.workers import (
    _ed25519_key,
    _expire_staged_sources,
    _source_media_type,
    _validate_worker_output,
    _worker_secret,
)


class FakeResponse:
    def __init__(self, payload=None, *, status_code: int = 200):
        self._payload = payload or {}
        self.status_code = status_code

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise httpx.HTTPStatusError(
                "failed",
                request=httpx.Request("POST", "https://example.edu"),
                response=httpx.Response(self.status_code),
            )


class FakeAsyncClient:
    response = FakeResponse()
    calls: list[tuple[str, dict]] = []

    def __init__(self, *args, **kwargs):
        del args, kwargs

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        del args

    async def post(self, url, **kwargs):
        self.calls.append((str(url), kwargs))
        return self.response

    async def get(self, url, **kwargs):
        self.calls.append((str(url), kwargs))
        return self.response


def test_accessibility_local_descriptions_cover_supported_evidence_shapes():
    decorative = GenerateDescriptionRequest(
        figure_kind="decorative", generation_mode="local_model"
    )
    assert local_description(decorative) == ("", "decorative", 0.95)

    chart = GenerateDescriptionRequest(
        figure_kind="chart",
        generation_mode="local_model",
        extracted_text="Velocity over time",
        structured_data={"chart_type": "line chart", "axes": ["s", "m/s"], "trend": "rises"},
    )
    assert "line chart" in local_description(chart)[0]
    table = GenerateDescriptionRequest(
        figure_kind="table",
        generation_mode="local_model",
        structured_data={"rows": 2, "columns": 3},
    )
    assert "2 rows" in local_description(table)[0]
    equation = GenerateDescriptionRequest(
        figure_kind="equation", generation_mode="local_model"
    )
    assert "specialist review" in local_description(equation)[0]
    diagram = GenerateDescriptionRequest(
        figure_kind="diagram",
        generation_mode="local_model",
        extracted_text="Force arrow",
        surrounding_context="Newton's second law",
    )
    assert "Force arrow" in local_description(diagram)[0]


def test_accessibility_ai_policy_validates_service_output(monkeypatch):
    body = GenerateDescriptionRequest(
        figure_kind="chart", generation_mode="institution_ai"
    )
    disabled = replace(get_settings(), external_ai_enabled=False)
    with pytest.raises(HTTPException) as disabled_error:
        asyncio.run(institution_ai_description(body, disabled))
    assert disabled_error.value.status_code == 409

    enabled = replace(
        disabled,
        external_ai_enabled=True,
        accessibility_ai_endpoint="https://ai.example.edu/describe",
        accessibility_ai_token="protected-token",
    )
    FakeAsyncClient.response = FakeResponse(
        {"description": "A rising line chart.", "description_kind": "short", "confidence": 0.7}
    )
    monkeypatch.setattr("institution_server.accessibility.httpx.AsyncClient", FakeAsyncClient)
    assert asyncio.run(institution_ai_description(body, enabled))[0] == "A rising line chart."

    FakeAsyncClient.response = FakeResponse(
        {"description": "unsupported", "description_kind": "script", "confidence": 0.7}
    )
    with pytest.raises(HTTPException) as invalid_kind:
        asyncio.run(institution_ai_description(body, enabled))
    assert invalid_kind.value.status_code == 422
    FakeAsyncClient.response = FakeResponse(
        {"description": "", "description_kind": "short", "confidence": 2}
    )
    with pytest.raises(HTTPException):
        asyncio.run(institution_ai_description(body, enabled))
    FakeAsyncClient.response = FakeResponse(status_code=503)
    with pytest.raises(HTTPException) as unavailable:
        asyncio.run(institution_ai_description(body, enabled))
    assert unavailable.value.status_code == 503


def test_configuration_rejects_unsafe_production_combinations(monkeypatch):
    monkeypatch.setenv("PREDIXALEARN_BOOLEAN_TEST", "true")
    assert _bool("PREDIXALEARN_BOOLEAN_TEST") is True
    monkeypatch.setenv("PREDIXALEARN_BOOLEAN_TEST", "off")
    assert _bool("PREDIXALEARN_BOOLEAN_TEST") is False
    monkeypatch.setenv("PREDIXALEARN_BOOLEAN_TEST", "maybe")
    with pytest.raises(ValueError):
        _bool("PREDIXALEARN_BOOLEAN_TEST")
    monkeypatch.setenv("PREDIXALEARN_CHOICE_TEST", "unknown")
    with pytest.raises(ValueError):
        _choice("PREDIXALEARN_CHOICE_TEST", "a", {"a", "b"})

    base = get_settings()
    unsafe: list[Settings] = [
        replace(base, environment="production"),
        replace(base, environment="production", database_url="postgresql://db/test"),
        replace(
            base,
            environment="production",
            database_url="postgresql://db/test",
            redis_url="redis://redis",
        ),
        replace(
            base,
            environment="production",
            database_url="postgresql://db/test",
            redis_url="redis://redis",
            auth_mode="oidc",
            storage_provider="s3",
            s3_bucket="bucket",
        ),
    ]
    for value in unsafe:
        with pytest.raises(ValueError):
            value.validate()
    production = replace(
        base,
        environment="production",
        database_url="postgresql://db/test",
        redis_url="redis://redis",
        auth_mode="oidc",
        oidc_issuer="https://idp.example",
        oidc_audience="predixalearn-api",
        oidc_jwks_url="https://idp.example/jwks.json",
        oidc_authorization_endpoint="https://idp.example/authorize",
        oidc_token_endpoint="https://idp.example/token",
        oidc_client_id="client",
        oidc_client_secret="protected-client-secret",
        session_secret="protected-session-secret-at-least-32-bytes",
        storage_provider="s3",
        s3_bucket="institution-bucket",
        public_base_url="https://predixalearn.example.edu",
        worker_signing_key="protected-worker-signing-key",
        webhook_signing_secret="protected-webhook-signing-key",
        api_token_secret="protected-api-token-signing-key",
        identity_encryption_key="protected-identity-encryption-key",
    )
    production.validate()
    with pytest.raises(ValueError):
        replace(production, api_token_secret=None).validate()
    with pytest.raises(ValueError):
        replace(
            base,
            external_ai_enabled=True,
            accessibility_ai_endpoint="http://unsafe.example",
            accessibility_ai_token=None,
        ).validate()


def test_storage_provider_contracts_without_real_cloud_credentials(monkeypatch):
    class S3Body:
        def read(self):
            return b"value"

    class S3Client:
        def __init__(self):
            self.calls = []

        def head_bucket(self, **kwargs):
            self.calls.append(("head", kwargs))

        def put_object(self, **kwargs):
            self.calls.append(("put", kwargs))

        def get_object(self, **kwargs):
            self.calls.append(("get", kwargs))
            return {"Body": S3Body()}

        def delete_object(self, **kwargs):
            self.calls.append(("delete", kwargs))

        def generate_presigned_url(self, *args, **kwargs):
            self.calls.append(("url", (args, kwargs)))
            return "https://objects.example/signed"

    s3 = object.__new__(S3ObjectStorage)
    s3.bucket = "bucket"
    s3.client = S3Client()
    s3.healthcheck()
    stored = s3.put("tenant/item", b"value", content_type="text/plain")
    assert stored["size_bytes"] == 5
    assert s3.read("tenant/item") == b"value"
    s3.delete("tenant/item")
    assert s3.download_url("tenant/item") == "https://objects.example/signed"

    class Download:
        def readall(self):
            return b"azure"

    class Container:
        def __init__(self):
            self.calls = []

        def get_container_properties(self):
            self.calls.append("health")

        def upload_blob(self, *args, **kwargs):
            self.calls.append(("put", args, kwargs))

        def download_blob(self, key):
            self.calls.append(("get", key))
            return Download()

        def delete_blob(self, key, **kwargs):
            self.calls.append(("delete", key, kwargs))

    azure = object.__new__(AzureObjectStorage)
    azure.container = Container()
    azure_module = ModuleType("azure")
    azure_storage = ModuleType("azure.storage")
    azure_blob = ModuleType("azure.storage.blob")
    azure_blob.ContentSettings = lambda **values: values
    monkeypatch.setitem(sys.modules, "azure", azure_module)
    monkeypatch.setitem(sys.modules, "azure.storage", azure_storage)
    monkeypatch.setitem(sys.modules, "azure.storage.blob", azure_blob)
    azure.healthcheck()
    assert azure.put("tenant/item", b"azure", content_type="text/plain")["size_bytes"] == 5
    assert azure.read("tenant/item") == b"azure"
    azure.delete("tenant/item")
    assert azure.download_url("tenant/item") is None


def test_webhook_delivery_success_retry_and_missing_resource(
    client, admin_headers, monkeypatch
):
    subscription = client.post(
        "/api/v2/webhooks",
        headers=admin_headers,
        json={
            "title": "Audit receiver",
            "target_url": "https://hooks.example.edu/predixalearn",
            "event_types": ["resource.created"],
        },
    )
    assert subscription.status_code == 201
    created = client.post(
        "/api/v2/archive/documents",
        headers=admin_headers,
        json={
            "title": "Webhook evidence",
            "status": "indexed",
            "content_sha256": "d" * 64,
            "search_text": "safe text",
        },
    )
    assert created.status_code == 201
    FakeAsyncClient.calls = []
    FakeAsyncClient.response = FakeResponse()
    monkeypatch.setattr("institution_server.webhooks.httpx.AsyncClient", FakeAsyncClient)
    settings = replace(get_settings(), webhook_signing_secret="s" * 32)
    delivered = asyncio.run(deliver_once(settings))
    assert delivered["delivered"] >= 1
    assert FakeAsyncClient.calls[0][1]["headers"]["X-PredixaLearn-Signature"].startswith("v1=")

    # No configured secret is always a no-op and cannot leak queued payloads.
    assert asyncio.run(deliver_once(replace(settings, webhook_signing_secret=None))) == {
        "delivered": 0,
        "pending": 0,
        "failed": 0,
    }

    client.post(
        "/api/v2/archive/documents",
        headers=admin_headers,
        json={
            "title": "Retry event",
            "status": "indexed",
            "content_sha256": "a" * 64,
            "search_text": "retry",
        },
    )
    FakeAsyncClient.response = FakeResponse(status_code=503)
    retry = asyncio.run(deliver_once(settings))
    assert retry["pending"] >= 1
    with SessionLocal() as session:
        pending = session.scalars(
            select(WebhookDelivery).where(WebhookDelivery.status == "pending")
        ).all()
        for delivery in pending:
            delivery.attempt = 9
            delivery.next_attempt_at = datetime.now(timezone.utc) - timedelta(seconds=1)
        session.commit()
    exhausted = asyncio.run(deliver_once(settings))
    assert exhausted["failed"] >= 1

    client.post(
        "/api/v2/archive/documents",
        headers=admin_headers,
        json={
            "title": "Missing subscription event",
            "status": "indexed",
            "content_sha256": "b" * 64,
            "search_text": "missing",
        },
    )
    with SessionLocal() as session:
        subscription_resource = session.scalar(
            select(Resource).where(
                Resource.resource_id == subscription.json()["id"]
            )
        )
        assert subscription_resource is not None
        session.delete(subscription_resource)
        session.commit()
    missing = asyncio.run(deliver_once(settings))
    assert missing["failed"] >= 1


def test_webhook_background_loop_stops_cleanly(monkeypatch):
    async def exercise():
        stop = asyncio.Event()

        async def deliver_and_stop(_settings):
            stop.set()
            return {"delivered": 0, "pending": 0, "failed": 0}

        monkeypatch.setattr("institution_server.webhooks.deliver_once", deliver_and_stop)
        await delivery_loop(stop)

    asyncio.run(exercise())


def _oidc_settings() -> Settings:
    return replace(
        get_settings(),
        public_base_url="https://institution.example.edu",
        oidc_authorization_endpoint="https://id.example.edu/authorize",
        oidc_token_endpoint="https://id.example.edu/token",
        oidc_client_id="client-id",
        oidc_client_secret="client-secret",
        oidc_issuer="https://id.example.edu",
        oidc_jwks_url="https://id.example.edu/jwks",
        session_secret="portal-session-secret-at-least-32-bytes",
    )


def test_portal_oidc_login_callback_and_logout(monkeypatch):
    settings = _oidc_settings()
    response = asyncio.run(login(settings))
    assert response.status_code == 302
    assert response.headers["location"].startswith("https://id.example.edu/authorize?")
    assert "predixalearn_oidc_flow=" in response.headers["set-cookie"]
    with pytest.raises(HTTPException):
        _required(replace(settings, oidc_client_id=None), "oidc_client_id")

    private_key = generate_private_key(public_exponent=65537, key_size=2048)
    now = datetime.now(timezone.utc)
    flow = {
        "state": "state-value",
        "nonce": "nonce-value",
        "verifier": "verifier-value",
        "aud": "predixalearn-oidc-flow",
        "iat": now,
        "exp": now + timedelta(minutes=10),
    }
    flow_cookie = jwt.encode(flow, settings.session_secret, algorithm="HS256")
    id_token = jwt.encode(
        {
            "sub": "teacher-1",
            "tenant_id": "tenant-a",
            "roles": ["teacher"],
            "nonce": "nonce-value",
            "iss": settings.oidc_issuer,
            "aud": settings.oidc_client_id,
            "iat": now,
            "exp": now + timedelta(minutes=10),
        },
        private_key,
        algorithm="RS256",
    )
    FakeAsyncClient.response = FakeResponse({"id_token": id_token})
    monkeypatch.setattr("institution_server.portal_auth.httpx.AsyncClient", FakeAsyncClient)
    monkeypatch.setattr(
        "institution_server.portal_auth.jwt.PyJWKClient",
        lambda _url: SimpleNamespace(
            get_signing_key_from_jwt=lambda _token: SimpleNamespace(key=private_key.public_key())
        ),
    )
    request = Request(
        {
            "type": "http",
            "method": "GET",
            "path": "/auth/callback",
            "headers": [(b"cookie", f"predixalearn_oidc_flow={flow_cookie}".encode())],
            "query_string": b"",
            "server": ("testserver", 443),
            "scheme": "https",
        }
    )
    callback_response = asyncio.run(
        callback(request, "authorization-code", "state-value", settings)
    )
    assert callback_response.status_code == 303
    cookies = [
        value.decode()
        for name, value in callback_response.raw_headers
        if name.lower() == b"set-cookie"
    ]
    assert any("predixalearn_session=" in value for value in cookies)
    assert asyncio.run(logout()).status_code == 303


def test_event_stream_returns_tenant_audit_without_document_text(client, admin_headers):
    client.post(
        "/api/v2/archive/documents",
        headers=admin_headers,
        json={
            "title": "Event source",
            "status": "indexed",
            "content_sha256": "e" * 64,
            "search_text": "must not enter event stream",
        },
    )
    principal = Principal(
        subject="teacher-1",
        tenant_id="tenant-a",
        roles=frozenset({"teacher"}),
        permissions=frozenset({"review:read"}),
        academic_unit_ids=frozenset(),
        course_ids=frozenset(),
        enabled_features=frozenset({"archive_review"}),
        csrf_token=None,
    )

    class RequestState:
        calls = 0

        async def is_disconnected(self):
            self.calls += 1
            return self.calls > 1

    async def first_event():
        response = await event_stream(RequestState(), principal, after=0)
        return await anext(response.body_iterator)

    event = asyncio.run(first_event())
    assert "event: audit" in event
    assert "must not enter event stream" not in event


def test_webhook_schema_rejects_loopback_and_permissions(client):
    headers = {"Authorization": f"Bearer {token(roles=['teacher'])}"}
    forbidden = client.post(
        "/api/v2/webhooks",
        headers=headers,
        json={
            "title": "No access",
            "target_url": "https://hooks.example.edu",
            "event_types": ["*"],
        },
    )
    assert forbidden.status_code == 403
    invalid = client.post(
        "/api/v2/webhooks",
        headers={"Authorization": f"Bearer {token()}"},
        json={
            "title": "Unsafe",
            "target_url": "https://127.0.0.1/hook",
            "event_types": ["*"],
        },
    )
    assert invalid.status_code == 422


def test_curriculum_parsers_reject_bad_input_and_report_broken_relationships():
    with pytest.raises(HTTPException):
        _csv_items(b"\xff\xfe")
    with pytest.raises(HTTPException):
        _json_items(b"not-json")
    with pytest.raises(HTTPException):
        _json_items(b'{"unexpected": true}')
    case = _json_items(
        json.dumps(
            {
                "CFItems": [
                    {
                        "identifier": "case-1",
                        "humanCodingScheme": "PHY-1",
                        "fullStatement": "Describe force and acceleration",
                        "uri": "https://case.example/items/1",
                    },
                    "ignored",
                ]
            }
        ).encode()
    )
    assert case[0]["stable_code"] == "PHY-1"
    normalized, errors = validate_import(
        [
            {"stable_code": "PHY-1", "title": "Force", "description": "Force"},
            {"stable_code": "phy-1", "title": "Duplicate", "description": "Duplicate"},
            {"stable_code": "PHY-2", "title": "Motion", "description": "Motion", "parent_code": "MISSING"},
            {"stable_code": "", "title": "Incomplete", "description": ""},
        ]
    )
    assert len(normalized) == 2
    assert {item["code"] for item in errors} == {
        "duplicate_code",
        "required_field",
        "unresolved_reference",
    }


def _lti_registration(client, admin_headers):
    response = client.post(
        "/api/v2/lti/registrations",
        headers=admin_headers,
        json={
            "title": "LTI coverage platform",
            "issuer": "https://lms.coverage.example",
            "client_id": "coverage-client",
            "deployment_id": "coverage-deployment",
            "authorization_endpoint": "https://lms.coverage.example/authorize",
            "token_endpoint": "https://lms.coverage.example/token",
            "jwks_url": "https://lms.coverage.example/jwks",
            "enabled_services": ["deep_linking", "nrps", "ags"],
            "payload": {},
        },
    )
    assert response.status_code == 201
    return response.json()


def test_lti_login_launch_deep_link_and_roster(
    client, admin_headers, monkeypatch
):
    registration = _lti_registration(client, admin_headers)
    login_response = client.get(
        "/api/v2/lti/login",
        params={
            "iss": "https://lms.coverage.example",
            "login_hint": "opaque-login",
            "target_link_uri": "https://institution.example/portal",
            "lti_message_hint": "opaque-message",
            "client_id": "coverage-client",
        },
        follow_redirects=False,
    )
    assert login_response.status_code == 302
    query = parse_qs(urlsplit(login_response.headers["location"]).query)
    state = query["state"][0]
    with SessionLocal() as session:
        nonce_record = session.scalar(select(LtiNonce).where(LtiNonce.state == state))
        assert nonce_record is not None
        nonce = nonce_record.nonce

    assert (
        client.get(
            "/api/v2/lti/login",
            params={
                "iss": "https://lms.coverage.example",
                "login_hint": "x",
                "target_link_uri": "https://institution.example/portal",
                "client_id": "wrong-client",
            },
        ).status_code
        == 400
    )
    assert client.post(
        "/api/v2/lti/launch", data={"id_token": "invalid", "state": "missing"}
    ).status_code == 400

    platform_key = generate_private_key(public_exponent=65537, key_size=2048)
    now = datetime.now(timezone.utc)
    id_token = jwt.encode(
        {
            "iss": "https://lms.coverage.example",
            "aud": "coverage-client",
            "sub": "opaque-user",
            "nonce": nonce,
            "iat": now,
            "exp": now + timedelta(minutes=5),
            "https://purl.imsglobal.org/spec/lti/claim/deployment_id": "coverage-deployment",
            "https://purl.imsglobal.org/spec/lti/claim/roles": ["Instructor"],
            "https://purl.imsglobal.org/spec/lti/claim/context": {"id": "course-1"},
            "https://purl.imsglobal.org/spec/lti/claim/resource_link": {"id": "resource-1"},
        },
        platform_key,
        algorithm="RS256",
    )
    monkeypatch.setattr(
        "institution_server.lti.jwt.PyJWKClient",
        lambda _url: SimpleNamespace(
            get_signing_key_from_jwt=lambda _token: SimpleNamespace(
                key=platform_key.public_key()
            )
        ),
    )
    launched = client.post(
        "/api/v2/lti/launch", data={"id_token": id_token, "state": state}
    )
    assert launched.status_code == 200
    assert launched.json()["tenant_id"] == "tenant-a"
    assert client.post(
        "/api/v2/lti/launch", data={"id_token": id_token, "state": state}
    ).status_code == 400

    no_key = client.post(
        "/api/v2/lti/deep-links/response",
        headers=admin_headers,
        json={
            "registration_id": registration["id"],
            "data": "opaque-data",
            "content_items": [{"type": "ltiResourceLink", "title": "OCR review"}],
        },
    )
    assert no_key.status_code == 503
    signing_key = generate_private_key(public_exponent=65537, key_size=2048)
    signing_pem = signing_key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ).decode()
    monkeypatch.setenv("PREDIXALEARN_LTI_PRIVATE_KEY", signing_pem)
    monkeypatch.setenv("PREDIXALEARN_LTI_KEY_ID", "coverage-key")
    linked = client.post(
        "/api/v2/lti/deep-links/response",
        headers=admin_headers,
        json={
            "registration_id": registration["id"],
            "data": "opaque-data",
            "content_items": [{"type": "ltiResourceLink", "title": "OCR review"}],
        },
    )
    assert linked.status_code == 200
    assert jwt.decode(linked.json()["JWT"], signing_key.public_key(), algorithms=["RS256"], audience="https://lms.coverage.example")[
        "https://purl.imsglobal.org/spec/lti/claim/message_type"
    ] == "LtiDeepLinkingResponse"

    class LtiClient(FakeAsyncClient):
        async def post(self, url, **kwargs):
            del url, kwargs
            return FakeResponse({"access_token": "service-token"})

        async def get(self, url, **kwargs):
            del url, kwargs
            return FakeResponse(
                {
                    "context": {"id": "course-1"},
                    "members": [{"user_id": "opaque-member"}],
                }
            )

    monkeypatch.setattr("institution_server.lti.httpx.AsyncClient", LtiClient)
    roster = client.get(
        "/api/v2/lti/roster",
        headers=admin_headers,
        params={
            "registration_id": registration["id"],
            "memberships_url": "https://lms.coverage.example/nrps/members",
        },
    )
    assert roster.status_code == 200
    assert roster.json()["learner_identifiers_persisted"] is False


def test_catalog_crud_and_service_metadata_endpoints(client, admin_headers):
    assert client.get("/").status_code == 200
    assert client.get("/portal").status_code == 200
    metrics = client.get("/metrics")
    assert metrics.status_code == 200
    assert "predixalearn_institution_uptime_seconds" in metrics.text

    created = client.post(
        "/api/v2/courses",
        headers=admin_headers,
        json={"title": "Physics 101", "status": "active", "payload": {}},
    )
    assert created.status_code == 201
    course = created.json()
    listed = client.get("/api/v2/courses", headers=admin_headers)
    assert listed.status_code == 200
    assert listed.json()["items"][0]["id"] == course["id"]
    fetched = client.get(f"/api/v2/courses/{course['id']}", headers=admin_headers)
    assert fetched.headers["etag"] == course["etag"]
    assert (
        client.patch(
            f"/api/v2/courses/{course['id']}",
            headers=admin_headers,
            json={"title": "Updated physics"},
        ).status_code
        == 412
    )
    updated = client.patch(
        f"/api/v2/courses/{course['id']}",
        headers={**admin_headers, "If-Match": course["etag"]},
        json={"title": "Updated physics"},
    )
    assert updated.status_code == 200
    assert updated.json()["title"] == "Updated physics"
    assert client.get("/api/v2/not-a-collection", headers=admin_headers).status_code == 404
    assert (
        client.post(
            "/api/v2/retention/preview",
            headers=admin_headers,
            json={
                "older_than_days": 1,
                "resource_types": ["unknown_type"],
                "confirm": False,
            },
        ).status_code
        == 422
    )
    assert (
        client.post(
            "/api/v2/retention/apply",
            headers=admin_headers,
            json={
                "older_than_days": 1,
                "resource_types": ["archive_document"],
                "confirm": False,
            },
        ).status_code
        == 409
    )


def _worker_keypair():
    private = Ed25519PrivateKey.generate()
    public = base64.urlsafe_b64encode(
        private.public_key().public_bytes(
            serialization.Encoding.Raw, serialization.PublicFormat.Raw
        )
    ).decode().rstrip("=")
    return private, public


def _worker_request_headers(private, credential, method, path, query=""):
    timestamp = str(int(time.time()))
    target = f"{path}?{query}" if query else path
    signature = private.sign(f"{timestamp}\n{method}\n{target}".encode())
    return {
        "Authorization": f"Bearer {credential}",
        "X-PredixaLearn-Device-Timestamp": timestamp,
        "X-PredixaLearn-Device-Signature": base64.urlsafe_b64encode(signature)
        .decode()
        .rstrip("="),
    }


def test_worker_channel_rejects_unapproved_or_malformed_content(client, admin_headers):
    issued = client.post(
        "/api/v2/workers/enrollment-tokens",
        headers=admin_headers,
        json={"expires_minutes": 10},
    ).json()
    private, public = _worker_keypair()
    enrollment = {
        "enrollment_token": issued["enrollment_token"],
        "name": "Hardened worker",
        "device_public_key": public,
        "capabilities": {"workflows": ["text_recognition"]},
    }
    worker = client.post("/api/v2/workers/enroll", json=enrollment).json()
    second_token = client.post(
        "/api/v2/workers/enrollment-tokens",
        headers=admin_headers,
        json={"expires_minutes": 10},
    ).json()
    assert (
        client.post(
            "/api/v2/workers/enroll",
            json={**enrollment, "enrollment_token": second_token["enrollment_token"]},
        ).status_code
        == 409
    )
    assert (
        client.post(
            "/api/v2/workers/enroll",
            json={
                **enrollment,
                "enrollment_token": "unused",
                "device_public_key": "x" * 32,
            },
        ).status_code
        == 422
    )

    assert (
        client.post(
            "/api/v2/worker-jobs/sources",
            headers=admin_headers,
            files={"file": ("bad.txt", b"text", "text/plain")},
        ).status_code
        == 415
    )
    assert (
        client.post(
            "/api/v2/worker-jobs/sources",
            headers=admin_headers,
            files={"file": ("fake.pdf", b"not a pdf", "application/pdf")},
        ).status_code
        == 422
    )
    source = client.post(
        "/api/v2/worker-jobs/sources",
        headers=admin_headers,
        files={"file": ("paper.pdf", b"%PDF-1.7\nfixture", "application/pdf")},
    ).json()
    invalid_source = {**source, "kind": "result_json"}
    assert (
        client.post(
            "/api/v2/worker-jobs",
            headers=admin_headers,
            json={
                "workflow": "text_recognition",
                "source_artifact": invalid_source,
                "allowed_outputs": ["result_json"],
            },
        ).status_code
        == 422
    )
    job = client.post(
        "/api/v2/worker-jobs",
        headers=admin_headers,
        json={
            "workflow": "text_recognition",
            "source_artifact": source,
            "allowed_outputs": [
                "result_json",
                "markdown",
                "docx",
                "preview",
                "table",
            ],
        },
    ).json()
    next_path = "/api/v2/workers/jobs/next"
    claimed = client.get(
        next_path,
        headers=_worker_request_headers(
            private, worker["credential"], "GET", next_path
        ),
    )
    assert claimed.status_code == 200
    artifact_path = f"/api/v2/workers/jobs/{job['id']}/artifacts"
    invalid_artifacts = [
        ("crop", "bad.png", b"bad", "image/png"),
        ("result_json", "bad.json", b"[]", "application/json"),
        ("markdown", "bad.md", b"\xff", "text/markdown"),
        (
            "docx",
            "bad.docx",
            b"not-zip",
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        ),
        ("preview", "bad.png", b"not-png", "image/png"),
        ("table", "bad.bin", b"bad", "application/octet-stream"),
    ]
    for kind, filename, content, media_type in invalid_artifacts:
        query = f"kind={kind}"
        response = client.post(
            artifact_path,
            params={"kind": kind},
            headers=_worker_request_headers(
                private, worker["credential"], "POST", artifact_path, query
            ),
            files={"file": (filename, content, media_type)},
        )
        assert response.status_code == 422
    artifact = client.post(
        artifact_path,
        params={"kind": "result_json"},
        headers=_worker_request_headers(
            private,
            worker["credential"],
            "POST",
            artifact_path,
            "kind=result_json",
        ),
        files={"file": ("result.json", b'{"ok":true}', "application/json")},
    ).json()
    result_path = f"/api/v2/workers/jobs/{job['id']}/result"
    unknown = {**artifact, "sha256": "f" * 64}
    assert (
        client.post(
            result_path,
            headers=_worker_request_headers(
                private, worker["credential"], "POST", result_path
            ),
            json={"status": "completed", "artifacts": [unknown]},
        ).status_code
        == 422
    )
    completed = client.post(
        result_path,
        headers=_worker_request_headers(
            private, worker["credential"], "POST", result_path
        ),
        json={"status": "completed", "artifacts": [artifact]},
    )
    assert completed.status_code == 200
    assert (
        client.get(
            next_path,
            headers=_worker_request_headers(
                private, worker["credential"], "GET", next_path
            ),
        ).status_code
        == 204
    )


def test_worker_format_and_signing_handlers_cover_approved_types():
    assert worker_aware(datetime(2026, 1, 1)).tzinfo == timezone.utc
    aware = datetime.now(timezone.utc)
    assert worker_aware(aware) is aware
    assert _source_media_type(b"\x89PNG\r\n\x1a\n") == "image/png"
    assert _source_media_type(b"\xff\xd8\xff") == "image/jpeg"
    assert _source_media_type(b"BM") == "image/bmp"
    assert _source_media_type(b"II*\x00") == "image/tiff"
    assert _source_media_type(b"RIFF0000WEBP") == "image/webp"
    assert _source_media_type(b"unknown") is None

    _validate_worker_output("markdown", b"# Verified", "text/markdown")
    _validate_worker_output("preview", b"\x89PNG\r\n\x1a\ncontent", "image/png")
    _validate_worker_output("table", b"a,b\n1,2", "text/csv")
    docx = io.BytesIO()
    with zipfile.ZipFile(docx, "w") as archive:
        archive.writestr("[Content_Types].xml", "<Types/>")
    _validate_worker_output(
        "docx",
        docx.getvalue(),
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    )

    settings = replace(get_settings(), worker_signing_key=None, test_jwt_secret=None)
    with pytest.raises(HTTPException):
        _worker_secret(settings)
    key = Ed25519PrivateKey.generate()
    pem = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ).decode()
    assert isinstance(
        _ed25519_key(replace(settings, worker_signing_key=pem)), Ed25519PrivateKey
    )
    raw = key.private_bytes(
        serialization.Encoding.Raw,
        serialization.PrivateFormat.Raw,
        serialization.NoEncryption(),
    )
    assert isinstance(
        _ed25519_key(
            replace(
                settings,
                worker_signing_key=base64.b64encode(raw).decode(),
            )
        ),
        Ed25519PrivateKey,
    )


def test_expired_unassigned_worker_sources_are_cleaned_without_touching_current_source():
    storage = create_storage()
    expired_storage = storage.put(
        "tenant-a/workers/sources/expired", b"expired", content_type="application/pdf"
    )
    current_storage = storage.put(
        "tenant-a/workers/sources/current", b"current", content_type="application/pdf"
    )
    now = datetime.now(timezone.utc)
    with SessionLocal() as session:
        expired = Resource(
            tenant_id="tenant-a",
            resource_type="worker_source",
            resource_id="expired-source",
            title="Expired source",
            status="staged",
            access_classification="private",
            content_sha256="a" * 64,
            payload={
                "storage": expired_storage,
                "expires_at": (now - timedelta(minutes=1)).isoformat(),
            },
            immutable=True,
            created_by="admin",
            updated_by="admin",
        )
        current = Resource(
            tenant_id="tenant-a",
            resource_type="worker_source",
            resource_id="current-source",
            title="Current source",
            status="staged",
            access_classification="private",
            content_sha256="b" * 64,
            payload={
                "storage": current_storage,
                "expires_at": (now + timedelta(hours=1)).isoformat(),
            },
            immutable=True,
            created_by="admin",
            updated_by="admin",
        )
        session.add_all([expired, current])
        session.commit()
        _expire_staged_sources(session, "tenant-a")
        session.refresh(expired)
        session.refresh(current)
        assert expired.status == "expired"
        assert current.status == "staged"
    with pytest.raises(FileNotFoundError):
        storage.read("tenant-a/workers/sources/expired")
    assert storage.read("tenant-a/workers/sources/current") == b"current"
