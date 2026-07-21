"""Tenant-scoped institutional REST API."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from typing import Annotated, Any, TypeVar

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request, Response
from sqlalchemy import select
from sqlalchemy.orm import Session

from .config import Settings, get_settings
from .database import session_scope
from .models import AuditEvent, IdempotencyRecord, Resource
from .schemas import (
    AccessibilityDescriptionCreate,
    AnswerScriptCreate,
    ArchiveDocumentCreate,
    CommentCreate,
    CurriculumVersionCreate,
    DatasetItemCreate,
    DatasetProjectCreate,
    DatasetReviewCreate,
    EvidenceVersionCreate,
    LearningObjectiveCreate,
    ObjectiveMappingCreate,
    ResourceCreate,
    ResourcePatch,
    ReviewAssignmentUpdate,
    ReviewDecisionCreate,
    ReviewTaskCreate,
    RubricVersionCreate,
    SavedFilterCreate,
    SearchRequest,
    TeacherApproval,
    VerificationCreate,
    WebhookSubscriptionCreate,
)
from .security import Principal, require
from .service import ResourceService, serialize_resource

router = APIRouter(prefix="/api/v2")
CreateModel = TypeVar("CreateModel", bound=ResourceCreate)


def db_session():
    yield from session_scope()


def service_for(session: Session, principal: Principal, request: Request) -> ResourceService:
    return ResourceService(session, principal, str(request.state.request_id))


def _created(resource, response: Response) -> dict[str, Any]:
    response.headers["ETag"] = f'W/"{resource.version}"'
    response.headers["Location"] = f"/api/v2/{resource.resource_type.replace('_', '-')}/{resource.resource_id}"
    return serialize_resource(resource)


def _create_resource(
    resource_type: str,
    body: ResourceCreate,
    response: Response,
    request: Request,
    session: Session,
    principal: Principal,
    *,
    immutable: bool = False,
) -> dict[str, Any]:
    service = service_for(session, principal, request)
    idempotency_key = request.headers.get("Idempotency-Key")
    request_hash = hashlib.sha256(
        json.dumps(
            {"resource_type": resource_type, "body": body.model_dump(mode="json")},
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
    ).hexdigest()
    if idempotency_key:
        if not 8 <= len(idempotency_key) <= 200:
            raise HTTPException(
                status_code=422,
                detail={
                    "code": "invalid_idempotency_key",
                    "message": "Idempotency-Key must contain 8-200 characters",
                },
            )
        existing = session.scalar(
            select(IdempotencyRecord).where(
                IdempotencyRecord.tenant_id == principal.tenant_id,
                IdempotencyRecord.endpoint == request.url.path,
                IdempotencyRecord.idempotency_key == idempotency_key,
            )
        )
        if existing:
            if existing.request_hash != request_hash:
                raise HTTPException(
                    status_code=409,
                    detail={
                        "code": "idempotency_conflict",
                        "message": "Idempotency-Key was reused with a different request",
                    },
                )
            response.headers["X-Idempotent-Replay"] = "true"
            return existing.response_body
    resource = service.create(
        resource_type, body, immutable=immutable
    )
    response_body = _created(resource, response)
    if idempotency_key:
        session.add(
            IdempotencyRecord(
                tenant_id=principal.tenant_id,
                endpoint=request.url.path,
                idempotency_key=idempotency_key,
                request_hash=request_hash,
                response_status=201,
                response_body=response_body,
            )
        )
        session.commit()
    return response_body


def _list_resources(
    resource_type: str,
    request: Request,
    session: Session,
    principal: Principal,
    limit: int,
    cursor: str | None,
    status_filter: str | None,
) -> dict[str, Any]:
    return service_for(session, principal, request).list(
        resource_type, limit=limit, cursor=cursor, status=status_filter
    )


def _read_resource(
    resource_type: str,
    resource_id: str,
    response: Response,
    request: Request,
    session: Session,
    principal: Principal,
) -> dict[str, Any]:
    resource = service_for(session, principal, request).get(resource_type, resource_id)
    response.headers["ETag"] = f'W/"{resource.version}"'
    return serialize_resource(resource)


def _patch_resource(
    resource_type: str,
    resource_id: str,
    body: ResourcePatch,
    response: Response,
    request: Request,
    session: Session,
    principal: Principal,
    if_match: str | None,
) -> dict[str, Any]:
    service = service_for(session, principal, request)
    resource = service.patch(service.get(resource_type, resource_id), body, if_match)
    response.headers["ETag"] = f'W/"{resource.version}"'
    return serialize_resource(resource)


@router.get("/me", tags=["identity"])
async def me(
    principal: Annotated[Principal, Depends(require("archive:read"))],
) -> dict[str, Any]:
    return {
        "subject": principal.subject,
        "tenant_id": principal.tenant_id,
        "roles": sorted(principal.roles),
        "permissions": sorted(principal.permissions),
        "academic_unit_ids": sorted(principal.academic_unit_ids),
        "course_ids": sorted(principal.course_ids),
        "enabled_features": sorted(principal.enabled_features),
        "csrf_token": principal.csrf_token,
    }


@router.get("/features", tags=["operations"])
async def enabled_features(
    settings: Annotated[Settings, Depends(get_settings)],
) -> dict[str, list[str]]:
    """Expose rollout state without exposing tenant configuration or secrets."""

    return {"enabled": sorted(settings.enabled_features)}


@router.post("/archive/documents", status_code=201, tags=["archive"])
async def create_archive_document(
    body: ArchiveDocumentCreate,
    response: Response,
    request: Request,
    session: Annotated[Session, Depends(db_session)],
    principal: Annotated[Principal, Depends(require("archive:write"))],
) -> dict[str, Any]:
    return _create_resource("archive_document", body, response, request, session, principal)


@router.get("/archive/documents", tags=["archive"])
async def list_archive_documents(
    request: Request,
    session: Annotated[Session, Depends(db_session)],
    principal: Annotated[Principal, Depends(require("archive:read"))],
    limit: Annotated[int, Query(ge=1, le=100)] = 25,
    cursor: str | None = None,
    status_filter: Annotated[str | None, Query(alias="status")] = None,
) -> dict[str, Any]:
    return _list_resources(
        "archive_document", request, session, principal, limit, cursor, status_filter
    )


@router.get("/archive/documents/{resource_id}", tags=["archive"])
async def get_archive_document(
    resource_id: str,
    response: Response,
    request: Request,
    session: Annotated[Session, Depends(db_session)],
    principal: Annotated[Principal, Depends(require("archive:read"))],
) -> dict[str, Any]:
    return _read_resource(
        "archive_document", resource_id, response, request, session, principal
    )


@router.patch("/archive/documents/{resource_id}", tags=["archive"])
async def patch_archive_document(
    resource_id: str,
    body: ResourcePatch,
    response: Response,
    request: Request,
    session: Annotated[Session, Depends(db_session)],
    principal: Annotated[Principal, Depends(require("archive:write"))],
    if_match: Annotated[str | None, Header(alias="If-Match")] = None,
) -> dict[str, Any]:
    return _patch_resource(
        "archive_document", resource_id, body, response, request, session, principal, if_match
    )


@router.post("/archive/documents/{document_id}/evidence", status_code=201, tags=["archive"])
async def create_evidence_version(
    document_id: str,
    body: EvidenceVersionCreate,
    response: Response,
    request: Request,
    session: Annotated[Session, Depends(db_session)],
    principal: Annotated[Principal, Depends(require("archive:write"))],
) -> dict[str, Any]:
    service = service_for(session, principal, request)
    service.get("archive_document", document_id)
    body.parent_id = document_id
    return _created(service.create("evidence_version", body, immutable=True), response)


@router.post("/search", tags=["archive"])
async def search_archive(
    body: SearchRequest,
    request: Request,
    session: Annotated[Session, Depends(db_session)],
    principal: Annotated[Principal, Depends(require("archive:read"))],
    settings: Annotated[Settings, Depends(get_settings)],
) -> dict[str, Any]:
    if body.semantic and not settings.semantic_search_enabled:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "semantic_search_disabled",
                "message": "Semantic indexing is disabled by institution policy",
            },
        )
    return service_for(session, principal, request).search(
        query=body.query,
        resource_types=body.resource_types,
        course_id=body.course_id,
        academic_unit_id=body.academic_unit_id,
        statuses=body.statuses,
        assessment_id=body.assessment_id,
        access_classifications=body.access_classifications,
        reviewer_id=body.reviewer_id,
        created_from=body.created_from,
        created_to=body.created_to,
        quality_min=body.quality_min,
        warning_max=body.warning_max,
        limit=body.limit,
        cursor=body.cursor,
        semantic=body.semantic,
    )


@router.post("/reviews", status_code=201, tags=["review"])
async def create_review_task(
    body: ReviewTaskCreate,
    response: Response,
    request: Request,
    session: Annotated[Session, Depends(db_session)],
    principal: Annotated[Principal, Depends(require("review:write"))],
) -> dict[str, Any]:
    return _create_resource("review_task", body, response, request, session, principal)


@router.post("/saved-filters", status_code=201, tags=["archive"])
async def create_saved_filter(
    body: SavedFilterCreate,
    response: Response,
    request: Request,
    session: Annotated[Session, Depends(db_session)],
    principal: Annotated[Principal, Depends(require("archive:read"))],
) -> dict[str, Any]:
    return _create_resource("saved_filter", body, response, request, session, principal)


@router.get("/saved-filters", tags=["archive"])
async def list_saved_filters(
    request: Request,
    session: Annotated[Session, Depends(db_session)],
    principal: Annotated[Principal, Depends(require("archive:read"))],
    limit: Annotated[int, Query(ge=1, le=100)] = 25,
    cursor: str | None = None,
) -> dict[str, Any]:
    return _list_resources(
        "saved_filter", request, session, principal, limit, cursor, "active"
    )


@router.get("/reviews", tags=["review"])
async def list_review_tasks(
    request: Request,
    session: Annotated[Session, Depends(db_session)],
    principal: Annotated[Principal, Depends(require("review:read"))],
    limit: Annotated[int, Query(ge=1, le=100)] = 25,
    cursor: str | None = None,
    status_filter: Annotated[str | None, Query(alias="status")] = None,
) -> dict[str, Any]:
    return _list_resources("review_task", request, session, principal, limit, cursor, status_filter)


@router.patch("/reviews/{review_id}/assignment", tags=["review"])
async def update_review_assignment(
    review_id: str,
    body: ReviewAssignmentUpdate,
    response: Response,
    request: Request,
    session: Annotated[Session, Depends(db_session)],
    principal: Annotated[Principal, Depends(require("review:write"))],
    if_match: Annotated[str | None, Header(alias="If-Match")] = None,
) -> dict[str, Any]:
    service = service_for(session, principal, request)
    review = service.get("review_task", review_id)
    payload = dict(review.payload)
    payload.update(
        {
            "assigned_to": sorted(set(body.assigned_to)),
            "due_at": body.due_at.isoformat() if body.due_at else None,
            "moderation_required": body.moderation_required,
        }
    )
    review = service.patch(review, ResourcePatch(payload=payload), if_match)
    response.headers["ETag"] = f'W/"{review.version}"'
    return serialize_resource(review)


@router.post("/reviews/{review_id}/comments", status_code=201, tags=["review"])
async def add_review_comment(
    review_id: str,
    body: CommentCreate,
    response: Response,
    request: Request,
    session: Annotated[Session, Depends(db_session)],
    principal: Annotated[Principal, Depends(require("review:write"))],
) -> dict[str, Any]:
    service = service_for(session, principal, request)
    review = service.get("review_task", review_id)
    create = ResourceCreate(
        title=f"Comment on {review.title}",
        status="published",
        parent_id=review_id,
        academic_unit_id=review.academic_unit_id,
        course_id=review.course_id,
        access_classification=review.access_classification,
        payload={"text": body.text, "mentions": body.mentions},
    )
    return _created(service.create("review_comment", create, immutable=True), response)


@router.post("/reviews/{review_id}/decision", tags=["review"])
async def decide_review(
    review_id: str,
    body: ReviewDecisionCreate,
    response: Response,
    request: Request,
    session: Annotated[Session, Depends(db_session)],
    principal: Annotated[Principal, Depends(require("review:write"))],
    if_match: Annotated[str | None, Header(alias="If-Match")] = None,
) -> dict[str, Any]:
    service = service_for(session, principal, request)
    review = service.get("review_task", review_id)
    service.require_match(review, if_match)
    review = service.transition(
        review,
        body.decision,
        {"rationale": body.rationale, "decided_by": principal.subject},
    )
    response.headers["ETag"] = f'W/"{review.version}"'
    return serialize_resource(review)


@router.post("/curricula/versions", status_code=201, tags=["curriculum"])
async def create_curriculum_version(
    body: CurriculumVersionCreate,
    response: Response,
    request: Request,
    session: Annotated[Session, Depends(db_session)],
    principal: Annotated[Principal, Depends(require("curriculum:write"))],
) -> dict[str, Any]:
    return _create_resource("curriculum_version", body, response, request, session, principal)


@router.get("/curricula/versions", tags=["curriculum"])
async def list_curriculum_versions(
    request: Request,
    session: Annotated[Session, Depends(db_session)],
    principal: Annotated[Principal, Depends(require("curriculum:read"))],
    limit: Annotated[int, Query(ge=1, le=100)] = 25,
    cursor: str | None = None,
    status_filter: Annotated[str | None, Query(alias="status")] = None,
) -> dict[str, Any]:
    return _list_resources(
        "curriculum_version", request, session, principal, limit, cursor, status_filter
    )


@router.post("/curricula/versions/{version_id}/publish", tags=["curriculum"])
async def publish_curriculum_version(
    version_id: str,
    response: Response,
    request: Request,
    session: Annotated[Session, Depends(db_session)],
    principal: Annotated[Principal, Depends(require("curriculum:write"))],
    if_match: Annotated[str | None, Header(alias="If-Match")] = None,
) -> dict[str, Any]:
    service = service_for(session, principal, request)
    resource = service.get("curriculum_version", version_id)
    service.require_match(resource, if_match)
    resource = service.transition(resource, "published", {})
    response.headers["ETag"] = f'W/"{resource.version}"'
    return serialize_resource(resource)


@router.post("/curricula/versions/{version_id}/objectives", status_code=201, tags=["curriculum"])
async def create_learning_objective(
    version_id: str,
    body: LearningObjectiveCreate,
    response: Response,
    request: Request,
    session: Annotated[Session, Depends(db_session)],
    principal: Annotated[Principal, Depends(require("curriculum:write"))],
) -> dict[str, Any]:
    service = service_for(session, principal, request)
    version = service.get("curriculum_version", version_id)
    if version.status != "draft":
        raise HTTPException(status_code=409, detail={"code": "immutable_resource", "message": "Published curriculum versions cannot receive new objectives"})
    body.parent_id = version_id
    return _created(service.create("learning_objective", body), response)


@router.post("/rubrics/versions", status_code=201, tags=["rubric"])
async def create_rubric_version(
    body: RubricVersionCreate,
    response: Response,
    request: Request,
    session: Annotated[Session, Depends(db_session)],
    principal: Annotated[Principal, Depends(require("rubric:write"))],
) -> dict[str, Any]:
    return _create_resource("rubric_version", body, response, request, session, principal)


@router.post("/rubrics/versions/{version_id}/publish", tags=["rubric"])
async def publish_rubric_version(
    version_id: str,
    response: Response,
    request: Request,
    session: Annotated[Session, Depends(db_session)],
    principal: Annotated[Principal, Depends(require("rubric:write"))],
    if_match: Annotated[str | None, Header(alias="If-Match")] = None,
) -> dict[str, Any]:
    service = service_for(session, principal, request)
    resource = service.get("rubric_version", version_id)
    service.require_match(resource, if_match)
    resource = service.transition(resource, "published", {})
    response.headers["ETag"] = f'W/"{resource.version}"'
    return serialize_resource(resource)


@router.post("/objective-mappings", status_code=201, tags=["curriculum"])
async def create_objective_mapping(
    body: ObjectiveMappingCreate,
    response: Response,
    request: Request,
    session: Annotated[Session, Depends(db_session)],
    principal: Annotated[Principal, Depends(require("curriculum:write"))],
) -> dict[str, Any]:
    service = service_for(session, principal, request)
    service.get("curriculum_version", body.curriculum_version_id)
    objective = service.get("learning_objective", body.objective_id)
    if objective.parent_id != body.curriculum_version_id:
        raise HTTPException(
            status_code=422,
            detail={
                "code": "objective_version_mismatch",
                "message": "Objective does not belong to the selected curriculum version",
            },
        )
    source = session.scalar(
        service._query().where(Resource.resource_id == body.source_resource_id)  # noqa: SLF001
    )
    if source is None:
        raise HTTPException(
            status_code=404,
            detail={"code": "mapping_source_not_found", "message": "Mapping source was not found"},
        )
    return _created(service.create("objective_mapping", body), response)


@router.post("/answer-scripts", status_code=201, tags=["answer-scripts"])
async def create_answer_script(
    body: AnswerScriptCreate,
    response: Response,
    request: Request,
    session: Annotated[Session, Depends(db_session)],
    principal: Annotated[Principal, Depends(require("answer:write"))],
) -> dict[str, Any]:
    service = service_for(session, principal, request)
    service.get("assessment", body.assessment_id)
    rubric = service.get("rubric_version", body.rubric_version_id)
    if rubric.status != "published":
        raise HTTPException(
            status_code=409,
            detail={
                "code": "published_rubric_required",
                "message": "Answer scripts require an immutable published rubric version",
            },
        )
    return _created(service.create("answer_script", body), response)


@router.post("/answer-scripts/{script_id}/approve", tags=["answer-scripts"])
async def approve_answer_script(
    script_id: str,
    body: TeacherApproval,
    response: Response,
    request: Request,
    session: Annotated[Session, Depends(db_session)],
    principal: Annotated[Principal, Depends(require("answer:write"))],
    if_match: Annotated[str | None, Header(alias="If-Match")] = None,
) -> dict[str, Any]:
    service = service_for(session, principal, request)
    script = service.get("answer_script", script_id)
    service.require_match(script, if_match)
    if script.status != "ready_for_review":
        raise HTTPException(status_code=409, detail={"code": "review_required", "message": "Answer script must be ready for teacher review"})
    payload = dict(script.payload)
    payload.update(
        {
            "teacher_mark": body.teacher_mark,
            "teacher_rationale": body.rationale,
            "approved_by": principal.subject,
            "approved_at": datetime.now(timezone.utc).isoformat(),
            "grade_published": False,
            "approved_for_lms": body.approve_for_lms,
        }
    )
    script.payload = payload
    script = service.transition(script, "approved", {"approve_for_lms": body.approve_for_lms})
    response.headers["ETag"] = f'W/"{script.version}"'
    return serialize_resource(script)


@router.post("/accessibility/descriptions", status_code=201, tags=["accessibility"])
async def create_accessibility_description(
    body: AccessibilityDescriptionCreate,
    response: Response,
    request: Request,
    session: Annotated[Session, Depends(db_session)],
    principal: Annotated[Principal, Depends(require("accessibility:read"))],
    settings: Annotated[Settings, Depends(get_settings)],
) -> dict[str, Any]:
    if body.generation_mode == "institution_ai" and not settings.external_ai_enabled:
        raise HTTPException(status_code=409, detail={"code": "external_ai_disabled", "message": "Institution AI is disabled by policy"})
    service_for(session, principal, request).get("figure", body.figure_id)
    return _create_resource("accessibility_description", body, response, request, session, principal)


@router.post("/accessibility/descriptions/{description_id}/verify", tags=["accessibility"])
async def verify_accessibility_description(
    description_id: str,
    body: VerificationCreate,
    response: Response,
    request: Request,
    session: Annotated[Session, Depends(db_session)],
    principal: Annotated[Principal, Depends(require("accessibility:verify"))],
    if_match: Annotated[str | None, Header(alias="If-Match")] = None,
) -> dict[str, Any]:
    service = service_for(session, principal, request)
    description = service.get("accessibility_description", description_id)
    service.require_match(description, if_match)
    payload = dict(description.payload)
    payload["verification"] = {
        "decision": body.decision,
        "notes": body.notes,
        "reviewer": principal.subject,
        "reviewed_at": datetime.now(timezone.utc).isoformat(),
    }
    description.payload = payload
    description = service.transition(description, body.decision, {"reviewer": principal.subject})
    response.headers["ETag"] = f'W/"{description.version}"'
    return serialize_resource(description)


@router.post("/datasets", status_code=201, tags=["datasets"])
async def create_dataset_project(
    body: DatasetProjectCreate,
    response: Response,
    request: Request,
    session: Annotated[Session, Depends(db_session)],
    principal: Annotated[Principal, Depends(require("dataset:write"))],
) -> dict[str, Any]:
    return _create_resource("dataset_project", body, response, request, session, principal)


@router.post("/datasets/{dataset_id}/items", status_code=201, tags=["datasets"])
async def add_dataset_item(
    dataset_id: str,
    body: DatasetItemCreate,
    response: Response,
    request: Request,
    session: Annotated[Session, Depends(db_session)],
    principal: Annotated[Principal, Depends(require("dataset:contribute"))],
) -> dict[str, Any]:
    service = service_for(session, principal, request)
    dataset = service.get("dataset_project", dataset_id)
    if dataset.status not in {"draft", "active"}:
        raise HTTPException(status_code=409, detail={"code": "dataset_closed", "message": "Dataset no longer accepts items"})
    service.get("archive_document", body.source_document_id)
    body.parent_id = dataset_id
    return _created(service.create("dataset_item", body), response)


@router.post("/datasets/items/{item_id}/reviews", tags=["datasets"])
async def review_dataset_item(
    item_id: str,
    body: DatasetReviewCreate,
    response: Response,
    request: Request,
    session: Annotated[Session, Depends(db_session)],
    principal: Annotated[Principal, Depends(require("dataset:write"))],
    if_match: Annotated[str | None, Header(alias="If-Match")] = None,
) -> dict[str, Any]:
    service = service_for(session, principal, request)
    item = service.get("dataset_item", item_id)
    service.require_match(item, if_match)
    payload = dict(item.payload)
    reviews = list(payload.get("reviews", []))
    if any(review.get("reviewer") == principal.subject for review in reviews):
        raise HTTPException(status_code=409, detail={"code": "duplicate_review", "message": "A second independent reviewer is required"})
    reviews.append(
        {
            "reviewer": principal.subject,
            "decision": body.decision,
            "notes": body.notes,
            "reviewed_at": datetime.now(timezone.utc).isoformat(),
        }
    )
    payload["reviews"] = reviews
    verified = sum(review["decision"] == "verified" for review in reviews)
    item.payload = payload
    item.status = "gold" if verified >= 2 else "silver" if verified == 1 else "rejected"
    item.version += 1
    item.updated_by = principal.subject
    item.updated_at = datetime.now(timezone.utc)
    service.audit("dataset.item_reviewed", item, {"decision": body.decision, "label_state": item.status})
    session.commit()
    session.refresh(item)
    response.headers["ETag"] = f'W/"{item.version}"'
    return serialize_resource(item)


@router.post("/datasets/{dataset_id}/release", status_code=201, tags=["datasets"])
async def release_dataset(
    dataset_id: str,
    response: Response,
    request: Request,
    session: Annotated[Session, Depends(db_session)],
    principal: Annotated[Principal, Depends(require("dataset:release"))],
) -> dict[str, Any]:
    service = service_for(session, principal, request)
    dataset = service.get("dataset_project", dataset_id)
    items = list(
        session.scalars(
            service._query("dataset_item").where(Resource.parent_id == dataset_id)  # noqa: SLF001
        )
    )
    if not items or any(item.status != "gold" for item in items):
        raise HTTPException(status_code=409, detail={"code": "verification_required", "message": "Every dataset item requires two independent verified reviews"})
    item_manifest = [
        {
            "id": item.resource_id,
            "source_document_id": item.payload.get("source_document_id"),
            "sha256": item.content_sha256,
            "labels": item.payload.get("labels"),
            "provenance": item.payload.get("provenance"),
            "label_state": item.status,
            "split": (
                "test"
                if int(hashlib.sha256(str(item.payload.get("source_document_id")).encode()).hexdigest(), 16) % 10 == 0
                else "validation"
                if int(hashlib.sha256(str(item.payload.get("source_document_id")).encode()).hexdigest(), 16) % 10 == 1
                else "train"
            ),
        }
        for item in sorted(items, key=lambda value: value.resource_id)
    ]
    manifest_hash = hashlib.sha256(
        json.dumps(item_manifest, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    create = ResourceCreate(
        title=f"{dataset.title} release",
        status="published",
        parent_id=dataset_id,
        academic_unit_id=dataset.academic_unit_id,
        course_id=dataset.course_id,
        access_classification=dataset.access_classification,
        content_sha256=manifest_hash,
        payload={
            "schema_version": dataset.payload.get("schema_version"),
            "manifest_sha256": manifest_hash,
            "items": item_manifest,
            "split_policy": "source-document-hash-v1",
            "released_by": principal.subject,
        },
    )
    return _created(service.create("dataset_release", create, immutable=True), response)


@router.post("/webhooks", status_code=201, tags=["integrations"])
async def create_webhook_subscription(
    body: WebhookSubscriptionCreate,
    response: Response,
    request: Request,
    session: Annotated[Session, Depends(db_session)],
    principal: Annotated[Principal, Depends(require("integration:write"))],
) -> dict[str, Any]:
    return _create_resource("webhook_subscription", body, response, request, session, principal)


@router.get("/audit", tags=["audit"])
async def list_audit_events(
    request: Request,
    session: Annotated[Session, Depends(db_session)],
    principal: Annotated[Principal, Depends(require("audit:read"))],
    limit: Annotated[int, Query(ge=1, le=200)] = 100,
) -> dict[str, Any]:
    service_for(session, principal, request)
    events = list(
        session.scalars(
            select(AuditEvent)
            .where(AuditEvent.tenant_id == principal.tenant_id)
            .order_by(AuditEvent.sequence.desc())
            .limit(limit)
        )
    )
    return {
        "items": [
            {
                "event_id": event.event_id,
                "actor_id": event.actor_id,
                "action": event.action,
                "resource_type": event.resource_type,
                "resource_id": event.resource_id,
                "request_id": event.request_id,
                "details": event.details,
                "previous_hash": event.previous_hash,
                "event_hash": event.event_hash,
                "created_at": event.created_at.isoformat(),
            }
            for event in events
        ]
    }
