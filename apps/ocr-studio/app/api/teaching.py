"""Local evidence review, teaching, revision, and batch APIs."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Annotated, Any, Literal

from fastapi import APIRouter, File, Form, Header, HTTPException, Query, UploadFile
from fastapi.responses import FileResponse, Response
from pydantic import BaseModel, Field

from app.storage.history import HistoryNotFoundError
from app.teaching.batches import build_batch_manifest, file_sha256
from app.teaching.languages import reliability_registry
from app.teaching.service import (
    compare_papers,
    corrected_export_path,
    duplicate_matches,
    export_question_bank,
    parse_syllabus,
    parse_taxonomy,
    review_workspace,
    revision_pack_export_path,
    taxonomy_export,
)
from app.teaching.store import (
    TeachingConflictError,
    TeachingStore,
    TeachingValidationError,
)

router = APIRouter(prefix="/api/v1", tags=["teaching"])


def _store() -> TeachingStore:
    return TeachingStore()


def _not_found(exc: Exception) -> HTTPException:
    return HTTPException(status_code=404, detail=str(exc))


def _invalid(exc: Exception) -> HTTPException:
    return HTTPException(status_code=422, detail=str(exc))


def _conflict(exc: Exception) -> HTTPException:
    return HTTPException(status_code=409, detail=str(exc))


class CorrectionRequest(BaseModel):
    target_kind: Literal["line", "block", "question", "marks", "table_cell", "reconstructed_text"]
    target_id: str = Field(min_length=1, max_length=256)
    original: Any = None
    replacement: Any
    reason: str = Field(min_length=1, max_length=1000)
    actor: str = Field(default="local-teacher", max_length=128)
    status: Literal["draft", "reviewed", "approved", "rejected"] = "draft"
    source_line_ids: list[str] = Field(default_factory=list, max_length=500)
    geometry: list[float] | None = None


class CorrectionPatch(BaseModel):
    replacement: Any = None
    reason: str | None = Field(default=None, max_length=1000)
    actor: str = Field(default="local-teacher", max_length=128)
    status: Literal["draft", "reviewed", "approved", "rejected"] | None = None


class ActorRequest(BaseModel):
    actor: str = Field(default="local-teacher", max_length=128)


class TaxonomyRequest(BaseModel):
    name: str = Field(min_length=1, max_length=256)
    subject: str | None = Field(default=None, max_length=128)
    academic_level: str | None = Field(default=None, max_length=128)
    status: Literal["draft", "published", "superseded", "retired"] = "draft"
    nodes: list[dict[str, Any]] = Field(default_factory=list, max_length=2000)
    actor: str = Field(default="local-teacher", max_length=128)


class MappingRequest(BaseModel):
    job_id: str
    question_id: str
    syllabus_version: int | None = None
    objective_id: str
    topic_id: str | None = None
    status: Literal["draft", "approved", "rejected"] = "draft"
    method: Literal["teacher", "deterministic", "ai_suggestion"] = "teacher"
    confidence: float | None = Field(default=None, ge=0, le=1)
    actor: str = Field(default="local-teacher", max_length=128)


class SyllabusVersionRequest(BaseModel):
    name: str = Field(min_length=1, max_length=256)
    subject: str | None = Field(default=None, max_length=128)
    academic_level: str | None = Field(default=None, max_length=128)
    status: Literal["draft", "published", "superseded", "retired"] = "draft"
    source_type: str = Field(max_length=64)
    source_checksum: str = Field(min_length=64, max_length=64)
    effective_from: str | None = None
    effective_to: str | None = None
    warnings: list[str] = Field(default_factory=list, max_length=500)
    objectives: list[dict[str, Any]] = Field(min_length=1, max_length=5000)
    actor: str = Field(default="local-teacher", max_length=128)


class DuplicateRequest(BaseModel):
    questions: list[dict[str, Any]] | None = Field(default=None, max_length=5000)
    job_ids: list[str] = Field(default_factory=list, max_length=50)
    language: Literal["en"] = "en"
    threshold: float = Field(default=0.55, ge=0, le=1)
    decisions: list[dict[str, Any]] = Field(default_factory=list, max_length=500)


class BankRequest(BaseModel):
    job_id: str
    question_id: str
    status: Literal["draft", "reviewed", "approved", "rejected"] = "draft"
    payload: dict[str, Any]
    actor: str = Field(default="local-teacher", max_length=128)


class BankPatch(BaseModel):
    status: Literal["draft", "reviewed", "approved", "rejected"] | None = None
    payload: dict[str, Any] | None = None
    actor: str = Field(default="local-teacher", max_length=128)


class BankExportRequest(BaseModel):
    formats: list[Literal["qti", "csv", "jsonl", "markdown", "docx"]] = Field(min_length=1)
    include_source_images: bool = False


class ComparisonRequest(BaseModel):
    name: str = Field(default="Paper comparison", max_length=256)
    job_ids: list[str] = Field(min_length=2, max_length=50)
    taxonomy_ref: str | None = None
    syllabus_ref: str | None = None
    actor: str = Field(default="local-teacher", max_length=128)


class RevisionRequest(BaseModel):
    pack_id: str | None = None
    title: str = Field(min_length=1, max_length=256)
    status: Literal["draft", "reviewed", "approved", "retired"] = "draft"
    course_ref: str | None = Field(default=None, max_length=128)
    cohort_ref: str | None = Field(default=None, max_length=128)
    language: Literal["en"] = "en"
    source_item_ids: list[str] = Field(default_factory=list, max_length=1000)
    payload: dict[str, Any] = Field(default_factory=dict)
    actor: str = Field(default="local-teacher", max_length=128)


class BatchPatch(BaseModel):
    status: Literal["queued", "paused", "cancelled"]
    actor: str = Field(default="local-teacher", max_length=128)


class BatchItemPatch(BaseModel):
    action: Literal["retry", "cancel", "move_up", "move_down", "process_next"]
    actor: str = Field(default="local-teacher", max_length=128)


@router.get("/history/{job_id}/review-workspace")
async def get_review_workspace(job_id: str) -> dict[str, Any]:
    try:
        return review_workspace(_store(), job_id)
    except (HistoryNotFoundError, KeyError) as exc:
        raise _not_found(exc) from exc
    except Exception as exc:
        if exc.__class__.__name__ in {"EducationError", "HistoryResultError"}:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        raise


@router.get("/history/{job_id}/corrections")
async def get_corrections(job_id: str) -> dict[str, Any]:
    try:
        return _store().list_corrections(job_id)
    except (HistoryNotFoundError, KeyError) as exc:
        raise _not_found(exc) from exc


@router.post("/history/{job_id}/corrections", status_code=201)
async def create_correction(job_id: str, request: CorrectionRequest) -> dict[str, Any]:
    try:
        return _store().create_correction(job_id, request.model_dump())
    except TeachingValidationError as exc:
        raise _invalid(exc) from exc
    except TeachingConflictError as exc:
        raise _conflict(exc) from exc
    except HistoryNotFoundError as exc:
        raise _not_found(exc) from exc


@router.patch("/history/{job_id}/corrections/{correction_id}")
async def patch_correction(job_id: str, correction_id: str, request: CorrectionPatch, if_match: Annotated[str | None, Header()] = None) -> dict[str, Any]:
    store = _store()
    try:
        existing = store.get_correction(correction_id)
        if existing["job_id"] != job_id:
            raise KeyError("Correction not found")
        return store.update_correction(correction_id, request.model_dump(exclude_none=True), if_match)
    except KeyError as exc:
        raise _not_found(exc) from exc
    except TeachingValidationError as exc:
        raise _invalid(exc) from exc
    except TeachingConflictError as exc:
        raise _conflict(exc) from exc


@router.post("/history/{job_id}/corrections/{correction_id}/revert")
async def revert_correction(job_id: str, correction_id: str, request: ActorRequest, if_match: Annotated[str | None, Header()] = None) -> dict[str, Any]:
    store = _store()
    try:
        existing = store.get_correction(correction_id)
        if existing["job_id"] != job_id:
            raise KeyError("Correction not found")
        return store.revert_correction(correction_id, actor=request.actor, if_match=if_match)
    except KeyError as exc:
        raise _not_found(exc) from exc
    except TeachingConflictError as exc:
        raise _conflict(exc) from exc


@router.get("/history/{job_id}/corrections/audit")
async def correction_audit(job_id: str) -> dict[str, Any]:
    return {"job_id": job_id, "events": _store().audit(job_id=job_id)}


@router.get("/history/{job_id}/corrected/download")
async def download_corrected_view(
    job_id: str,
    format_name: Annotated[
        Literal["markdown", "json", "docx"], Query(alias="format")
    ] = "markdown",
) -> FileResponse:
    try:
        path = corrected_export_path(_store(), job_id, format_name)
    except (HistoryNotFoundError, KeyError) as exc:
        raise _not_found(exc) from exc
    media_type = {
        "markdown": "text/markdown; charset=utf-8",
        "json": "application/json",
        "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    }[format_name]
    return FileResponse(path, media_type=media_type, filename=path.name)


@router.get("/taxonomies")
async def list_taxonomies() -> dict[str, Any]:
    return {"items": _store().list_taxonomies()}


@router.post("/taxonomies", status_code=201)
async def create_taxonomy(request: TaxonomyRequest) -> dict[str, Any]:
    try:
        return _store().create_taxonomy(request.model_dump())
    except TeachingValidationError as exc:
        raise _invalid(exc) from exc


@router.post("/taxonomies/import", status_code=201)
async def import_taxonomy(
    file: Annotated[UploadFile, File()],
    name: Annotated[str, Form()] = "Imported taxonomy",
    subject: Annotated[str | None, Form()] = None,
    academic_level: Annotated[str | None, Form()] = None,
    actor: Annotated[str, Form()] = "local-teacher",
) -> dict[str, Any]:
    data = await file.read(10 * 1024 * 1024 + 1)
    await file.close()
    if len(data) > 10 * 1024 * 1024:
        raise HTTPException(status_code=413, detail="Taxonomy upload exceeds the 10 MB limit")
    try:
        parsed = parse_taxonomy(data, file.filename or "taxonomy.json")
        return _store().create_taxonomy(
            {
                **parsed,
                "name": name,
                "subject": subject,
                "academic_level": academic_level,
                "actor": actor,
            }
        )
    except (ValueError, json.JSONDecodeError, TeachingValidationError) as exc:
        raise _invalid(exc) from exc


@router.get("/taxonomies/{taxonomy_id}/export")
async def export_taxonomy(
    taxonomy_id: str,
    format_name: Annotated[Literal["csv", "json"], Query(alias="format")] = "json",
    version: int | None = Query(default=None, ge=1),
) -> Response:
    try:
        body, media_type, filename = taxonomy_export(
            _store().get_taxonomy(taxonomy_id, version), format_name
        )
    except KeyError as exc:
        raise _not_found(exc) from exc
    return Response(
        content=body,
        media_type=media_type,
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.post("/taxonomies/{taxonomy_id}/versions", status_code=201)
async def create_taxonomy_version(taxonomy_id: str, request: TaxonomyRequest) -> dict[str, Any]:
    try:
        prior = _store().get_taxonomy(taxonomy_id)
        if prior["status"] == "published" and request.status == "draft":
            pass
        return _store().create_taxonomy(request.model_dump(), taxonomy_id=taxonomy_id)
    except KeyError as exc:
        raise _not_found(exc) from exc
    except TeachingValidationError as exc:
        raise _invalid(exc) from exc


@router.post("/syllabi/import", status_code=201)
async def import_syllabus(
    file: Annotated[UploadFile, File()],
    name: Annotated[str, Form()] = "Imported syllabus",
    subject: Annotated[str | None, Form()] = None,
    academic_level: Annotated[str | None, Form()] = None,
    actor: Annotated[str, Form()] = "local-teacher",
) -> dict[str, Any]:
    data = await file.read(25 * 1024 * 1024 + 1)
    await file.close()
    if len(data) > 25 * 1024 * 1024:
        raise HTTPException(status_code=413, detail="Syllabus upload exceeds the 25 MB limit")
    try:
        parsed = parse_syllabus(data, file.filename or "syllabus", file.content_type)
        return _store().save_syllabus({**parsed, "name": name, "subject": subject, "academic_level": academic_level, "actor": actor})
    except (ValueError, json.JSONDecodeError, TeachingValidationError) as exc:
        raise _invalid(exc) from exc


@router.get("/syllabi")
async def list_syllabi() -> dict[str, Any]:
    return {"items": _store().list_syllabi()}


@router.get("/syllabi/{syllabus_id}")
async def get_syllabus(syllabus_id: str, version: int | None = Query(default=None, ge=1)) -> dict[str, Any]:
    try:
        return _store().get_syllabus(syllabus_id, version)
    except KeyError as exc:
        raise _not_found(exc) from exc


@router.post("/syllabi/{syllabus_id}/versions", status_code=201)
async def create_syllabus_version(
    syllabus_id: str,
    request: SyllabusVersionRequest,
) -> dict[str, Any]:
    store = _store()
    try:
        store.get_syllabus(syllabus_id)
        return store.save_syllabus(
            {**request.model_dump(), "syllabus_id": syllabus_id}
        )
    except KeyError as exc:
        raise _not_found(exc) from exc
    except TeachingValidationError as exc:
        raise _invalid(exc) from exc


@router.get("/syllabi/{syllabus_id}/mappings")
async def get_syllabus_mappings(syllabus_id: str) -> dict[str, Any]:
    return {"items": _store().list_mappings(syllabus_id)}


@router.post("/syllabi/{syllabus_id}/mappings", status_code=201)
async def create_syllabus_mapping(syllabus_id: str, request: MappingRequest) -> dict[str, Any]:
    try:
        return _store().save_mapping(syllabus_id, request.model_dump())
    except KeyError as exc:
        raise _not_found(exc) from exc
    except (TeachingValidationError, ValueError) as exc:
        raise _invalid(exc) from exc


@router.post("/duplicates/check")
async def check_duplicates(request: DuplicateRequest) -> dict[str, Any]:
    store = _store()
    questions = list(request.questions or [])
    for job_id in request.job_ids:
        try:
            workspace = review_workspace(store, job_id)
        except (HistoryNotFoundError, KeyError) as exc:
            raise _not_found(exc) from exc
        questions.extend(
            {
                "question_key": f"{job_id}:{question.get('question_id')}",
                "job_id": job_id,
                **question,
            }
            for question in workspace["questions"]
        )
    matches = duplicate_matches(questions, threshold=request.threshold, language=request.language)
    saved = []
    for decision in request.decisions:
        if decision.get("disposition") not in {"duplicate", "variant", "related", "unrelated"}:
            raise HTTPException(status_code=422, detail="Duplicate disposition is invalid")
        saved.append(store.save_duplicate_decision(decision))
    return {"question_count": len(questions), "matches": matches, "saved_decisions": saved, "automatic_merges": 0}


@router.get("/question-bank")
async def list_question_bank(status: str | None = None, query: str = "", limit: int = Query(default=200, ge=1, le=500)) -> dict[str, Any]:
    return {"items": _store().list_bank(status=status, query=query, limit=limit)}


@router.post("/question-bank", status_code=201)
async def create_question_bank_item(request: BankRequest) -> dict[str, Any]:
    try:
        return _store().upsert_bank_item(request.model_dump())
    except (TeachingValidationError, ValueError) as exc:
        raise _invalid(exc) from exc
    except HistoryNotFoundError as exc:
        raise _not_found(exc) from exc


@router.patch("/question-bank/{item_id}")
async def patch_question_bank_item(item_id: str, request: BankPatch, if_match: Annotated[str | None, Header()] = None) -> dict[str, Any]:
    store = _store()
    try:
        existing = store.get_bank_item(item_id)
    except KeyError as exc:
        raise _not_found(exc) from exc
    if if_match != existing["etag"]:
        raise HTTPException(status_code=409, detail="The question-bank item changed; reload before saving")
    return store.upsert_bank_item({"job_id": existing["job_id"], "question_id": existing["question_id"], "status": request.status or existing["status"], "payload": request.payload if request.payload is not None else existing["payload"], "actor": request.actor})


@router.post("/question-bank/export")
async def export_bank(request: BankExportRequest) -> dict[str, Any]:
    try:
        return export_question_bank(_store(), formats=request.formats, include_source_images=request.include_source_images)
    except ValueError as exc:
        raise _invalid(exc) from exc


@router.get("/question-bank/exports/{export_id}")
async def download_bank_export(export_id: str) -> FileResponse:
    if not re_full_hex(export_id, 16):
        raise HTTPException(status_code=404, detail="Export not found")
    store = _store()
    path = (store.settings.history_dir / "question-bank-exports" / export_id / "predixalearn-question-bank.zip").resolve()
    root = (store.settings.history_dir / "question-bank-exports").resolve()
    try:
        path.relative_to(root)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail="Export not found") from exc
    if not path.is_file():
        raise HTTPException(status_code=404, detail="Export not found")
    return FileResponse(path, media_type="application/zip", filename="predixalearn-question-bank.zip")


def re_full_hex(value: str, length: int) -> bool:
    return len(value) == length and all(character in "0123456789abcdef" for character in value)


@router.get("/comparisons")
async def list_comparisons() -> dict[str, Any]:
    return {"items": _store().list_comparisons()}


@router.post("/comparisons", status_code=201)
async def create_comparison(request: ComparisonRequest) -> dict[str, Any]:
    store = _store()
    try:
        report = compare_papers(store.history, store, request.job_ids)
        return store.save_comparison({**request.model_dump(), "source_hashes": report["source_hashes"], "analysis_versions": {}, "report": report})
    except HistoryNotFoundError as exc:
        raise _not_found(exc) from exc
    except ValueError as exc:
        raise _invalid(exc) from exc


@router.get("/dashboards/teacher")
async def teacher_dashboard() -> dict[str, Any]:
    store = _store()
    records, total = store.history.list(status="completed", limit=20)
    counts = store.dashboard_counts()
    return {"mode": "standalone_teacher", "student_accounts": False, "completed_papers": total, "papers_awaiting_review": [record.summary() for record in records if record.analysis_status != "completed"], **counts}


@router.get("/revision-packs")
async def list_revision_packs(audience: Literal["teacher", "student"] = "teacher") -> dict[str, Any]:
    approved_only = audience == "student"
    return {"mode": "standalone_teacher_preview", "student_accounts": False, "items": _store().list_revision_packs(approved_only=approved_only)}


@router.post("/revision-packs", status_code=201)
async def create_revision_pack(request: RevisionRequest) -> dict[str, Any]:
    store = _store()
    if request.status == "approved":
        approved = {item["item_id"] for item in store.list_bank(status="approved") if not item.get("stale")}
        unknown = set(request.source_item_ids) - approved
        if unknown:
            raise HTTPException(status_code=422, detail="Approved revision packs may contain only current teacher-approved question-bank items")
    try:
        return store.save_revision_pack(request.model_dump())
    except TeachingValidationError as exc:
        raise _invalid(exc) from exc


@router.get("/revision-packs/{pack_id}/export")
async def export_revision_pack(
    pack_id: str,
    format_name: Annotated[
        Literal["markdown", "json", "docx"], Query(alias="format")
    ] = "markdown",
) -> FileResponse:
    try:
        path = revision_pack_export_path(_store(), pack_id, format_name)
    except KeyError as exc:
        raise _not_found(exc) from exc
    except ValueError as exc:
        raise _invalid(exc) from exc
    media_type = {
        "markdown": "text/markdown; charset=utf-8",
        "json": "application/json",
        "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    }[format_name]
    return FileResponse(path, media_type=media_type, filename=path.name)


@router.get("/languages/reliability")
async def english_language_reliability() -> dict[str, Any]:
    return {
        "items": reliability_registry(),
        "gate": (
            "This release accepts English only. Additional languages can be "
            "implemented later after language-specific OCR, analysis, and export validation."
        ),
    }


@router.get("/batches")
async def list_batches() -> dict[str, Any]:
    return {"items": _store().list_batches(), "concurrency": 1, "persistent": True}


@router.post("/batches", status_code=201)
async def create_batch(
    files: Annotated[list[UploadFile], File()],
    name: Annotated[str, Form()] = "OCR batch",
    workflow: Annotated[str, Form()] = "text_recognition",
    language: Annotated[str, Form()] = "en",
    document_profile: Annotated[str, Form()] = "auto",
    remove_terms: Annotated[str, Form()] = "[]",
    per_file_overrides: Annotated[str, Form()] = "{}",
    actor: Annotated[str, Form()] = "local-teacher",
) -> dict[str, Any]:
    if not 1 <= len(files) <= 100:
        raise HTTPException(status_code=422, detail="A batch must contain between 1 and 100 files")
    if workflow not in {"text_recognition", "layout_parsing", "table_extraction", "vl_processing"}:
        raise HTTPException(status_code=422, detail="Batch workflow is invalid")
    if language.strip().casefold() != "en":
        raise HTTPException(
            status_code=422,
            detail="This PredixaLearn release accepts English documents only",
        )
    store = _store()
    root = store.settings.upload_dir / "batches"
    root.mkdir(parents=True, exist_ok=True)
    staged = []
    created: list[Path] = []
    try:
        terms = json.loads(remove_terms)
        if not isinstance(terms, list) or any(not isinstance(value, str) for value in terms):
            raise ValueError("Removal terms must be a JSON array of text values")
        overrides = json.loads(per_file_overrides)
        if not isinstance(overrides, dict):
            raise ValueError("Per-file overrides must be a JSON object keyed by filename")
        for upload in files:
            from app.api.ocr import _save_upload

            saved = await _save_upload(upload)
            target = root / f"{hashlib.sha256((saved.name + (upload.filename or '')).encode()).hexdigest()[:20]}{saved.suffix}"
            target.parent.mkdir(parents=True, exist_ok=True)
            saved.replace(target)
            created.append(target)
            input_name = upload.filename or target.name
            item_overrides = overrides.get(input_name, {})
            if not isinstance(item_overrides, dict):
                raise ValueError(f"Overrides for {input_name} must be an object")
            override_language = str(item_overrides.get("language", "en")).casefold()
            if override_language != "en":
                raise ValueError(
                    f"Overrides for {input_name} must use the English language code 'en'"
                )
            staged.append({"input_name": input_name, "staged_path": str(target.relative_to(root)), "source_sha256": file_sha256(target), "media_type": upload.content_type or "application/octet-stream", "size_bytes": target.stat().st_size, "overrides": item_overrides})
        return store.create_batch({"name": name, "workflow": workflow, "language": language, "document_profile": document_profile, "remove_terms": terms, "actor": actor, "status": "queued"}, staged)
    except ValueError as exc:
        for path in created:
            path.unlink(missing_ok=True)
        raise _invalid(exc) from exc
    except Exception:
        for path in created:
            path.unlink(missing_ok=True)
        raise


@router.get("/batches/{batch_id}")
async def get_batch(batch_id: str) -> dict[str, Any]:
    try:
        return _store().get_batch(batch_id)
    except KeyError as exc:
        raise _not_found(exc) from exc


@router.patch("/batches/{batch_id}")
async def patch_batch(batch_id: str, request: BatchPatch) -> dict[str, Any]:
    store = _store()
    try:
        batch = store.get_batch(batch_id)
        if request.status == "cancelled":
            for item in batch["items"]:
                if item["status"] == "processing" and item.get("job_id"):
                    from app.core.queue import get_queue

                    get_queue().cancel(item["job_id"])
                if item["status"] in {"queued", "failed", "interrupted"}:
                    store.patch_batch_item(batch_id, item["item_id"], {"status": "cancelled"})
                    safe_delete_staged(store, item["staged_path"])
        return store.patch_batch(batch_id, request.model_dump())
    except KeyError as exc:
        raise _not_found(exc) from exc
    except TeachingConflictError as exc:
        raise _conflict(exc) from exc


@router.delete("/batches/{batch_id}", status_code=204)
async def delete_batch(batch_id: str) -> None:
    store = _store()
    try:
        paths = store.delete_batch(batch_id)
        for path in paths:
            safe_delete_staged(store, path)
    except KeyError as exc:
        raise _not_found(exc) from exc
    except TeachingConflictError as exc:
        raise _conflict(exc) from exc


@router.get("/batches/{batch_id}/items/{item_id}")
async def get_batch_item(batch_id: str, item_id: str) -> dict[str, Any]:
    try:
        return next(item for item in _store().get_batch(batch_id)["items"] if item["item_id"] == item_id)
    except (KeyError, StopIteration) as exc:
        raise _not_found(KeyError("Batch item not found")) from exc


@router.patch("/batches/{batch_id}/items/{item_id}")
async def patch_batch_item(batch_id: str, item_id: str, request: BatchItemPatch) -> dict[str, Any]:
    store = _store()
    try:
        item = next(value for value in store.get_batch(batch_id)["items"] if value["item_id"] == item_id)
        if request.action == "retry":
            if item["status"] not in {"failed", "interrupted"}:
                raise TeachingConflictError("Only failed or interrupted items can be retried")
            updated = store.patch_batch_item(batch_id, item_id, {"status": "queued", "retry_pending": True, "error": None})
            store.patch_batch(batch_id, {"status": "queued", "actor": request.actor})
            return updated
        if request.action == "cancel":
            if item.get("job_id") and item["status"] == "processing":
                from app.core.queue import get_queue

                get_queue().cancel(item["job_id"])
            updated = store.patch_batch_item(batch_id, item_id, {"status": "cancelled"})
            safe_delete_staged(store, item["staged_path"])
            return updated
        if request.action == "process_next":
            updated = store.prioritize_batch_item(batch_id, item_id)
            store.patch_batch(batch_id, {"status": "queued", "actor": request.actor})
            return updated
        return store.reorder_batch_item(
            batch_id,
            item_id,
            direction=-1 if request.action == "move_up" else 1,
        )
    except (KeyError, StopIteration) as exc:
        raise _not_found(KeyError("Batch item not found")) from exc
    except TeachingConflictError as exc:
        raise _conflict(exc) from exc


@router.delete("/batches/{batch_id}/items/{item_id}", status_code=204)
async def delete_batch_item(batch_id: str, item_id: str) -> None:
    store = _store()
    try:
        item = next(value for value in store.get_batch(batch_id)["items"] if value["item_id"] == item_id)
        if item["status"] == "processing":
            raise TeachingConflictError("Cancel a processing item before removing it")
        safe_delete_staged(store, item["staged_path"])
        with store.connect() as connection:
            connection.execute("DELETE FROM batch_items WHERE batch_id = ? AND item_id = ?", (batch_id, item_id))
    except (KeyError, StopIteration) as exc:
        raise _not_found(KeyError("Batch item not found")) from exc
    except TeachingConflictError as exc:
        raise _conflict(exc) from exc


def safe_delete_staged(store: TeachingStore, value: str) -> None:
    root = (store.settings.upload_dir / "batches").resolve()
    path = (root / value).resolve() if not Path(value).is_absolute() else Path(value).resolve()
    try:
        path.relative_to(root)
    except ValueError as exc:
        raise TeachingConflictError("Stored batch source path is unsafe") from exc
    path.unlink(missing_ok=True)


@router.get("/batches/{batch_id}/export")
async def export_batch_manifest(batch_id: str) -> FileResponse:
    try:
        path = build_batch_manifest(_store(), batch_id)
    except KeyError as exc:
        raise _not_found(exc) from exc
    return FileResponse(
        path,
        media_type="application/zip",
        filename=f"predixalearn-batch-{batch_id[:8]}-manifest.zip",
    )
