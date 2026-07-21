"""Strict API schemas for institutional resources and actions."""

from __future__ import annotations

from datetime import date, datetime
from typing import Any, Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


AccessClassification = Literal["private", "course", "academic_unit", "institution"]


class ResourceCreate(StrictModel):
    title: str = Field(min_length=1, max_length=500)
    status: str = Field(default="draft", min_length=1, max_length=64)
    parent_id: str | None = Field(default=None, max_length=128)
    academic_unit_id: str | None = Field(default=None, max_length=128)
    course_id: str | None = Field(default=None, max_length=128)
    access_classification: AccessClassification = "institution"
    content_sha256: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")
    search_text: str = Field(default="", max_length=2_000_000)
    payload: dict[str, Any] = Field(default_factory=dict)


class ResourcePatch(StrictModel):
    title: str | None = Field(default=None, min_length=1, max_length=500)
    status: str | None = Field(default=None, min_length=1, max_length=64)
    access_classification: AccessClassification | None = None
    search_text: str | None = Field(default=None, max_length=2_000_000)
    payload: dict[str, Any] | None = None


class ArchiveDocumentCreate(ResourceCreate):
    status: Literal["draft", "indexed", "archived"] = "draft"

    @model_validator(mode="after")
    def evidence_hash_required(self):
        if not self.content_sha256:
            raise ValueError("archive documents require an immutable content hash")
        return self


class EvidenceVersionCreate(ResourceCreate):
    status: Literal["published"] = "published"

    @model_validator(mode="after")
    def hash_required(self):
        if not self.content_sha256:
            raise ValueError("evidence versions require a content hash")
        return self


class ReviewTaskCreate(ResourceCreate):
    status: Literal["open", "in_review"] = "open"
    assigned_to: list[str] = Field(default_factory=list, max_length=50)
    due_at: datetime | None = None
    moderation_required: bool = False
    saved_filter_id: str | None = Field(default=None, max_length=128)

    @model_validator(mode="after")
    def put_review_fields_in_payload(self):
        self.payload.update(
            {
                "assigned_to": sorted(set(self.assigned_to)),
                "due_at": self.due_at.isoformat() if self.due_at else None,
                "moderation_required": self.moderation_required,
                "saved_filter_id": self.saved_filter_id,
            }
        )
        return self


class ReviewAssignmentUpdate(StrictModel):
    assigned_to: list[str] = Field(default_factory=list, max_length=50)
    due_at: datetime | None = None
    moderation_required: bool = False


class CommentCreate(StrictModel):
    text: str = Field(min_length=1, max_length=5000)
    mentions: list[str] = Field(default_factory=list, max_length=50)


class ReviewDecisionCreate(StrictModel):
    decision: Literal["approved", "changes_requested", "rejected"]
    rationale: str = Field(min_length=1, max_length=5000)


class CurriculumVersionCreate(ResourceCreate):
    status: Literal["draft"] = "draft"
    version_label: str = Field(min_length=1, max_length=100)
    jurisdiction: str | None = Field(default=None, max_length=200)
    subject: str = Field(min_length=1, max_length=200)
    academic_level: str = Field(min_length=1, max_length=200)
    effective_from: date | None = None
    effective_to: date | None = None

    @model_validator(mode="after")
    def put_version_fields_in_payload(self):
        if self.effective_from and self.effective_to and self.effective_to < self.effective_from:
            raise ValueError("effective_to cannot precede effective_from")
        self.payload.update(
            {
                "version_label": self.version_label,
                "jurisdiction": self.jurisdiction,
                "subject": self.subject,
                "academic_level": self.academic_level,
                "effective_from": self.effective_from.isoformat() if self.effective_from else None,
                "effective_to": self.effective_to.isoformat() if self.effective_to else None,
            }
        )
        return self


class LearningObjectiveCreate(ResourceCreate):
    stable_code: str = Field(min_length=1, max_length=200)
    description: str = Field(min_length=1, max_length=5000)
    prerequisites: list[str] = Field(default_factory=list, max_length=100)
    replaces: list[str] = Field(default_factory=list, max_length=100)

    @model_validator(mode="after")
    def put_objective_fields_in_payload(self):
        self.payload.update(
            {
                "stable_code": self.stable_code,
                "description": self.description,
                "prerequisites": self.prerequisites,
                "replaces": self.replaces,
            }
        )
        self.search_text = " ".join((self.search_text, self.stable_code, self.description)).strip()
        return self


