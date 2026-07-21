"""Deterministic, source-linked answer-script alignment and draft feedback."""

from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import Field, model_validator
from sqlalchemy.orm import Session

from .api import db_session, service_for
from .models import Resource
from .schemas import ResourceCreate, StrictModel
from .security import Principal, require
from .service import serialize_resource

router = APIRouter(prefix="/api/v2", tags=["answer-scripts"])
QUESTION_MARKER = re.compile(r"^\s*(?:question\s+)?(?P<number>\d+(?:\s*\([a-z0-9ivx]+\))?|[a-z])\s*[.):\-]", re.I)


class QuestionEvidence(StrictModel):
    question_id: str = Field(min_length=1, max_length=128)
    display_number: str = Field(min_length=1, max_length=50)
    text: str = Field(min_length=1, max_length=20_000)
    source_page: int = Field(ge=1)
    source_line_ids: list[str] = Field(min_length=1, max_length=500)
    rubric_criteria: list[str] = Field(default_factory=list, max_length=100)


class ResponseLine(StrictModel):
    line_id: str = Field(min_length=1, max_length=128)
    page: int = Field(ge=1)
    text: str = Field(min_length=1, max_length=10_000)
    geometry: list[float] | None = Field(default=None, min_length=4, max_length=8)
    ocr_confidence: float | None = Field(default=None, ge=0, le=1)


class AnswerAnalysisRequest(StrictModel):
    questions: list[QuestionEvidence] = Field(min_length=1, max_length=500)
    response_lines: list[ResponseLine] = Field(min_length=1, max_length=20_000)

    @model_validator(mode="after")
    def unique_evidence_ids(self):
        question_ids = [item.question_id for item in self.questions]
        line_ids = [item.line_id for item in self.response_lines]
        if len(set(question_ids)) != len(question_ids):
            raise ValueError("question IDs must be unique")
        if len(set(line_ids)) != len(line_ids):
            raise ValueError("response line IDs must be unique")
        return self


def _normalize_number(value: str) -> str:
    return re.sub(r"\s+", "", value).casefold().rstrip(".)")


def align_answers(request: AnswerAnalysisRequest) -> dict[str, Any]:
    number_to_question = {
        _normalize_number(item.display_number): item for item in request.questions
    }
    grouped: dict[str, list[ResponseLine]] = {item.question_id: [] for item in request.questions}
    unassigned: list[ResponseLine] = []
    current: QuestionEvidence | None = request.questions[0] if len(request.questions) == 1 else None
    match_modes: dict[str, str] = {}
    for line in request.response_lines:
        marker = QUESTION_MARKER.match(line.text)
        if marker:
            candidate = number_to_question.get(_normalize_number(marker.group("number")))
            if candidate:
                current = candidate
                match_modes[candidate.question_id] = "explicit_number"
        if current:
            grouped[current.question_id].append(line)
        else:
            unassigned.append(line)
    results = []
    for question in request.questions:
        lines = grouped[question.question_id]
        confidences = [line.ocr_confidence for line in lines if line.ocr_confidence is not None]
        alignment_confidence = 0.95 if match_modes.get(question.question_id) == "explicit_number" else 0.75 if lines else 0.0
        criteria_text = ", ".join(question.rubric_criteria) or "the assigned rubric criteria"
        results.append(
            {
                "question_id": question.question_id,
                "display_number": question.display_number,
                "question_text": question.text,
                "response_text": "\n".join(line.text for line in lines),
                "response_line_ids": [line.line_id for line in lines],
                "source_pages": sorted({line.page for line in lines}),
                "source_geometry": [
                    {"line_id": line.line_id, "page": line.page, "geometry": line.geometry}
                    for line in lines
                    if line.geometry
                ],
                "ocr_confidence": round(sum(confidences) / len(confidences), 4) if confidences else None,
                "alignment_confidence": alignment_confidence,
                "ai_confidence": None,
                "draft_feedback": (
                    f"Teacher review: compare this response with {criteria_text}; "
                    "confirm accuracy, reasoning, and supporting evidence."
                    if lines
                    else "Teacher review: no response was aligned; inspect the source pages manually."
                ),
                "teacher_mark": None,
                "review_status": "needs_teacher_review",
            }
        )
    return {
        "responses": results,
        "unassigned_lines": [line.model_dump() for line in unassigned],
        "limitations": [
            "Alignment is deterministic and may require correction when question numbers are absent.",
            "Draft feedback does not assign marks or publish a grade.",
        ],
    }


@router.post("/answer-scripts/{script_id}/analyze")
async def analyze_answer_script(
    script_id: str,
    body: AnswerAnalysisRequest,
    request: Request,
    session: Annotated[Session, Depends(db_session)],
    principal: Annotated[Principal, Depends(require("answer:write"))],
) -> dict[str, Any]:
    service = service_for(session, principal, request)
    script = service.get("answer_script", script_id)
    if script.status not in {"draft", "changes_requested"}:
        raise HTTPException(status_code=409, detail={"code": "analysis_locked", "message": "Approved or active-review scripts cannot be re-analyzed"})
    analysis = align_answers(body)
    existing = list(
        session.scalars(
            service._query("answer_response").where(Resource.parent_id == script_id)  # noqa: SLF001
        )
    )
    if existing:
        raise HTTPException(status_code=409, detail={"code": "analysis_exists", "message": "Remove or review the existing deterministic alignment before re-analysis"})
    response_ids = []
    for item in analysis["responses"]:
        response_resource = service.create(
            "answer_response",
            ResourceCreate(
                title=f"Response {item['display_number']}",
                status="needs_teacher_review",
                parent_id=script_id,
                academic_unit_id=script.academic_unit_id,
                course_id=script.course_id,
                access_classification="private",
                search_text=item["response_text"],
                payload=item,
            ),
            immutable=True,
        )
        response_ids.append(response_resource.resource_id)
    payload = dict(script.payload)
    payload["deterministic_analysis"] = {
        "response_ids": response_ids,
        "unassigned_lines": analysis["unassigned_lines"],
        "limitations": analysis["limitations"],
        "created_at": datetime.now(timezone.utc).isoformat(),
        "automatic_mark": None,
    }
    script.payload = payload
    script.status = "ready_for_review"
    script.version += 1
    script.updated_by = principal.subject
    script.updated_at = datetime.now(timezone.utc)
    service.audit("answer_script.aligned", script, {"response_count": len(response_ids), "automatic_mark": None})
    session.commit()
    session.refresh(script)
    return {"answer_script": serialize_resource(script), **analysis}
