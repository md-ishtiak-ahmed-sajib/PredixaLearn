"""Source-linked Past Paper Intelligence API."""

from __future__ import annotations

from typing import Annotated, Literal

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from app.core.config import get_settings
from app.core.queue import JobStatus, get_queue
from app.education.demo import create_judge_demo
from app.education.service import (
    EducationError,
    aggregate_history,
    analysis_download_path,
    create_analysis,
    generate_practice,
    question_crop_path,
    read_analysis,
    read_practice,
    source_preview_path,
    update_question,
)
from app.storage.history import HistoryNotFoundError, get_history_store
from app.teaching.languages import language_reliability
from app.workflows import run_text_recognition

router = APIRouter(prefix="/api/v1", tags=["education"])


class AnalysisRequest(BaseModel):
    subject: str | None = Field(default=None, max_length=120)
    academic_level: str | None = Field(default=None, max_length=120)
    topic_taxonomy: list[str] = Field(default_factory=list, max_length=100)
    consent_to_cloud: bool = False


class QuestionReview(BaseModel):
    topic: str | None = Field(default=None, max_length=500)
    subtopic: str | None = Field(default=None, max_length=500)
    difficulty: Literal["low", "medium", "high", "unclassified"] | None = None
    difficulty_reason: str | None = Field(default=None, max_length=500)
    cognitive_skill: str | None = Field(default=None, max_length=500)
    marks: int | None = Field(default=None, ge=0, le=100)
    review_status: Literal["needs_teacher_review", "teacher_reviewed", "approved"] | None = None


class AggregateRequest(BaseModel):
    job_ids: list[str] = Field(min_length=1, max_length=50)


@router.post("/demo/judge", status_code=202)
async def launch_judge_demo() -> dict:
    settings = get_settings()
    path = create_judge_demo(settings)
    try:
        job_id = await get_queue().submit(
            run_text_recognition,
            path,
            workflow_name="text_recognition",
            input_name="PredixaLearn Judge Demo — synthetic exam paper.pdf",
            delete_input=True,
            language="en",
            document_profile="exam",
        )
    except Exception:
        path.unlink(missing_ok=True)
        raise
    return {
        "job_id": job_id,
        "status": JobStatus.PENDING.value,
        "document_profile": "exam",
        "message": "Judge Demo submitted through the standard local OCR queue.",
    }


def _record(job_id: str):
    try:
        record = get_history_store().get(job_id)
    except HistoryNotFoundError as exc:
        raise HTTPException(status_code=404, detail="History record not found") from exc
    if record.status != "completed":
        raise HTTPException(status_code=409, detail="Past Paper Intelligence requires a completed OCR run")
    return record


def _education_error(exc: EducationError) -> HTTPException:
    message = str(exc)
    code = 503 if "unavailable" in message.casefold() else 422
    return HTTPException(status_code=code, detail=message)


def _analysis_record(job_id: str):
    record = _record(job_id)
    reliability = language_reliability(record.language)
    if reliability["question_segmentation"] != "supported":
        raise HTTPException(
            status_code=422,
            detail=reliability["analysis_reason"],
        )
    return record


@router.get("/history/{job_id}/analysis")
async def get_analysis(job_id: str) -> dict:
    store = get_history_store()
    try:
        return read_analysis(store, _analysis_record(job_id), create_preview=True)
    except EducationError as exc:
        raise _education_error(exc) from exc


@router.post("/history/{job_id}/analysis")
async def create_or_refresh_analysis(job_id: str, request: AnalysisRequest) -> dict:
    store = get_history_store()
    try:
        return create_analysis(
            store,
            _analysis_record(job_id),
            subject=request.subject,
            academic_level=request.academic_level,
            taxonomy=request.topic_taxonomy,
            consent_to_cloud=request.consent_to_cloud,
        )
    except EducationError as exc:
        raise _education_error(exc) from exc


@router.patch("/history/{job_id}/analysis/questions/{question_id}")
async def update_analysis_question(
    job_id: str,
    question_id: str,
    review: QuestionReview,
) -> dict:
    store = get_history_store()
    changes = review.model_dump(exclude_none=True)
    if not changes:
        raise HTTPException(status_code=422, detail="Provide at least one teacher review field")
    try:
        return update_question(store, _analysis_record(job_id), question_id, changes)
    except EducationError as exc:
        raise _education_error(exc) from exc


@router.get("/history/{job_id}/analysis/practice")
async def get_practice_suggestions(job_id: str) -> dict:
    """Get saved suggestions without contacting OpenAI or changing OCR evidence."""

    try:
        return read_practice(get_history_store(), _analysis_record(job_id))
    except EducationError as exc:
        raise _education_error(exc) from exc


@router.post("/history/{job_id}/analysis/practice")
async def prepare_practice_suggestions(job_id: str) -> dict:
    """Generate local starters, or return already validated GPT suggestions.

    This route is deliberately local-only: it never turns a practice button
    into a hidden OpenAI request.
    """

    try:
        practice = generate_practice(get_history_store(), _analysis_record(job_id))
    except EducationError as exc:
        raise _education_error(exc) from exc
    return practice


@router.get("/history/{job_id}/analysis/download")
async def download_analysis(
    job_id: str,
    format_name: Annotated[Literal["markdown", "json", "docx"], Query(alias="format")] = "markdown",
) -> FileResponse:
    store = get_history_store()
    try:
        path = analysis_download_path(store, _analysis_record(job_id), format_name)
    except EducationError as exc:
        raise _education_error(exc) from exc
    media_type = {
        "markdown": "text/markdown; charset=utf-8",
        "json": "application/json",
        "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    }[format_name]
    extension = {"markdown": "md", "json": "json", "docx": "docx"}[format_name]
    return FileResponse(path, media_type=media_type, filename=f"past-paper-analysis.{extension}")


@router.get("/history/{job_id}/source-pages/{page}")
async def get_source_preview(job_id: str, page: int) -> FileResponse:
    store = get_history_store()
    try:
        path = source_preview_path(store, _record(job_id), page)
    except EducationError as exc:
        raise _education_error(exc) from exc
    return FileResponse(path, media_type="image/png", filename=f"source-page-{page:03d}.png")


@router.get("/history/{job_id}/analysis/questions/{question_id}/crop")
async def get_question_crop(job_id: str, question_id: str) -> FileResponse:
    store = get_history_store()
    try:
        path = question_crop_path(store, _analysis_record(job_id), question_id)
    except EducationError as exc:
        raise _education_error(exc) from exc
    return FileResponse(path, media_type="image/png", filename=f"{question_id}.png")


@router.post("/analysis/aggregate")
async def aggregate_analyses(request: AggregateRequest) -> dict:
    store = get_history_store()
    analyses = []
    for job_id in dict.fromkeys(request.job_ids):
        try:
            analyses.append(read_analysis(store, _analysis_record(job_id), create_preview=False))
        except EducationError as exc:
            raise _education_error(exc) from exc
    return aggregate_history(analyses)
