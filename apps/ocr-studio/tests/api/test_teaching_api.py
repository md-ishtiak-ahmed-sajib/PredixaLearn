from __future__ import annotations

import hashlib
import io
import json
import time
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient
from PIL import Image

from app import main
from app.core import queue as queue_module
from app.core.config import get_settings
from app.documents.artifacts import ArtifactOutcome
from app.storage.history import get_history_store, reset_history_store
from app.teaching import batches as batch_module
from app.teaching.store import TeachingStore


class FakeManager:
    status = {"ocr_engine": True, "structure_engine": False, "vl_engine": False}


def image_bytes(color=(30, 90, 170)) -> bytes:
    stream = io.BytesIO()
    Image.new("RGB", (24, 16), color).save(stream, format="PNG")
    return stream.getvalue()


def seeded_result(label: str) -> dict:
    return {
        "full_text": f"1. Explain {label}. (5 marks)\n2. Compare {label}. (4 marks)",
        "lines": [
            {"line_id": "p1-l1", "page_number": 1, "text": f"1. Explain {label}. (5 marks)", "confidence": 0.61, "normalized_bbox": [0.1, 0.1, 0.9, 0.2]},
            {"line_id": "p1-l2", "page_number": 1, "text": f"2. Compare {label}. (4 marks)", "confidence": 0.93, "normalized_bbox": [0.1, 0.3, 0.9, 0.4]},
        ],
        "page_count": 1,
    }


@pytest.fixture
def teaching_client(monkeypatch, tmp_path):
    monkeypatch.setenv("OCR_OUTPUT_DIR", str(tmp_path / "output"))
    monkeypatch.setenv("OCR_UPLOAD_DIR", str(tmp_path / "uploads"))
    monkeypatch.setenv("OCR_HISTORY_DIR", str(tmp_path / "history"))
    monkeypatch.setenv("OCR_LOG_DIR", str(tmp_path / "logs"))
    monkeypatch.setenv("OCR_INSTITUTION_DIR", str(tmp_path / "institution"))
    monkeypatch.setenv("OCR_DEVICE", "cpu")
    get_settings.cache_clear()
    reset_history_store()
    monkeypatch.setattr(main, "get_manager", lambda: FakeManager())
    monkeypatch.setattr(main, "reset_manager", lambda: None)

    def batch_workflow(path, **kwargs):
        del path, kwargs
        return seeded_result("batch evidence")

    monkeypatch.setitem(batch_module._WORKFLOWS, "text_recognition", batch_workflow)
    monkeypatch.setattr(
        queue_module,
        "build_and_publish_artifacts",
        lambda path, name, workflow, result, **kwargs: ArtifactOutcome(
            result=result,
            manifest_path=None,
            archive_saved=False,
        ),
    )
    with TestClient(
        main.create_app(),
        base_url="http://127.0.0.1:8000",
        raise_server_exceptions=False,
    ) as client:
        store = get_history_store()
        created = datetime(2026, 7, 21, tzinfo=timezone.utc)
        job_ids = []
        for index, label in enumerate(("force", "force with 12 units")):
            job_id = f"{index + 1:032x}"
            result_path = store.save_result(job_id, seeded_result(label))
            store.upsert(
                job_id=job_id,
                input_name=f"paper-{index + 1}.pdf",
                workflow="text_recognition",
                input_kind="pdf",
                status="completed",
                item_count=1,
                error=None,
                result_path=result_path,
                language="en",
                document_profile="exam",
                quality_score=0.65 + index * 0.2,
                warning_count=index,
                created_at=created + timedelta(minutes=index),
                started_at=created + timedelta(minutes=index),
                completed_at=created + timedelta(minutes=index, seconds=2),
                updated_at=created + timedelta(minutes=index, seconds=2),
            )
            job_ids.append(job_id)
        yield client, tmp_path, job_ids
    reset_history_store()
    get_settings.cache_clear()


def assert_ok(response, status=200):
    assert response.status_code == status, response.text
    return response.json() if response.content else None


