from __future__ import annotations

import json
from dataclasses import replace
from datetime import datetime, timezone

import pytest
from PIL import Image

from app.core.config import get_settings
from app.education.service import (
    EducationError,
    _enrich,
    _validate_model_output,
    create_analysis,
    generate_practice,
    read_analysis,
    source_preview_path,
    update_question,
)
from app.storage.history import HistoryStore


def _store(tmp_path):
    settings = replace(
        get_settings(),
        output_dir=tmp_path / "output",
        upload_dir=tmp_path / "uploads",
        history_dir=tmp_path / "history",
        history_db_path=tmp_path / "history" / "history.sqlite3",
        log_dir=tmp_path / "logs",
    )
    store = HistoryStore(settings)
    job_id = "a" * 32
    result = {
        "full_text": "1(a) Explain continuity. (5 marks)",
        "page_count": 1,
        "lines": [
            {
                "line_id": "p0001-l00001",
                "page_number": 1,
                "text": "1(a) Explain continuity. (5 marks)",
                "confidence": 0.95,
                "normalized_bbox": [0.1, 0.2, 0.8, 0.3],
            }
        ],
    }
    result_path = store.save_result(job_id, result)
    now = datetime.now(timezone.utc)
    store.upsert(
        job_id=job_id,
        input_name="exam.pdf",
        workflow="text_recognition",
        input_kind="pdf",
        status="completed",
        item_count=1,
        error=None,
        result_path=result_path,
        created_at=now,
        started_at=now,
        completed_at=now,
        updated_at=now,
        document_profile="exam",
    )
    archive = settings.history_dir / "jobs" / job_id / "artifacts" / "exam"
    preview = archive / "source-pages" / "page-001.png"
    preview.parent.mkdir(parents=True)
    Image.new("RGB", (400, 600), "white").save(preview)
    manifest = settings.history_dir / "jobs" / job_id / "manifest.json"
    manifest.write_text(
        json.dumps({"archive_root": archive.relative_to(settings.history_dir).as_posix(), "files": {}}),
        encoding="utf-8",
    )
    store.upsert(
        job_id=job_id,
        input_name="exam.pdf",
        workflow="text_recognition",
        input_kind="pdf",
        status="completed",
        item_count=1,
        error=None,
        result_path=result_path,
        artifact_manifest_path=manifest.relative_to(settings.history_dir).as_posix(),
        created_at=now,
        started_at=now,
        completed_at=now,
        updated_at=now,
        document_profile="exam",
    )
    return store, store.get(job_id)


def test_local_analysis_is_source_linked_and_teacher_edits_are_audited(tmp_path):
    store, record = _store(tmp_path)
    analysis = create_analysis(
        store,
        record,
        subject="Civil Engineering",
        academic_level="Undergraduate",
        taxonomy=["Hydraulics"],
        consent_to_cloud=False,
    )
    assert analysis["model"]["consent_to_cloud"] is False
    assert analysis["questions"][0]["source_line_ids"] == ["p0001-l00001"]
    assert source_preview_path(store, record, 1).is_file()
    updated = update_question(store, record, "q-1-a", {"topic": "Hydraulics", "review_status": "approved"})
    question = updated["questions"][0]
    assert question["teacher_override"]["topic"] == "Hydraulics"
    assert question["review_status"] == "approved"
    assert read_analysis(store, record, create_preview=False)["analysis"]["topic_distribution"][0]["topic"] == "Hydraulics"
    refreshed = create_analysis(
        store,
        record,
        subject="Civil Engineering",
        academic_level="Undergraduate",
        taxonomy=["Hydraulics"],
        consent_to_cloud=False,
    )
    assert refreshed["questions"][0]["teacher_override"]["topic"] == "Hydraulics"
    audit = (store.settings.history_dir / "jobs" / record.job_id / "education" / "audit.jsonl").read_text(encoding="utf-8")
    assert "teacher_override" in audit


def test_model_output_rejects_unknown_or_ungrounded_evidence(tmp_path):
    store, record = _store(tmp_path)
    analysis = create_analysis(store, record, subject=None, academic_level=None, taxonomy=None, consent_to_cloud=False)
    with pytest.raises(EducationError, match="unknown question"):
        _validate_model_output(
            analysis,
            {"questions": [{"question_id": "q-not-real", "source_page": 1, "source_line_ids": ["p0001-l00001"]}], "practice_items": []},
        )
    with pytest.raises(EducationError, match="invalid source line"):
        _validate_model_output(
            analysis,
            {"questions": [{"question_id": "q-1-a", "source_page": 1, "source_line_ids": ["p0009-l00001"]}], "practice_items": []},
        )


def test_local_practice_generation_stays_source_linked_and_needs_teacher_review(tmp_path):
    store, record = _store(tmp_path)
    create_analysis(store, record, subject=None, academic_level=None, taxonomy=None, consent_to_cloud=False)
    practice = generate_practice(store, record)
    assert practice["consent_to_cloud"] is False
    assert practice["review_required"] is True
    assert practice["items"][0]["source_question_id"] == "q-1-a"
    assert practice["items"][0]["review_status"] == "needs_teacher_review"
    assert "parallel practice question" in practice["items"][0]["prompt"]


def test_sol_failure_uses_terra_only_for_recoverable_analysis_failure(tmp_path):
    store, record = _store(tmp_path)
    analysis = create_analysis(store, record, subject=None, academic_level=None, taxonomy=None, consent_to_cloud=False)

    class Response:
        output_text = json.dumps(
            {
                "questions": [
                    {
                        "question_id": "q-1-a",
                        "source_page": 1,
                        "source_line_ids": ["p0001-l00001"],
                        "topic": "Hydraulics",
                        "subtopic": None,
                        "question_type": "short answer",
                        "difficulty": "medium",
                        "difficulty_reason": "Requires explanation.",
                        "cognitive_skill": "explain",
                        "classification_confidence": 0.7,
                        "marks_inference": {"marks": 5, "reason": "Explicit OCR evidence."},
                    }
                ],
                "practice_items": [],
            }
        )

    class Client:
        def __init__(self):
            self.models: list[str] = []

        def create(self, **kwargs):
            self.models.append(kwargs["model"])
            if kwargs["model"] == "gpt-5.6-sol":
                raise RuntimeError("temporary upstream failure")
            return Response()

    client = Client()
    _enrich(analysis, client=client)
    assert client.models == ["gpt-5.6-sol", "gpt-5.6-terra"]
    assert analysis["model"]["model"] == "gpt-5.6-terra"
    assert analysis["model"]["fallback_used"] is True
