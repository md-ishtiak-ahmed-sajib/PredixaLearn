"""Tenant-safe resource, search, versioning, and audit operations."""

from __future__ import annotations

import base64
import hashlib
import json
from datetime import datetime, timezone
from typing import Any

from fastapi import HTTPException
from sqlalchemy import and_, or_, select, text
from sqlalchemy.orm import Session

from .config import get_settings
from .database import set_tenant_context
from .models import AuditEvent, Resource, WebhookDelivery
from .schemas import ResourceCreate, ResourcePatch
from .security import Principal
from .semantic import cosine, embedding, pgvector_literal

IMMUTABLE_TYPES = {"evidence_version", "dataset_release"}
PUBLISHABLE_TYPES = {"curriculum_version", "rubric_version", "taxonomy_version", "syllabus_version"}
ALLOWED_TRANSITIONS: dict[str, dict[str, set[str]]] = {
    "curriculum_version": {
        "draft": {"published"},
        "published": {"superseded", "retired"},
        "superseded": {"retired"},
        "retired": set(),
    },
    "rubric_version": {
        "draft": {"published"},
        "published": {"superseded", "retired"},
        "superseded": {"retired"},
        "retired": set(),
    },
    "taxonomy_version": {
        "draft": {"published"},
        "published": {"superseded", "retired"},
        "superseded": {"retired"},
        "retired": set(),
    },
    "syllabus_version": {
        "draft": {"published"},
        "published": {"superseded", "retired"},
        "superseded": {"retired"},
        "retired": set(),
    },
    "correction_overlay": {
        "draft": {"reviewed", "rejected"},
        "reviewed": {"approved", "rejected", "draft"},
        "approved": {"reverted"},
        "rejected": {"draft"},
        "reverted": set(),
    },
    "review_task": {
        "open": {"in_review", "cancelled"},
        "in_review": {"approved", "changes_requested", "rejected", "cancelled"},
        "changes_requested": {"in_review", "cancelled"},
        "approved": set(),
        "rejected": set(),
        "cancelled": set(),
    },
    "answer_script": {
        "draft": {"ready_for_review"},
        "ready_for_review": {"approved", "changes_requested"},
        "changes_requested": {"ready_for_review"},
        "approved": set(),
    },
    "accessibility_description": {
        "draft": {"approved", "rejected", "needs_specialist"},
        "needs_specialist": {"approved", "rejected"},
        "approved": set(),
        "rejected": set(),
    },
    "dataset_project": {
        "draft": {"active", "archived"},
        "active": {"archived"},
        "archived": set(),
    },
}


def _encode_cursor(updated_at: datetime, resource_id: str) -> str:
    raw = json.dumps([updated_at.isoformat(), resource_id], separators=(",", ":"))
    return base64.urlsafe_b64encode(raw.encode()).decode().rstrip("=")


def _decode_cursor(value: str) -> tuple[datetime, str]:
    try:
        padded = value + "=" * (-len(value) % 4)
        timestamp, identifier = json.loads(base64.urlsafe_b64decode(padded).decode())
        return datetime.fromisoformat(timestamp), str(identifier)
    except Exception as exc:
        raise HTTPException(
            status_code=422,
            detail={"code": "invalid_cursor", "message": "Pagination cursor is invalid"},
        ) from exc


def serialize_resource(resource: Resource) -> dict[str, Any]:
    return {
        "id": resource.resource_id,
        "type": resource.resource_type,
        "version": resource.version,
        "etag": f'W/"{resource.version}"',
        "title": resource.title,
        "status": resource.status,
        "parent_id": resource.parent_id,
        "academic_unit_id": resource.academic_unit_id,
        "course_id": resource.course_id,
        "access_classification": resource.access_classification,
        "content_sha256": resource.content_sha256,
        "payload": resource.payload,
        "created_by": resource.created_by,
        "updated_by": resource.updated_by,
        "created_at": resource.created_at.isoformat(),
        "updated_at": resource.updated_at.isoformat(),
    }