def test_review_correction_taxonomy_syllabus_and_exports(teaching_client):
    client, _, job_ids = teaching_client
    job_id = job_ids[0]
    workspace = assert_ok(client.get(f"/api/v1/history/{job_id}/review-workspace"))
    assert workspace["screen_reader_issues"][0]["confidence_band"] == "low"

    correction = assert_ok(
        client.post(
            f"/api/v1/history/{job_id}/corrections",
            json={
                "target_kind": "line",
                "target_id": "p1-l1",
                "original": {"text": "untrusted client value"},
                "replacement": {"text": "1. Explain the force. (5 marks)"},
                "reason": "Teacher checked the scan",
                "status": "draft",
            },
        ),
        201,
    )
    assert correction["original"]["text"] == "1. Explain force. (5 marks)"
    assert client.patch(f"/api/v1/history/{job_id}/corrections/{correction['correction_id']}", json={"status": "approved"}).status_code == 409
    correction = assert_ok(
        client.patch(
            f"/api/v1/history/{job_id}/corrections/{correction['correction_id']}",
            headers={"If-Match": correction["etag"]},
            json={"status": "approved", "actor": "teacher-1"},
        )
    )
    for format_name in ("markdown", "json", "docx"):
        assert client.get(f"/api/v1/history/{job_id}/corrected/download?format={format_name}").status_code == 200
    assert assert_ok(client.get(f"/api/v1/history/{job_id}/corrections/audit"))["events"]
    assert_ok(
        client.post(
            f"/api/v1/history/{job_id}/corrections/{correction['correction_id']}/revert",
            headers={"If-Match": correction["etag"]},
            json={"actor": "teacher-2"},
        )
    )

    taxonomy = assert_ok(
        client.post(
            "/api/v1/taxonomies",
            json={
                "name": "Physics",
                "nodes": [
                    {"node_id": "mechanics", "label": "Mechanics", "aliases": ["Forces"]},
                    {"node_id": "motion", "parent_id": "mechanics", "label": "Motion"},
                ],
            },
        ),
        201,
    )
    assert assert_ok(client.get("/api/v1/taxonomies"))["items"]
    assert client.get(f"/api/v1/taxonomies/{taxonomy['taxonomy_id']}/export?format=csv").status_code == 200
    assert client.get(f"/api/v1/taxonomies/{taxonomy['taxonomy_id']}/export?format=json").status_code == 200
    assert_ok(
        client.post(
            f"/api/v1/taxonomies/{taxonomy['taxonomy_id']}/versions",
            json={"name": "Physics", "status": "published", "nodes": taxonomy["nodes"]},
        ),
        201,
    )
    imported = assert_ok(
        client.post(
            "/api/v1/taxonomies/import",
            files={"file": ("topics.csv", b"node_id,parent_id,label,aliases\nroot,,Biology,Life|Cells\n", "text/csv")},
            data={"name": "Biology"},
        ),
        201,
    )
    assert imported["nodes"][0]["label"] == "Biology"

    syllabus = assert_ok(
        client.post(
            "/api/v1/syllabi/import",
            files={"file": ("syllabus.csv", b"objective_id,parent_id,code,label,description,aliases\nobj-1,,P1,Forces,Explain force,Mechanics\n", "text/csv")},
            data={"name": "Physics syllabus", "subject": "Physics"},
        ),
        201,
    )
    assert assert_ok(client.get(f"/api/v1/syllabi/{syllabus['syllabus_id']}"))["objectives"]
    assert assert_ok(client.get("/api/v1/syllabi"))["items"]
    published = assert_ok(
        client.post(
            f"/api/v1/syllabi/{syllabus['syllabus_id']}/versions",
            json={
                "name": syllabus["name"],
                "subject": syllabus["subject"],
                "status": "published",
                "source_type": syllabus["source_type"],
                "source_checksum": syllabus["source_checksum"],
                "warnings": syllabus["warnings"],
                "objectives": syllabus["objectives"],
            },
        ),
        201,
    )
    assert published["version"] == 2
    mapping = assert_ok(
        client.post(
            f"/api/v1/syllabi/{syllabus['syllabus_id']}/mappings",
            json={"job_id": job_id, "question_id": "q-1", "syllabus_version": syllabus["version"], "objective_id": "obj-1", "status": "approved", "method": "teacher"},
        ),
        201,
    )
    assert mapping["stale"] is False
    assert assert_ok(client.get(f"/api/v1/syllabi/{syllabus['syllabus_id']}/mappings"))["items"]


