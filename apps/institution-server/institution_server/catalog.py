"""Common CRUD surface for scoped institutional catalog resources."""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request, Response
from sqlalchemy.orm import Session

from .api import db_session, service_for
from .schemas import ResourceCreate, ResourcePatch
from .security import Principal, current_principal
from .service import serialize_resource

router = APIRouter(prefix="/api/v2", tags=["institution-catalog"])

COLLECTIONS: dict[str, tuple[str, str, str]] = {
    "institutions": ("institution", "archive:read", "*"),
    "memberships": ("membership", "archive:read", "*"),
    "academic-units": ("academic_unit", "archive:read", "*"),
    "courses": ("course", "archive:read", "archive:write"),
    "cohorts": ("cohort", "archive:read", "archive:write"),
    "assessments": ("assessment", "archive:read", "archive:write"),
    "evidence-versions": ("evidence_version", "archive:read", "*"),
    "analysis-overlays": ("analysis_overlay", "archive:read", "*"),
    "artifacts": ("artifact", "archive:read", "archive:write"),
    "review-comments": ("review_comment", "review:read", "*"),
    "saved-filters": ("saved_filter", "archive:read", "archive:read"),
    "figures": ("figure", "accessibility:read", "archive:write"),
    "answer-responses": ("answer_response", "answer:read", "answer:write"),
    "feedback": ("feedback", "answer:read", "answer:write"),
    "moderation": ("moderation", "answer:read", "answer:moderate"),
    "learning-objectives": ("learning_objective", "curriculum:read", "curriculum:write"),
    "rubric-versions": ("rubric_version", "rubric:read", "rubric:write"),
    "objective-mappings": ("objective_mapping", "curriculum:read", "curriculum:write"),
    "answer-scripts": ("answer_script", "answer:read", "answer:write"),
    "accessibility-descriptions": (
        "accessibility_description",
        "accessibility:read",
        "accessibility:verify",
    ),
    "datasets": ("dataset_project", "dataset:read", "dataset:write"),
    "dataset-items": ("dataset_item", "dataset:read", "dataset:write"),
    "dataset-releases": ("dataset_release", "dataset:read", "*"),
    "webhooks": ("webhook_subscription", "integration:read", "integration:write"),
    "worker-jobs": ("worker_job", "worker:read", "worker:write"),
    "lti-registrations": ("lti_registration", "integration:read", "integration:write"),
    "corrections": ("correction_overlay", "teaching:read", "teaching:write"),
    "taxonomies": ("taxonomy_version", "teaching:read", "teaching:write"),
    "taxonomy-nodes": ("taxonomy_node", "teaching:read", "teaching:write"),
    "syllabi": ("syllabus_version", "teaching:read", "teaching:write"),
    "syllabus-mappings": ("syllabus_mapping", "teaching:read", "teaching:write"),
    "duplicate-decisions": ("duplicate_decision", "teaching:read", "teaching:write"),
    "question-bank": ("question_bank_item", "teaching:read", "teaching:write"),
    "comparisons": ("comparison_report", "teaching:read", "teaching:write"),
    "revision-packs": ("revision_pack", "revision:read", "revision:write"),
    "revision-progress": ("student_revision_progress", "revision:read", "revision:progress"),
}


def _collection(name: str) -> tuple[str, str, str]:
    value = COLLECTIONS.get(name)
    if value is None:
        raise HTTPException(status_code=404, detail={"code": "collection_not_found", "message": "Institution collection was not found"})
    return value


def _permission(principal: Principal, permission: str) -> None:
    if not principal.allows(permission):
        raise HTTPException(status_code=403, detail={"code": "permission_denied", "message": f"Permission {permission} is required"})


@router.post("/{collection}", status_code=201)
async def create_catalog_resource(
    collection: str,
    body: ResourceCreate,
    response: Response,
    request: Request,
    session: Annotated[Session, Depends(db_session)],
    principal: Annotated[Principal, Depends(current_principal)],
) -> dict[str, Any]:
    resource_type, _read, write = _collection(collection)
    _permission(principal, write)
    resource = service_for(session, principal, request).create(resource_type, body)
    response.headers["ETag"] = f'W/"{resource.version}"'
    return serialize_resource(resource)


@router.get("/{collection}")
async def list_catalog_resources(
    collection: str,
    request: Request,
    session: Annotated[Session, Depends(db_session)],
    principal: Annotated[Principal, Depends(current_principal)],
    limit: Annotated[int, Query(ge=1, le=100)] = 25,
    cursor: str | None = None,
    status_filter: Annotated[str | None, Query(alias="status")] = None,
) -> dict[str, Any]:
    resource_type, read, _write = _collection(collection)
    _permission(principal, read)
    return service_for(session, principal, request).list(
        resource_type, limit=limit, cursor=cursor, status=status_filter
    )


@router.get("/{collection}/{resource_id}")
async def get_catalog_resource(
    collection: str,
    resource_id: str,
    response: Response,
    request: Request,
    session: Annotated[Session, Depends(db_session)],
    principal: Annotated[Principal, Depends(current_principal)],
) -> dict[str, Any]:
    resource_type, read, _write = _collection(collection)
    _permission(principal, read)
    resource = service_for(session, principal, request).get(resource_type, resource_id)
    response.headers["ETag"] = f'W/"{resource.version}"'
    return serialize_resource(resource)


@router.patch("/{collection}/{resource_id}")
async def patch_catalog_resource(
    collection: str,
    resource_id: str,
    body: ResourcePatch,
    response: Response,
    request: Request,
    session: Annotated[Session, Depends(db_session)],
    principal: Annotated[Principal, Depends(current_principal)],
    if_match: Annotated[str | None, Header(alias="If-Match")] = None,
) -> dict[str, Any]:
    resource_type, _read, write = _collection(collection)
    _permission(principal, write)
    service = service_for(session, principal, request)
    resource = service.patch(service.get(resource_type, resource_id), body, if_match)
    response.headers["ETag"] = f'W/"{resource.version}"'
    return serialize_resource(resource)
