from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone

os.environ["PREDIXALEARN_INSTITUTION_ENV"] = "test"
os.environ["PREDIXALEARN_DATABASE_URL"] = "sqlite:///:memory:"
os.environ["PREDIXALEARN_AUTH_MODE"] = "test"
os.environ["PREDIXALEARN_TEST_JWT_SECRET"] = "institution-test-secret-at-least-32-bytes"
os.environ["PREDIXALEARN_STORAGE_ROOT"] = "./apps/institution-server/.test-storage"

import jwt
import pytest
from fastapi.testclient import TestClient

from institution_server.database import Base, engine
from institution_server.main import create_app


def token(
    subject: str = "teacher-1",
    *,
    tenant_id: str = "tenant-a",
    roles: list[str] | None = None,
    course_ids: list[str] | None = None,
    features: list[str] | None = None,
) -> str:
    now = datetime.now(timezone.utc)
    claims = {
            "sub": subject,
            "tenant_id": tenant_id,
            "roles": roles or ["institution_admin"],
            "course_ids": course_ids or [],
            "aud": "predixalearn-institution",
            "iat": now,
            "exp": now + timedelta(hours=1),
        }
    if features is not None:
        claims["features"] = features
    return jwt.encode(
        claims,
        "institution-test-secret-at-least-32-bytes",
        algorithm="HS256",
    )


@pytest.fixture
def client():
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    with TestClient(create_app()) as test_client:
        yield test_client


@pytest.fixture
def admin_headers():
    return {"Authorization": f"Bearer {token()}"}