def test_duplicate_bank_comparison_dashboard_revision_and_languages(teaching_client):
    client, _, job_ids = teaching_client
    duplicate = assert_ok(client.post("/api/v1/duplicates/check", json={"job_ids": job_ids, "threshold": 0.2}))
    assert duplicate["matches"]
    match = duplicate["matches"][0]
    saved = assert_ok(
        client.post(
            "/api/v1/duplicates/check",
            json={"questions": [], "decisions": [{**match, "disposition": "variant", "explanation": {"changed_numbers": True}}]},
        )
    )
    assert saved["saved_decisions"][0]["disposition"] == "variant"

    assert client.post(
        "/api/v1/question-bank",
        json={
            "job_id": job_ids[0],
            "question_id": "q-1",
            "payload": {"text": "Unavailable", "language": "fr"},
        },
    ).status_code == 422

    bank = assert_ok(
        client.post(
            "/api/v1/question-bank",
            json={"job_id": job_ids[0], "question_id": "q-1", "status": "reviewed", "payload": {"text": "Explain the force.", "topic": "Mechanics", "marks": 5}},
        ),
        201,
    )
    bank = assert_ok(
        client.patch(
            f"/api/v1/question-bank/{bank['item_id']}",
            headers={"If-Match": bank["etag"]},
            json={"status": "approved"},
        )
    )
    assert bank["payload"]["source_line_ids"] == ["p1-l1"]
    assert assert_ok(client.get("/api/v1/question-bank?status=approved"))["items"]
    export = assert_ok(
        client.post(
            "/api/v1/question-bank/export",
            json={"formats": ["qti", "csv", "jsonl", "markdown", "docx"], "include_source_images": False},
        )
    )
    assert client.get(export["download_url"]).status_code == 200

    comparison = assert_ok(client.post("/api/v1/comparisons", json={"name": "Physics comparison", "job_ids": job_ids}), 201)
    assert comparison["report"]["paper_count"] == 2
    assert assert_ok(client.get("/api/v1/comparisons"))["items"]
    dashboard = assert_ok(client.get("/api/v1/dashboards/teacher"))
    assert dashboard["completed_papers"] == 2
    revision = assert_ok(
        client.post(
            "/api/v1/revision-packs",
            json={"title": "Mechanics revision", "status": "approved", "language": "en", "source_item_ids": [bank["item_id"]], "payload": {"introduction": "Review force."}},
        ),
        201,
    )
    assert revision["approved_by"] == "local-teacher"
    assert assert_ok(client.get("/api/v1/revision-packs?audience=student"))["items"]
    for format_name in ("markdown", "json", "docx"):
        assert client.get(
            f"/api/v1/revision-packs/{revision['pack_id']}/export?format={format_name}"
        ).status_code == 200
    reliability = assert_ok(client.get("/api/v1/languages/reliability"))
    assert [item["language"] for item in reliability["items"]] == ["en"]
    assert "English only" in reliability["gate"]
    assert client.post(
        "/api/v1/duplicates/check",
        json={"questions": [], "language": "ar"},
    ).status_code == 422
    assert client.post(
        "/api/v1/revision-packs",
        json={"title": "Unavailable language", "language": "ar"},
    ).status_code == 422