class RubricVersionCreate(ResourceCreate):
    status: Literal["draft"] = "draft"
    version_label: str = Field(min_length=1, max_length=100)
    maximum_marks: float = Field(ge=0, le=100_000)
    criteria: list[dict[str, Any]] = Field(min_length=1, max_length=200)

    @model_validator(mode="after")
    def validate_criteria(self):
        identifiers: set[str] = set()
        total_weight = 0.0
        for criterion in self.criteria:
            identifier = str(criterion.get("id", "")).strip()
            if not identifier or identifier in identifiers:
                raise ValueError("rubric criterion IDs must be present and unique")
            identifiers.add(identifier)
            weight = criterion.get("weight", 0)
            if not isinstance(weight, (int, float)) or weight < 0:
                raise ValueError("rubric criterion weights must be non-negative")
            total_weight += float(weight)
        if total_weight and abs(total_weight - 100.0) > 0.01:
            raise ValueError("weighted rubric criteria must total 100")
        self.payload.update(
            {
                "version_label": self.version_label,
                "maximum_marks": self.maximum_marks,
                "criteria": self.criteria,
            }
        )
        return self


class ObjectiveMappingCreate(ResourceCreate):
    source_resource_id: str = Field(min_length=1, max_length=128)
    objective_id: str = Field(min_length=1, max_length=128)
    curriculum_version_id: str = Field(min_length=1, max_length=128)
    mapping_status: Literal["draft", "approved"] = "draft"
    rationale: str | None = Field(default=None, max_length=2000)

    @model_validator(mode="after")
    def put_mapping_fields_in_payload(self):
        self.payload.update(
            {
                "source_resource_id": self.source_resource_id,
                "objective_id": self.objective_id,
                "curriculum_version_id": self.curriculum_version_id,
                "mapping_status": self.mapping_status,
                "rationale": self.rationale,
            }
        )
        return self


class AnswerScriptCreate(ResourceCreate):
    assessment_id: str = Field(min_length=1, max_length=128)
    learner_pseudonym: str = Field(min_length=8, max_length=255)
    rubric_version_id: str = Field(min_length=1, max_length=128)
    status: Literal["draft", "ready_for_review"] = "draft"

    @model_validator(mode="after")
    def put_answer_fields_in_payload(self):
        self.payload.update(
            {
                "assessment_id": self.assessment_id,
                "learner_pseudonym": self.learner_pseudonym,
                "rubric_version_id": self.rubric_version_id,
                "teacher_mark": None,
                "grade_published": False,
            }
        )
        return self


class TeacherApproval(StrictModel):
    teacher_mark: float = Field(ge=0, le=100_000)
    rationale: str = Field(min_length=1, max_length=5000)
    approve_for_lms: bool = False


class AccessibilityDescriptionCreate(ResourceCreate):
    figure_id: str = Field(min_length=1, max_length=128)
    description: str = Field(min_length=1, max_length=10_000)
    description_kind: Literal["short", "long", "decorative", "unreadable"]
    generation_mode: Literal["local_model", "institution_ai", "human"]
    model_name: str | None = Field(default=None, max_length=255)
    model_version: str | None = Field(default=None, max_length=100)
    status: Literal["draft"] = "draft"

    @model_validator(mode="after")
    def put_accessibility_fields_in_payload(self):
        self.payload.update(
            {
                "figure_id": self.figure_id,
                "description": self.description,
                "description_kind": self.description_kind,
                "generation_mode": self.generation_mode,
                "model_name": self.model_name,
                "model_version": self.model_version,
            }
        )
        return self


class VerificationCreate(StrictModel):
    decision: Literal["approved", "rejected", "needs_specialist"]
    notes: str = Field(default="", max_length=5000)


class DatasetProjectCreate(ResourceCreate):
    purpose: str = Field(min_length=1, max_length=2000)
    schema_version: str = Field(min_length=1, max_length=100)
    allowed_uses: list[str] = Field(min_length=1, max_length=50)
    license_name: str = Field(min_length=1, max_length=200)
    retention_days: int = Field(ge=1, le=36500)

    @model_validator(mode="after")
    def put_dataset_fields_in_payload(self):
        self.payload.update(
            {
                "purpose": self.purpose,
                "schema_version": self.schema_version,
                "allowed_uses": self.allowed_uses,
                "license_name": self.license_name,
                "retention_days": self.retention_days,
            }
        )
        return self


class DatasetItemCreate(ResourceCreate):
    source_document_id: str = Field(min_length=1, max_length=128)
    provenance: dict[str, Any]
    labels: dict[str, Any]
    contains_student_data: bool = False
    deidentified: bool = False

    @model_validator(mode="after")
    def protect_student_data(self):
        if self.contains_student_data and not self.deidentified:
            raise ValueError("student-derived dataset items must be de-identified")
        self.payload.update(
            {
                "source_document_id": self.source_document_id,
                "provenance": self.provenance,
                "labels": self.labels,
                "contains_student_data": self.contains_student_data,
                "deidentified": self.deidentified,
                "reviews": [],
            }
        )
        return self


class DatasetReviewCreate(StrictModel):
    decision: Literal["verified", "rejected"]
    notes: str = Field(default="", max_length=5000)


