from __future__ import annotations

from conftest import token


def headers(*, subject: str, roles: list[str], course_ids: list[str] | None = None):
    return {
        "Authorization": f"Bearer {token(subject, roles=roles, course_ids=course_ids)}"
    }


def test_teacher_catalog_and_dashboard_are_tenant_and_course_scoped(client):
    teacher = headers(subject="teacher-1", roles=["teacher"], course_ids=["course-a"])
    correction = client.post(
        "/api/v2/corrections",
        headers=teacher,
        json={
            "title": "Line correction",
            "status": "draft",
            "course_id": "course-a",
            "content_sha256": "a" * 64,
            "payload": {"source_hash": "b" * 64, "target_id": "p1-l1"},
        },
    )
    assert correction.status_code == 201

    dashboard = client.get("/api/v2/teacher/dashboard", headers=teacher)
    assert dashboard.status_code == 200
    assert dashboard.json()["counts"]["pending_corrections"] == 1
    assert dashboard.json()["scope"]["course_ids"] == ["course-a"]


def test_student_revision_endpoint_exposes_only_approved_safe_fields(client):
    admin = headers(subject="admin", roles=["institution_admin"])
    created = client.post(
        "/api/v2/revision-packs",
        headers=admin,
        json={
            "title": "Forces revision",
            "status": "approved",
            "course_id": "course-a",
            "content_sha256": "c" * 64,
            "payload": {
                "introduction": "Review force and acceleration.",
                "teacher_notes": "Do not reveal this note.",
                "hidden_marks": [5],
                "items": [
                    {
                        "question_id": "q-1",
                        "text": "Explain force.",
                        "topic": "Mechanics",
                        "marks": 5,
                        "teacher_approved": True,
                        "provenance": {"paper": "paper-1", "page": 1},
                    },
                    {
                        "question_id": "q-draft",
                        "text": "Unpublished question",
                        "teacher_approved": False,
                    },
                ],
            },
        },
    )
    assert created.status_code == 201

    student = headers(subject="student-1", roles=["student"], course_ids=["course-a"])
    response = client.get("/api/v2/student/revision-packs", headers=student)
    assert response.status_code == 200
    payload = response.json()["items"][0]["payload"]
    assert payload["items"] == [
        {
            "question_id": "q-1",
            "text": "Explain force.",
            "topic": "Mechanics",
            "provenance": {"paper": "paper-1", "page": 1},
        }
    ]
    assert "teacher_notes" not in payload
    assert "hidden_marks" not in payload
    assert "marks" not in payload["items"][0]

    other_course = headers(subject="student-2", roles=["student"], course_ids=["course-b"])
    assert client.get("/api/v2/student/revision-packs", headers=other_course).json()["items"] == []


def test_student_revision_progress_is_private_to_the_authenticated_learner(client):
    student_one = headers(subject="student-1", roles=["student"], course_ids=["course-a"])
    created = client.post(
        "/api/v2/revision-progress",
        headers=student_one,
        json={
            "title": "Mechanics progress",
            "status": "active",
            "course_id": "course-a",
            "access_classification": "institution",
            "content_sha256": "d" * 64,
            "payload": {"pack_id": "pack-1", "completed_item_ids": ["q-1"]},
        },
    )
    assert created.status_code == 201
    assert created.json()["access_classification"] == "private"
    assert len(client.get("/api/v2/revision-progress", headers=student_one).json()["items"]) == 1

    student_two = headers(subject="student-2", roles=["student"], course_ids=["course-a"])
    assert client.get("/api/v2/revision-progress", headers=student_two).json()["items"] == []


def test_student_revision_dashboard_is_local_static_and_accessible(client):
    page = client.get("/revision")
    assert page.status_code == 200
    assert "Teacher-approved practice" in page.text
    assert 'aria-live="polite"' in page.text
    script = client.get("/static/revision.js")
    assert script.status_code == 200
    assert "/api/v2/student/revision-packs" in script.text