def test_persistent_batch_api_processes_recovers_exports_and_cleans_sources(teaching_client):
    client, tmp_path, _ = teaching_client
    rejected = client.post(
        "/api/v1/batches",
        files=[("files", ("french.png", image_bytes(), "image/png"))],
        data={"name": "Rejected", "language": "fr"},
    )
    assert rejected.status_code == 422
    rejected_override = client.post(
        "/api/v1/batches",
        files=[("files", ("arabic.png", image_bytes(), "image/png"))],
        data={
            "name": "Rejected override",
            "language": "en",
            "per_file_overrides": json.dumps({"arabic.png": {"language": "ar"}}),
        },
    )
    assert rejected_override.status_code == 422
    assert list((tmp_path / "uploads" / "batches").glob("*.png")) == []
    created = assert_ok(
        client.post(
            "/api/v1/batches",
            files=[
                ("files", ("one.png", image_bytes(), "image/png")),
                ("files", ("two.png", image_bytes((80, 40, 100)), "image/png")),
            ],
            data={"name": "English batch", "workflow": "text_recognition", "language": "en", "per_file_overrides": json.dumps({"two.png": {"document_profile": "exam"}})},
        ),
        201,
    )
    batch_id = created["batch_id"]
    for _ in range(100):
        batch = assert_ok(client.get(f"/api/v1/batches/{batch_id}"))
        if batch["status"] in {"completed", "partial"}:
            break
        time.sleep(0.02)
    assert batch["status"] == "completed"
    assert all(item["status"] == "completed" for item in batch["items"])
    assert client.get(f"/api/v1/batches/{batch_id}/export").status_code == 200
    assert assert_ok(client.get("/api/v1/batches"))["items"]
    assert list((tmp_path / "uploads" / "batches").glob("*.png")) == []
    assert client.delete(f"/api/v1/batches/{batch_id}").status_code == 204


def test_teaching_api_validation_not_found_and_conflict_paths(teaching_client):
    client, tmp_path, job_ids = teaching_client
    missing = "f" * 32
    assert client.get(f"/api/v1/history/{missing}/review-workspace").status_code == 404
    assert client.get(f"/api/v1/history/{missing}/corrections").status_code == 404
    assert client.get(f"/api/v1/history/{missing}/corrected/download?format=json").status_code == 404
    assert client.post(
        f"/api/v1/history/{job_ids[0]}/corrections",
        json={"target_kind": "line", "target_id": "missing", "replacement": "x", "reason": "test"},
    ).status_code == 422
    assert client.patch(
        f"/api/v1/history/{job_ids[0]}/corrections/{missing}",
        headers={"If-Match": '"missing"'},
        json={"status": "approved"},
    ).status_code == 404
    assert client.post(
        f"/api/v1/history/{job_ids[0]}/corrections/{missing}/revert",
        headers={"If-Match": '"missing"'},
        json={},
    ).status_code == 404

    assert client.post(
        "/api/v1/taxonomies",
        json={"name": "Bad", "nodes": [{"node_id": "a", "label": "A", "parent_id": "a"}]},
    ).status_code == 422
    assert client.post(
        "/api/v1/taxonomies/import",
        files={"file": ("topics.txt", b"invalid", "text/plain")},
    ).status_code == 422
    assert client.get("/api/v1/taxonomies/missing/export").status_code == 404
    assert client.post("/api/v1/taxonomies/missing/versions", json={"name": "Missing"}).status_code == 404
    assert client.post(
        "/api/v1/syllabi/import",
        files={"file": ("empty.csv", b"objective_id,label\n", "text/csv")},
    ).status_code == 422
    assert client.get("/api/v1/syllabi/missing").status_code == 404
    assert client.post(
        "/api/v1/syllabi/missing/mappings",
        json={"job_id": job_ids[0], "question_id": "q-1", "objective_id": "missing"},
    ).status_code == 404
    assert client.post(
        "/api/v1/duplicates/check",
        json={"decisions": [{"disposition": "merge"}]},
    ).status_code == 422
    assert client.post(
        "/api/v1/duplicates/check",
        json={"job_ids": [missing]},
    ).status_code == 404
    assert client.post(
        "/api/v1/question-bank",
        json={"job_id": job_ids[0], "question_id": "missing", "payload": {"text": "Unknown"}},
    ).status_code == 422
    assert client.patch(
        f"/api/v1/question-bank/{missing}",
        headers={"If-Match": '"missing"'},
        json={"status": "approved"},
    ).status_code == 404
    assert client.post("/api/v1/question-bank/export", json={"formats": ["csv"]}).status_code == 422
    assert client.get("/api/v1/question-bank/exports/not-hex").status_code == 404
    assert client.get("/api/v1/question-bank/exports/aaaaaaaaaaaaaaaa").status_code == 404
    assert client.post(
        "/api/v1/comparisons",
        json={"job_ids": [job_ids[0], job_ids[0]]},
    ).status_code == 422
    assert client.post(
        "/api/v1/revision-packs",
        json={"title": "Unsafe pack", "status": "approved", "source_item_ids": [missing]},
    ).status_code == 422

    assert client.post(
        "/api/v1/batches",
        files=[("files", ("one.png", image_bytes(), "image/png"))],
        data={"workflow": "invalid"},
    ).status_code == 422
    assert client.post(
        "/api/v1/batches",
        files=[("files", ("one.png", image_bytes(), "image/png"))],
        data={"remove_terms": "{}"},
    ).status_code == 422
    assert list((tmp_path / "uploads" / "batches").glob("*.png")) == []
    assert client.get(f"/api/v1/batches/{missing}").status_code == 404
    assert client.patch(f"/api/v1/batches/{missing}", json={"status": "paused"}).status_code == 404
    assert client.delete(f"/api/v1/batches/{missing}").status_code == 404
    assert client.get(f"/api/v1/batches/{missing}/items/{missing}").status_code == 404
    assert client.patch(f"/api/v1/batches/{missing}/items/{missing}", json={"action": "retry"}).status_code == 404
    assert client.delete(f"/api/v1/batches/{missing}/items/{missing}").status_code == 404
    assert client.get(f"/api/v1/batches/{missing}/export").status_code == 404


