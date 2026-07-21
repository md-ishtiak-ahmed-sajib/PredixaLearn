"""Tenant-scoped teacher dashboard and student-safe revision delivery."""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query, Request
from sqlalchemy.orm import Session

from .api import db_session, service_for
from .security import Principal, require

router = APIRouter(prefix="/api/v2", tags=["teaching-workspace"])


@router.get("/teacher/dashboard")
async def teacher_dashboard(
    request: Request,
    session: Annotated[Session, Depends(db_session)],
    principal: Annotated[Principal, Depends(require("teaching:read"))],
) -> dict[str, Any]:
    service = service_for(session, principal, request)
    resources = {
        name: service.list(kind, limit=100, cursor=None, status=status)["items"]
        for name, kind, status in (
            ("pending_corrections", "correction_overlay", "draft"),
            ("pending_question_bank", "question_bank_item", "reviewed"),
            ("duplicate_queue", "duplicate_decision", "draft"),
            ("approved_revision_packs", "revision_pack", "approved"),
            ("comparison_reports", "comparison_report", None),
        )
    }
    return {
        "tenant_id": principal.tenant_id,
        "scope": {
            "academic_unit_ids": sorted(principal.academic_unit_ids),
            "course_ids": sorted(principal.course_ids),
        },
        "counts": {name: len(items) for name, items in resources.items()},
        "queues": resources,
    }


def _student_safe_pack(item: dict[str, Any]) -> dict[str, Any]:
    payload = item.get("payload") if isinstance(item.get("payload"), dict) else {}
    safe_items = []
    for raw in payload.get("items", []):
        if not isinstance(raw, dict) or not raw.get("teacher_approved", False):
            continue
        safe_items.append(
            {
                key: raw.get(key)
                for key in (
                    "question_id",
                    "text",
                    "topic",
                    "objective",
                    "difficulty",
                    "language",
                    "provenance",
                    "accessibility_description",
                )
                if key in raw
            }
        )
    return {
        "id": item["id"],
        "title": item["title"],
        "status": "approved",
        "course_id": item.get("course_id"),
        "updated_at": item["updated_at"],
        "payload": {
            "introduction": payload.get("introduction"),
            "objective_coverage": payload.get("objective_coverage", []),
            "items": safe_items,
            "statement": "Teacher-approved historical practice; not a future-exam prediction.",
        },
    }


@router.get("/student/revision-packs")
async def student_revision_packs(
    request: Request,
    session: Annotated[Session, Depends(db_session)],
    principal: Annotated[Principal, Depends(require("revision:read"))],
    limit: Annotated[int, Query(ge=1, le=100)] = 25,
    cursor: str | None = None,
) -> dict[str, Any]:
    page = service_for(session, principal, request).list(
        "revision_pack", limit=limit, cursor=cursor, status="approved"
    )
    return {
        "items": [_student_safe_pack(item) for item in page["items"]],
        "next_cursor": page["next_cursor"],
        "learner_id": principal.learner_id,
    }