class WorkerEnrollmentCreate(StrictModel):
    name: str = Field(min_length=1, max_length=200)
    device_public_key: str = Field(min_length=32, max_length=4000)
    capabilities: dict[str, Any]


class WorkerStateUpdate(StrictModel):
    status: Literal["active", "draining", "revoked"]
    reason: str = Field(default="", max_length=1000)


class ApiClientCreate(StrictModel):
    name: str = Field(min_length=1, max_length=200)
    permissions: list[str] = Field(min_length=1, max_length=50)
    academic_unit_ids: list[str] = Field(default_factory=list, max_length=100)
    course_ids: list[str] = Field(default_factory=list, max_length=100)
    enabled_features: list[str] = Field(default_factory=list, max_length=20)
    expires_days: int = Field(default=365, ge=1, le=3650)


class WorkerJobCreate(StrictModel):
    workflow: Literal["text_recognition", "layout_parsing", "table_extraction", "vl_processing"]
    source_artifact: dict[str, Any]
    settings: dict[str, Any] = Field(default_factory=dict)
    allowed_outputs: list[
        Literal["result_json", "markdown", "docx", "preview", "crop", "table"]
    ] = Field(default_factory=list, max_length=20)


class SearchRequest(StrictModel):
    query: str = Field(default="", max_length=1000)
    resource_types: list[str] = Field(default_factory=list, max_length=50)
    course_id: str | None = Field(default=None, max_length=128)
    academic_unit_id: str | None = Field(default=None, max_length=128)
    statuses: list[str] = Field(default_factory=list, max_length=50)
    assessment_id: str | None = Field(default=None, max_length=128)
    access_classifications: list[AccessClassification] = Field(default_factory=list)
    reviewer_id: str | None = Field(default=None, max_length=255)
    created_from: datetime | None = None
    created_to: datetime | None = None
    quality_min: float | None = Field(default=None, ge=0, le=1)
    warning_max: int | None = Field(default=None, ge=0, le=1_000_000)
    semantic: bool = False
    limit: int = Field(default=25, ge=1, le=100)
    cursor: str | None = None

    @model_validator(mode="after")
    def validate_dates(self):
        if self.created_from and self.created_to and self.created_to < self.created_from:
            raise ValueError("created_to cannot precede created_from")
        return self


class SavedFilterCreate(ResourceCreate):
    status: Literal["active"] = "active"
    access_classification: Literal["private"] = "private"
    search: SearchRequest

    @model_validator(mode="after")
    def put_search_in_payload(self):
        self.payload["search"] = self.search.model_dump(mode="json")
        return self


class WebhookSubscriptionCreate(ResourceCreate):
    target_url: str = Field(pattern=r"^https://", max_length=2000)
    event_types: list[str] = Field(min_length=1, max_length=50)

    @field_validator("target_url")
    @classmethod
    def reject_local_targets(cls, value: str) -> str:
        lowered = value.casefold()
        if any(host in lowered for host in ("localhost", "127.0.0.1", "[::1]")):
            raise ValueError("webhook targets cannot be loopback addresses")
        return value

    @model_validator(mode="after")
    def put_webhook_fields_in_payload(self):
        self.payload.update({"target_url": self.target_url, "event_types": self.event_types})
        return self


class LtiRegistrationCreate(ResourceCreate):
    issuer: str = Field(pattern=r"^https://", max_length=1000)
    client_id: str = Field(min_length=1, max_length=500)
    deployment_id: str = Field(min_length=1, max_length=500)
    authorization_endpoint: str = Field(pattern=r"^https://", max_length=2000)
    token_endpoint: str = Field(pattern=r"^https://", max_length=2000)
    jwks_url: str = Field(pattern=r"^https://", max_length=2000)
    enabled_services: list[Literal["deep_linking", "nrps", "ags"]] = Field(
        default_factory=list
    )
    allowed_service_hosts: list[str] = Field(default_factory=list, max_length=20)

    @model_validator(mode="after")
    def put_lti_fields_in_payload(self):
        endpoint_hosts = {
            str(urlsplit(value).hostname)
            for value in (
                self.issuer,
                self.authorization_endpoint,
                self.token_endpoint,
                self.jwks_url,
            )
            if urlsplit(value).hostname
        }
        requested_hosts = {host.casefold() for host in self.allowed_service_hosts}
        if any("/" in host or ":" in host for host in requested_hosts):
            raise ValueError("allowed LTI service hosts must be hostnames only")
        self.payload.update(
            {
                "issuer": self.issuer,
                "client_id": self.client_id,
                "deployment_id": self.deployment_id,
                "authorization_endpoint": self.authorization_endpoint,
                "token_endpoint": self.token_endpoint,
                "jwks_url": self.jwks_url,
                "enabled_services": self.enabled_services,
                "allowed_service_hosts": sorted(endpoint_hosts | requested_hosts),
            }
        )
        return self