def test_batch_api_pause_retry_reorder_cancel_and_delete_guards(teaching_client):
    client, tmp_path, _ = teaching_client
    store = TeachingStore()
    root = tmp_path / "uploads" / "batches"
    root.mkdir(parents=True, exist_ok=True)
    entries = []
    for index in range(2):
        path = root / f"manual-{index}.png"
        path.write_bytes(image_bytes((20 + index, 30, 40)))
        entries.append(
            {
                "input_name": path.name,
                "staged_path": path.name,
                "source_sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                "media_type": "image/png",
                "size_bytes": path.stat().st_size,
            }
        )
    batch = store.create_batch(
        {"name": "Manual controls", "workflow": "text_recognition", "language": "en", "status": "paused"},
        entries,
    )
    batch_id = batch["batch_id"]
    first, second = batch["items"]
    assert_ok(client.get(f"/api/v1/batches/{batch_id}/items/{first['item_id']}"))
    assert client.patch(f"/api/v1/batches/{batch_id}/items/{first['item_id']}", json={"action": "retry"}).status_code == 409
    moved = assert_ok(client.patch(f"/api/v1/batches/{batch_id}/items/{second['item_id']}", json={"action": "move_up"}))
    assert moved["position"] == 0
    prioritized = assert_ok(client.patch(f"/api/v1/batches/{batch_id}/items/{first['item_id']}", json={"action": "process_next"}))
    assert prioritized["position"] == 0
    store.patch_batch_item(batch_id, first["item_id"], {"status": "failed", "retry_pending": True})
    assert_ok(client.patch(f"/api/v1/batches/{batch_id}/items/{first['item_id']}", json={"action": "retry"}))
    assert_ok(client.patch(f"/api/v1/batches/{batch_id}", json={"status": "paused"}))
    assert_ok(client.patch(f"/api/v1/batches/{batch_id}/items/{first['item_id']}", json={"action": "cancel"}))
    assert not (root / first["staged_path"]).exists()
    assert client.delete(f"/api/v1/batches/{batch_id}/items/{second['item_id']}").status_code == 204

    processing_path = root / "processing.png"
    processing_path.write_bytes(image_bytes())
    guarded = store.create_batch(
        {"name": "Guarded", "workflow": "text_recognition", "language": "en", "status": "paused"},
        [{"input_name": "processing.png", "staged_path": "processing.png", "source_sha256": hashlib.sha256(processing_path.read_bytes()).hexdigest(), "media_type": "image/png", "size_bytes": processing_path.stat().st_size}],
    )
    guarded_item = guarded["items"][0]
    store.patch_batch_item(guarded["batch_id"], guarded_item["item_id"], {"status": "processing"})
    assert client.delete(f"/api/v1/batches/{guarded['batch_id']}").status_code == 409
    assert client.delete(f"/api/v1/batches/{guarded['batch_id']}/items/{guarded_item['item_id']}").status_code == 409
    assert_ok(client.patch(f"/api/v1/batches/{guarded['batch_id']}", json={"status": "cancelled"}))