class ResourceService:
    def __init__(self, session: Session, principal: Principal, request_id: str) -> None:
        self.session = session
        self.principal = principal
        self.request_id = request_id
        set_tenant_context(session, principal.tenant_id)

    def _scope_guard(self, academic_unit_id: str | None, course_id: str | None) -> None:
        if not self.principal.can_access_scope(academic_unit_id, course_id):
            raise HTTPException(
                status_code=403,
                detail={"code": "scope_denied", "message": "Resource is outside your assigned scope"},
            )

    def _query(self, resource_type: str | None = None):
        conditions = [
            Resource.tenant_id == self.principal.tenant_id,
            or_(
                Resource.access_classification != "private",
                Resource.created_by == self.principal.subject,
            ),
        ]
        if resource_type:
            conditions.append(Resource.resource_type == resource_type)
        if resource_type == "student_revision_progress" and "student" in self.principal.roles:
            conditions.append(Resource.created_by == self.principal.subject)
        if self.principal.course_ids and "*" not in self.principal.permissions:
            conditions.append(
                or_(Resource.course_id.is_(None), Resource.course_id.in_(self.principal.course_ids))
            )
        if self.principal.academic_unit_ids and "*" not in self.principal.permissions:
            conditions.append(
                or_(
                    Resource.academic_unit_id.is_(None),
                    Resource.academic_unit_id.in_(self.principal.academic_unit_ids),
                )
            )
        return select(Resource).where(and_(*conditions))

    def get(self, resource_type: str, resource_id: str) -> Resource:
        resource = self.session.scalar(
            self._query(resource_type).where(Resource.resource_id == resource_id)
        )
        if resource is None:
            raise HTTPException(
                status_code=404,
                detail={"code": "not_found", "message": "Institution resource was not found"},
            )
        self._scope_guard(resource.academic_unit_id, resource.course_id)
        return resource

    def create(
        self,
        resource_type: str,
        request: ResourceCreate,
        *,
        immutable: bool = False,
        resource_id: str | None = None,
    ) -> Resource:
        self._scope_guard(request.academic_unit_id, request.course_id)
        resource = Resource(
            tenant_id=self.principal.tenant_id,
            resource_type=resource_type,
            resource_id=resource_id,
            title=request.title,
            status=request.status,
            parent_id=request.parent_id,
            academic_unit_id=request.academic_unit_id,
            course_id=request.course_id,
            access_classification=(
                "private"
                if resource_type == "student_revision_progress"
                else request.access_classification
            ),
            content_sha256=request.content_sha256,
            search_text=request.search_text,
            payload=request.payload,
            immutable=immutable or resource_type in IMMUTABLE_TYPES,
            created_by=self.principal.subject,
            updated_by=self.principal.subject,
        )
        self.session.add(resource)
        self.session.flush()
        self._update_embedding(resource)
        self.audit("resource.created", resource, {"status": resource.status})
        self.session.commit()
        self.session.refresh(resource)
        return resource

    def patch(self, resource: Resource, request: ResourcePatch, if_match: str | None) -> Resource:
        if resource.immutable or (
            resource.resource_type in PUBLISHABLE_TYPES and resource.status != "draft"
        ):
            raise HTTPException(
                status_code=409,
                detail={
                    "code": "immutable_resource",
                    "message": "Published evidence and versions cannot be edited",
                },
            )
        self.require_match(resource, if_match)
        changes = request.model_dump(exclude_none=True)
        if "status" in changes:
            transitions = ALLOWED_TRANSITIONS.get(resource.resource_type)
            next_status = str(changes["status"])
            if transitions is not None and next_status not in transitions.get(resource.status, set()):
                raise HTTPException(
                    status_code=409,
                    detail={"code": "invalid_transition", "message": "Status transition is not allowed"},
                )
        before = {key: getattr(resource, key) for key in changes}
        for key, value in changes.items():
            setattr(resource, key, value)
        resource.version += 1
        resource.updated_by = self.principal.subject
        resource.updated_at = datetime.now(timezone.utc)
        self.session.flush()
        self._update_embedding(resource)
        self.audit("resource.updated", resource, {"before": before, "changed": sorted(changes)})
        self.session.commit()
        self.session.refresh(resource)
        return resource

    @staticmethod
    def require_match(resource: Resource, if_match: str | None) -> None:
        expected = f'W/"{resource.version}"'
        if if_match != expected:
            raise HTTPException(
                status_code=412,
                detail={
                    "code": "version_conflict",
                    "message": "Resource changed; reload before applying your review",
                    "current_etag": expected,
                },
            )

    def transition(self, resource: Resource, next_status: str, details: dict[str, Any]) -> Resource:
        transitions = ALLOWED_TRANSITIONS.get(resource.resource_type, {})
        if next_status not in transitions.get(resource.status, set()):
            raise HTTPException(
                status_code=409,
                detail={"code": "invalid_transition", "message": "Status transition is not allowed"},
            )
        prior = resource.status
        resource.status = next_status
        resource.version += 1
        resource.updated_by = self.principal.subject
        resource.updated_at = datetime.now(timezone.utc)
        if resource.resource_type in PUBLISHABLE_TYPES and next_status == "published":
            resource.immutable = True
        self.audit(
            "resource.transitioned",
            resource,
            {"from": prior, "to": next_status, **details},
        )
        self.session.commit()
        self.session.refresh(resource)
        return resource

    def list(
        self,
        resource_type: str,
        *,
        limit: int,
        cursor: str | None,
        status: str | None = None,
    ) -> dict[str, Any]:
        statement = self._query(resource_type)
        if status:
            statement = statement.where(Resource.status == status)
        if cursor:
            timestamp, identifier = _decode_cursor(cursor)
            statement = statement.where(
                or_(
                    Resource.updated_at < timestamp,
                    and_(Resource.updated_at == timestamp, Resource.resource_id < identifier),
                )
            )
        resources = list(
            self.session.scalars(
                statement.order_by(Resource.updated_at.desc(), Resource.resource_id.desc()).limit(
                    limit + 1
                )
            )
        )
        has_more = len(resources) > limit
        page = resources[:limit]
        next_cursor = (
            _encode_cursor(page[-1].updated_at, page[-1].resource_id)
            if has_more and page
            else None
        )
        return {"items": [serialize_resource(item) for item in page], "next_cursor": next_cursor}

    def search(
        self,
        *,
        query: str,
        resource_types: list[str],
        course_id: str | None,
        academic_unit_id: str | None,
        statuses: list[str],
        assessment_id: str | None,
        access_classifications: list[str],
        reviewer_id: str | None,
        created_from: datetime | None,
        created_to: datetime | None,
        quality_min: float | None,
        warning_max: int | None,
        limit: int,
        cursor: str | None,
        semantic: bool = False,
    ) -> dict[str, Any]:
        statement = self._query()
        if resource_types:
            statement = statement.where(Resource.resource_type.in_(resource_types))
        if course_id:
            self._scope_guard(None, course_id)
            statement = statement.where(Resource.course_id == course_id)
        if academic_unit_id:
            self._scope_guard(academic_unit_id, None)
            statement = statement.where(Resource.academic_unit_id == academic_unit_id)
        if statuses:
            statement = statement.where(Resource.status.in_(statuses))
        if assessment_id:
            statement = statement.where(
                Resource.payload["assessment_id"].as_string() == assessment_id
            )
        if access_classifications:
            statement = statement.where(
                Resource.access_classification.in_(access_classifications)
            )
        if reviewer_id:
            statement = statement.where(Resource.updated_by == reviewer_id)
        if created_from:
            statement = statement.where(Resource.created_at >= created_from)
        if created_to:
            statement = statement.where(Resource.created_at <= created_to)
        if quality_min is not None:
            statement = statement.where(
                Resource.payload["quality_score"].as_float() >= quality_min
            )
        if warning_max is not None:
            statement = statement.where(
                Resource.payload["warning_count"].as_integer() <= warning_max
            )
        if query and not semantic:
            terms = [term.casefold() for term in query.split() if term][:20]
            for term in terms:
                escaped = term.replace("%", "\\%").replace("_", "\\_")
                pattern = f"%{escaped}%"
                statement = statement.where(
                    or_(
                        Resource.title.ilike(pattern, escape="\\"),
                        Resource.search_text.ilike(pattern, escape="\\"),
                    )
                )
        if cursor:
            timestamp, identifier = _decode_cursor(cursor)
            statement = statement.where(
                or_(
                    Resource.updated_at < timestamp,
                    and_(Resource.updated_at == timestamp, Resource.resource_id < identifier),
                )
            )
        if semantic:
            query_embedding = embedding(query)
            if self.session.bind and self.session.bind.dialect.name == "postgresql":
                statement = statement.where(Resource.search_text != "").order_by(
                    text(
                        "embedding <=> CAST(:predixalearn_query_embedding AS vector)"
                    )
                ).params(predixalearn_query_embedding=pgvector_literal(query_embedding))
                resources = list(self.session.scalars(statement.limit(limit)))
            else:
                candidates = list(self.session.scalars(statement.limit(1000)))
                resources = sorted(
                    candidates,
                    key=lambda item: cosine(
                        query_embedding, embedding(f"{item.title} {item.search_text}")
                    ),
                    reverse=True,
                )[:limit]
        else:
            resources = list(
                self.session.scalars(
                    statement.order_by(
                        Resource.updated_at.desc(), Resource.resource_id.desc()
                    ).limit(limit + 1)
                )
            )
        page = resources[:limit]
        return {
            "items": [serialize_resource(item) for item in page],
            "next_cursor": (
                _encode_cursor(page[-1].updated_at, page[-1].resource_id)
                if not semantic and len(resources) > limit and page
                else None
            ),
            "semantic_used": semantic,
        }

    def _update_embedding(self, resource: Resource) -> None:
        settings = get_settings()
        if (
            not settings.semantic_search_enabled
            or not resource.search_text.strip()
            or not self.session.bind
            or self.session.bind.dialect.name != "postgresql"
        ):
            return
        vector = pgvector_literal(embedding(f"{resource.title} {resource.search_text}"))
        self.session.execute(
            text(
                "UPDATE resources SET embedding = CAST(:embedding AS vector) WHERE row_id = :row_id"
            ),
            {"embedding": vector, "row_id": resource.row_id},
        )

    def audit(self, action: str, resource: Resource, details: dict[str, Any]) -> None:
        self.audit_subject(
            action,
            resource.resource_type,
            resource.resource_id,
            details,
        )

    def audit_subject(
        self,
        action: str,
        resource_type: str,
        resource_id: str,
        details: dict[str, Any],
    ) -> None:
        """Append an audit event for resources that do not use the generic envelope."""

        previous = self.session.scalar(
            select(AuditEvent)
            .where(AuditEvent.tenant_id == self.principal.tenant_id)
            .order_by(AuditEvent.sequence.desc())
            .limit(1)
        )
        created_at = datetime.now(timezone.utc)
        body = {
            "tenant_id": self.principal.tenant_id,
            "actor_id": self.principal.subject,
            "action": action,
            "resource_type": resource_type,
            "resource_id": resource_id,
            "request_id": self.request_id,
            "details": details,
            "previous_hash": previous.event_hash if previous else None,
            "created_at": created_at.isoformat(),
        }
        event_hash = hashlib.sha256(
            json.dumps(body, sort_keys=True, separators=(",", ":"), default=str).encode()
        ).hexdigest()
        event = AuditEvent(
            tenant_id=self.principal.tenant_id,
            actor_id=self.principal.subject,
            action=action,
            resource_type=resource_type,
            resource_id=resource_id,
            request_id=self.request_id,
            details=details,
            previous_hash=body["previous_hash"],
            event_hash=event_hash,
            created_at=created_at,
        )
        self.session.add(event)
        self.session.flush()
        self.session.info.setdefault("predixalearn_audit_notifications", []).append(
            {
                "tenant_id": self.principal.tenant_id,
                "event_id": event.event_id,
                "sequence": event.sequence,
            }
        )
        subscriptions = list(self.session.scalars(self._query("webhook_subscription")))
        for subscription in subscriptions:
            event_types = subscription.payload.get("event_types", [])
            if action in event_types or "*" in event_types:
                self.session.add(
                    WebhookDelivery(
                        tenant_id=self.principal.tenant_id,
                        subscription_id=subscription.resource_id,
                        event_id=event.event_id,
                    )
                )
