from __future__ import annotations

import io
import json
import re
import time
import zipfile
from contextlib import contextmanager

import pytest
from fastapi.testclient import TestClient
from PIL import Image

from app import main
from app.api import ocr as api
from app.api import runtime as runtime_api
from app.core import queue as queue_module
from app.core.config import get_settings
from app.documents.artifacts import ArtifactOutcome
from app.maintenance.service import MaintenanceError, MaintenanceService


class FakeManager:
    status = {"ocr_engine": True, "structure_engine": False, "vl_engine": False}

    @contextmanager
    def session(self, name):
        del name
        yield object()


def png_bytes() -> bytes:
    stream = io.BytesIO()
    Image.new("RGB", (12, 8), (20, 100, 180)).save(stream, format="PNG")
    return stream.getvalue()


@pytest.fixture
def client(monkeypatch, tmp_path):
    monkeypatch.setenv("OCR_OUTPUT_DIR", str(tmp_path / "output"))
    monkeypatch.setenv("OCR_UPLOAD_DIR", str(tmp_path / "uploads"))
    monkeypatch.setenv("OCR_HISTORY_DIR", str(tmp_path / "history"))
    monkeypatch.setenv("OCR_LOG_DIR", str(tmp_path / "logs"))
    monkeypatch.setenv("OCR_INSTITUTION_DIR", str(tmp_path / "institution"))
    monkeypatch.setenv("OCR_MAX_UPLOAD_MB", "1")
    get_settings.cache_clear()
    fake_manager = FakeManager()
    monkeypatch.setattr(main, "get_manager", lambda: fake_manager)
    monkeypatch.setattr(main, "reset_manager", lambda: None)
    monkeypatch.setattr(api, "get_manager", lambda: fake_manager)

    def text_workflow(path, **kwargs):
        assert kwargs["job_id"] == kwargs["output_dir"].name
        return {"lines": [], "full_text": "hello", "page_count": 1}

    def layout_workflow(path, **kwargs):
        return {"markdown": "# title", "elements": [], "saved_files": {}}

    def table_workflow(path, **kwargs):
        return {"tables": [], "table_count": 0}

    def vl_workflow(path, **kwargs):
        return {
            "markdown": "formula",
            "latex_formulas": ["x"],
            "elements": [],
            "saved_files": {},
        }

    monkeypatch.setattr(api, "run_text_recognition", text_workflow)
    monkeypatch.setattr(api, "run_layout_parsing", layout_workflow)
    monkeypatch.setattr(api, "run_table_extraction", table_workflow)
    monkeypatch.setattr(api, "run_vl_processing", vl_workflow)
    def fake_artifacts(path, name, workflow, result, **kwargs):
        del path, workflow
        settings = kwargs["settings"]
        job_id = kwargs["job_id"]
        stem = "unsafe_name_1" if name.startswith("unsafe name") else "scan"
        archive = settings.history_dir / "jobs" / job_id / "artifacts" / stem
        archive.mkdir(parents=True, exist_ok=True)
        (archive / f"{stem}.md").write_text("# Generated\n", encoding="utf-8")
        (archive / f"{stem}.docx").write_bytes(b"fake docx")
        table_files = []
        if name.startswith("unsafe name"):
            (archive / "tables").mkdir()
            (archive / "tables" / "table-001.csv").write_text(
                "Year,1950\nPopulation,25\n",
                encoding="utf-8",
            )
            (archive / "tables" / "table-001.xlsx").write_bytes(b"fake xlsx")
            table_files = ["tables/table-001.xlsx", "tables/table-001.csv"]
        manifest = {
            "archive_root": archive.relative_to(settings.history_dir).as_posix(),
            "files": {
                "markdown": f"{stem}.md",
                "docx": f"{stem}.docx",
                "json": None,
                "images": [],
                "tables": table_files,
            },
            "legacy": False,
        }
        manifest_path = settings.history_dir / "jobs" / job_id / "manifest.json"
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
        value = dict(result)
        value.update(
            {
                "markdown": "# Generated\n",
                "artifact_status": "complete",
                "artifacts": {"markdown": f"{stem}/{stem}.md", "docx": f"{stem}/{stem}.docx"},
            }
        )
        if kwargs.get("remove_text"):
            value["removed_text"] = kwargs["remove_text"]
        if kwargs.get("remove_terms"):
            value["removed_terms"] = [
                {"term": term, "removed_count": 1} for term in kwargs["remove_terms"]
            ]
        if table_files:
            value["table_count"] = 1
        return ArtifactOutcome(
            result=value,
            manifest_path=manifest_path.relative_to(settings.history_dir).as_posix(),
            archive_saved=True,
        )

    monkeypatch.setattr(queue_module, "build_and_publish_artifacts", fake_artifacts)
    application = main.create_app()
    with TestClient(
        application,
        base_url="http://127.0.0.1:8000",
        raise_server_exceptions=False,
    ) as test_client:
        yield test_client, tmp_path
    get_settings.cache_clear()


@pytest.mark.parametrize(
    ("route", "field"),
    [
        ("text", "full_text"),
        ("layout", "markdown"),
        ("table", "table_count"),
        ("vl", "latex_formulas"),
    ],
)
def test_all_synchronous_routes_share_queue_and_cleanup(client, route, field):
    test_client, tmp_path = client
    response = test_client.post(
        f"/api/v1/ocr/{route}",
        files={"file": ("scan.png", png_bytes(), "image/png")},
    )
    assert response.status_code == 200
    assert field in response.json()
    assert list((tmp_path / "uploads").glob("*")) == []


def test_async_route_returns_202_and_pollable_job(client):
    test_client, tmp_path = client
    response = test_client.post(
        "/api/v1/ocr/text?async_mode=true",
        files={"file": ("scan.png", png_bytes(), "image/png")},
    )
    assert response.status_code == 202
    job_id = response.json()["job_id"]
    for _ in range(50):
        job = test_client.get(f"/api/v1/jobs/{job_id}").json()
        if job["status"] == "completed":
            break
        time.sleep(0.01)
    assert job["result"]["full_text"] == "hello"
    assert job["progress"]["stage"] == "completed"
    assert job["progress"]["message"] == "Processing complete"
    assert "updated_at" in job["progress"]
    assert "input_path" not in job
    assert "created_at" in job and "updated_at" in job
    assert list((tmp_path / "uploads").glob("*")) == []


def test_maintenance_check_is_safe_when_no_release_channel_is_configured(client, monkeypatch):
    def unavailable_channel(_self):
        raise MaintenanceError("The signed maintenance channel is not configured yet")

    monkeypatch.setattr(MaintenanceService, "_verify_manifest", unavailable_channel)
    test_client, _ = client
    response = test_client.post("/api/v1/maintenance/check")
    assert response.status_code == 200
    payload = response.json()
    assert payload["channel"]["status"] == "unavailable"
    assert payload["ready_to_update"] == []
    assert any(item["id"] == "predixalearn" for item in payload["inventory"])


def test_optional_remove_text_form_field_reaches_document_pipeline(client):
    test_client, _ = client
    response = test_client.post(
        "/api/v1/ocr/text",
        files={"file": ("scan.png", png_bytes(), "image/png")},
        data={"remove_text": "  Scanned   with  "},
    )

    assert response.status_code == 200
    assert response.json()["removed_text"] == "Scanned with"


def test_remove_text_form_field_is_bounded(client):
    test_client, _ = client
    response = test_client.post(
        "/api/v1/ocr/text",
        files={"file": ("scan.png", png_bytes(), "image/png")},
        data={"remove_text": "x" * 201},
    )

    assert response.status_code == 422


def test_repeated_remove_terms_language_and_profile_reach_pipeline(client):
    test_client, _ = client
    response = test_client.post(
        "/api/v1/ocr/text",
        files={"file": ("scan.png", png_bytes(), "image/png")},
        data={
            "remove_terms": ["CamScanner", "Scanned with"],
            "language": "en",
            "document_profile": "exam",
        },
    )

    assert response.status_code == 200
    assert response.json()["removed_terms"] == [
        {"term": "CamScanner", "removed_count": 1},
        {"term": "Scanned with", "removed_count": 1},
    ]


def test_capabilities_and_invalid_language_profile(client):
    test_client, _ = client
    capabilities = test_client.get("/api/v1/capabilities")
    assert capabilities.status_code == 200
    payload = capabilities.json()
    assert payload["runtime_profile"]["id"] == "full"
    assert payload["runtime_profile"]["features"]["document_ocr"] is True
    assert payload["defaults"] == {
        "language": "en",
        "document_profile": "auto",
        "primary_mode": "document",
        "document_strategy": "complete",
    }
    assert payload["languages"] == [{"code": "en", "label": "English"}]
    assert payload["document_profiles"] == ["auto", "general", "exam"]
    assert [item["id"] for item in payload["primary_modes"]] == [
        "document",
        "vision_language",
    ]
    assert [item["workflow"] for item in payload["primary_modes"]] == ["text", "vl"]
    assert [item["id"] for item in payload["document_strategies"]] == [
        "complete",
        "table_focused",
        "layout_diagnostics",
    ]
    assert [item["workflow"] for item in payload["document_strategies"]] == [
        "text",
        "table",
        "layout",
    ]
    assert payload["limits"] == {
        "max_upload_bytes": 1024 * 1024,
        "max_pdf_pages": get_settings().max_pdf_pages,
        "max_image_pixels": get_settings().max_image_pixels,
        "max_queued_jobs": get_settings().max_queued_jobs,
        "file_extensions": [".pdf", ".png", ".jpg", ".jpeg", ".bmp", ".tif", ".tiff", ".webp"],
        "mime_types": [
            "application/pdf",
            "image/png",
            "image/jpeg",
            "image/bmp",
            "image/tiff",
            "image/webp",
        ],
    }

    invalid_language = test_client.post(
        "/api/v1/ocr/text",
        files={"file": ("scan.png", png_bytes(), "image/png")},
        data={"language": "fr"},
    )
    assert invalid_language.status_code == 422
    invalid_profile = test_client.post(
        "/api/v1/ocr/text",
        files={"file": ("scan.png", png_bytes(), "image/png")},
        data={"document_profile": "creative"},
    )
    assert invalid_profile.status_code == 422


def test_history_api_persists_views_downloads_and_deletes(client):
    test_client, tmp_path = client
    response = test_client.post(
        "/api/v1/ocr/text",
        files={"file": ("unsafe name (1).png", png_bytes(), "image/png")},
    )
    assert response.status_code == 200

    listing = test_client.get("/api/v1/history?workflow=text_recognition&status=completed").json()
    assert listing["total"] == 1
    summary = listing["items"][0]
    assert summary["input_name"] == "unsafe name (1).png"
    assert summary["input_kind"] == "image"
    assert summary["item_count"] == 1
    assert summary["result_available"] is True

    job_id = summary["job_id"]
    detail = test_client.get(f"/api/v1/history/{job_id}").json()
    assert detail["primary_output"] == "hello"
    assert detail["result"]["full_text"] == "hello"
    assert detail["table_downloads"] == [
        {"table_index": 1, "csv": True, "xlsx": True}
    ]

    primary = test_client.get(f"/api/v1/history/{job_id}/download?format=primary")
    assert primary.status_code == 200
    assert primary.text == "hello"
    assert 'filename="unsafe_name_1.txt"' in primary.headers["content-disposition"]
    complete = test_client.get(f"/api/v1/history/{job_id}/download?format=json")
    assert complete.json()["full_text"] == "hello"
    markdown = test_client.get(f"/api/v1/history/{job_id}/download?format=markdown")
    assert markdown.text.replace("\r\n", "\n") == "# Generated\n"
    docx = test_client.get(f"/api/v1/history/{job_id}/download?format=docx")
    assert docx.content == b"fake docx"
    csv_download = test_client.get(
        f"/api/v1/history/{job_id}/download?format=csv&table=1"
    )
    assert csv_download.text.replace("\r\n", "\n") == "Year,1950\nPopulation,25\n"
    assert "unsafe_name_1-table-001.csv" in csv_download.headers["content-disposition"]
    xlsx_download = test_client.get(
        f"/api/v1/history/{job_id}/download?format=xlsx&table=1"
    )
    assert xlsx_download.content == b"fake xlsx"
    assert test_client.get(
        f"/api/v1/history/{job_id}/download?format=csv&table=2"
    ).status_code == 409

    deleted = test_client.delete(f"/api/v1/history/{job_id}")
    assert deleted.json() == {"deleted": True, "job_id": job_id}
    assert test_client.get(f"/api/v1/history/{job_id}").status_code == 404
    assert not (tmp_path / "history" / "jobs" / job_id).exists()


def test_history_rejects_invalid_id_and_hostile_delete_origin(client):
    test_client, _ = client
    assert test_client.get("/api/v1/history/not-a-job").status_code == 404
    response = test_client.delete(
        "/api/v1/history",
        headers={"Origin": "https://attacker.example"},
    )
    assert response.status_code == 403


def test_history_pinning_bulk_export_and_import_round_trip(client):
    test_client, _ = client
    completed = test_client.post(
        "/api/v1/ocr/layout",
        files={"file": ("report.png", png_bytes(), "image/png")},
    )
    assert completed.status_code == 200
    listing = test_client.get("/api/v1/history").json()
    job_id = listing["items"][0]["job_id"]

    pinned = test_client.patch(f"/api/v1/history/{job_id}", json={"pinned": True})
    assert pinned.status_code == 200
    assert pinned.json()["pinned"] is True
    assert test_client.get("/api/v1/history?pinned=true").json()["total"] == 1

    exported = test_client.post("/api/v1/history/export", json={"job_ids": [job_id]})
    assert exported.status_code == 200
    with zipfile.ZipFile(io.BytesIO(exported.content)) as archive:
        assert "manifest.json" in archive.namelist()
        assert any(name.endswith("result.json") for name in archive.namelist())

    assert test_client.post("/api/v1/history/bulk-delete", json={"job_ids": [job_id]}).json() == {
        "deleted_count": 1
    }
    imported = test_client.post(
        "/api/v1/history/import",
        files={"file": ("history.zip", exported.content, "application/zip")},
    )
    assert imported.status_code == 200
    assert imported.json() == {"imported_count": 1, "reassigned_count": 0}
    restored = test_client.get("/api/v1/history").json()["items"][0]
    assert restored["input_name"] == "report.png"
    assert restored["pinned"] is True


def test_upload_content_is_verified_and_limited(client):
    test_client, _ = client
    unsupported = test_client.post(
        "/api/v1/ocr/text",
        files={"file": ("fake.png", b"plain text", "image/png")},
    )
    assert unsupported.status_code == 415

    malformed = test_client.post(
        "/api/v1/ocr/text",
        files={"file": ("broken.png", b"\x89PNG\r\n\x1a\ninvalid", "image/png")},
    )
    assert malformed.status_code == 422

    oversized = test_client.post(
        "/api/v1/ocr/text",
        files={"file": ("large.png", b"\x89PNG\r\n\x1a\n" + b"x" * (1024 * 1024), "image/png")},
    )
    assert oversized.status_code == 413


def test_hostile_browser_origin_is_rejected(client):
    test_client, _ = client
    response = test_client.post(
        "/api/v1/ocr/text",
        headers={"Origin": "https://attacker.example"},
        files={"file": ("scan.png", png_bytes(), "image/png")},
    )
    assert response.status_code == 403


def test_desktop_quit_requires_the_local_origin_control_token_and_idle_queue(monkeypatch, tmp_path):
    for name in ("OCR_OUTPUT_DIR", "OCR_UPLOAD_DIR", "OCR_HISTORY_DIR", "OCR_LOG_DIR"):
        monkeypatch.setenv(name, str(tmp_path / name.lower()))
    certificate = tmp_path / "server.pem"
    key = tmp_path / "server-key.pem"
    certificate.write_text("certificate", encoding="utf-8")
    key.write_text("key", encoding="utf-8")
    token = "c" * 32
    monkeypatch.setenv("OCR_DESKTOP_MODE", "1")
    monkeypatch.setenv("OCR_HOST", "127.0.0.1")
    monkeypatch.setenv("OCR_PORT", "443")
    monkeypatch.setenv("OCR_DESKTOP_CONTROL_TOKEN", token)
    monkeypatch.setenv("OCR_DESKTOP_TLS_CERT_PATH", str(certificate))
    monkeypatch.setenv("OCR_DESKTOP_TLS_KEY_PATH", str(key))
    get_settings.cache_clear()
    monkeypatch.setattr(main, "get_manager", lambda: FakeManager())
    monkeypatch.setattr(main, "reset_manager", lambda: None)
    monkeypatch.setattr(main, "start_engine_warmup", lambda *_: None)
    monkeypatch.setattr(main, "start_libreoffice_probe", lambda *_: None)

    class Queue:
        has_active_jobs = False

    queue = Queue()
    monkeypatch.setattr(runtime_api, "get_queue", lambda: queue)
    shut_down: list[bool] = []
    application = main.create_app()
    application.state.request_desktop_shutdown = lambda: shut_down.append(True)
    with TestClient(application, base_url="https://app.predixalearn.com") as test_client:
        page = test_client.get("/")
        assert 'id="quit-app-button"' in page.text
        assert f'content="{token}"' in page.text
        assert test_client.post("/api/v1/app/quit").status_code == 403
        assert test_client.post(
            "/api/v1/app/quit",
            headers={"X-PredixaLearn-Control": token, "Origin": "https://attacker.example"},
        ).status_code == 403
        accepted = test_client.post(
            "/api/v1/app/quit",
            headers={"X-PredixaLearn-Control": token, "Origin": "https://app.predixalearn.com"},
        )
        assert accepted.status_code == 202
        assert shut_down == [True]
        application.state.request_desktop_shutdown = lambda: False
        race_blocked = test_client.post(
            "/api/v1/app/quit",
            headers={"X-PredixaLearn-Control": token, "Origin": "https://app.predixalearn.com"},
        )
        assert race_blocked.status_code == 409
        queue.has_active_jobs = True
        blocked = test_client.post(
            "/api/v1/app/quit",
            headers={"X-PredixaLearn-Control": token, "Origin": "https://app.predixalearn.com"},
        )
        assert blocked.status_code == 409
    get_settings.cache_clear()


def test_security_headers_and_static_ui(client):
    test_client, _ = client
    response = test_client.get("/")
    assert response.status_code == 200
    assert "PredixaLearn" in response.text
    assert re.search(r'href="/static/app\.css\?v=[a-f0-9]{12}"', response.text)
    assert 'rel="icon" type="image/png"' in response.text
    assert 'predixalearn-mark-32.png' in response.text
    assert 'predixalearn-mark-180.png' in response.text
    assert response.headers["x-frame-options"] == "DENY"
    assert response.headers["x-content-type-options"] == "nosniff"
    assert "default-src 'self'" in response.headers["content-security-policy"]
    assert "access-control-allow-origin" not in response.headers
    assert 'data-step="prepare"' in response.text
    assert 'data-step="recognize"' in response.text
    assert 'data-step="export"' in response.text
    assert 'id="page-progress-bar"' in response.text
    assert 'id="remove-text"' in response.text
    assert response.text.count('role="tab"') >= 6
    assert 'data-mode="document"' in response.text
    assert 'data-mode="vision_language"' in response.text
    assert 'id="document-strategy"' in response.text
    assert 'id="language-search"' in response.text
    assert 'id="cancel-job"' in response.text
    assert 'value="table_focused"' in response.text
    assert 'value="layout_diagnostics"' in response.text
    assert 'data-workflow=' not in response.text
    assert 'href="/tips"' in response.text
    assert 'href="/history"' in response.text
    css = test_client.get("/static/app.css")
    assert css.status_code == 200
    assert "#d7ecf7" in css.text
    mark = test_client.get("/static/predixalearn-mark.png")
    assert mark.status_code == 200
    assert mark.headers["content-type"] == "image/png"
    assert len(mark.content) > 1000
    script = test_client.get("/static/app.js")
    assert script.status_code == 200
    assert "job.progress" in script.text
    assert 'formData.append("remove_terms"' in script.text
    assert 'formData.append("language"' in script.text
    assert 'formData.append("document_profile"' in script.text
    assert "primaryMode" in script.text
    assert "documentStrategy" in script.text
    assert "ArrowRight" in script.text
    assert "Math.random" not in script.text
    assert "setInterval" not in script.text

    tips = test_client.get("/tips")
    assert tips.status_code == 200
    assert 'id="service-health"' in tips.text
    history = test_client.get("/history")
    assert history.status_code == 200
    assert 'id="history-list"' in history.text
    assert test_client.get("/static/history.js").status_code == 200
    assert test_client.get("/static/tips.js").status_code == 200
    assert test_client.get("/static/predixalearn-mark-32.png").status_code == 200
    assert test_client.get("/static/vendor/swagger-ui.css").status_code == 200
    docs = test_client.get("/docs")
    assert docs.status_code == 200
    assert "cdn.jsdelivr" not in docs.text
    assert docs.headers["cache-control"] == "no-store"
    assert test_client.get("/static/app.css?v=versioned").headers["cache-control"] == (
        "public, max-age=31536000, immutable"
    )


def test_institution_sync_is_unenrolled_and_never_automatic_by_default(client):
    test_client, _ = client
    status = test_client.get("/api/v1/institution/status")
    assert status.status_code == 200
    assert status.json()["enrolled"] is False
    assert status.json()["automatic_history_upload"] is False
    blocked = test_client.post(
        "/api/v1/institution/outbox", json={"job_ids": ["a" * 32]}
    )
    assert blocked.status_code == 409
    assert "Enroll" in blocked.json()["detail"]


def test_diagnostics_bundle_contains_only_redacted_support_data(client):
    test_client, _ = client
    response = test_client.post("/api/v1/diagnostics")
    assert response.status_code == 200
    assert response.headers["content-type"] == "application/zip"
    with zipfile.ZipFile(io.BytesIO(response.content)) as archive:
        assert sorted(archive.namelist()) == ["diagnostics.json", "logs/redacted-excerpt.txt"]
        payload = json.loads(archive.read("diagnostics.json"))
    assert payload["schema_version"] == 1
    assert "output_dir" not in json.dumps(payload["configuration"]).lower()


def test_history_uses_public_workflow_labels_without_changing_filter_values(client):
    test_client, _ = client
    history = test_client.get("/history")
    assert history.status_code == 200
    assert 'value="text_recognition">Document OCR</option>' in history.text
    assert 'value="table_extraction">Table-focused</option>' in history.text
    assert 'value="layout_parsing">Layout diagnostics</option>' in history.text
    script = test_client.get("/static/history.js")
    assert 'text_recognition: "Document OCR"' in script.text
    assert 'table_extraction: "Table-focused"' in script.text


def test_past_paper_analysis_routes_require_explicit_consent_and_preserve_local_mode(client):
    test_client, _ = client
    response = test_client.post(
        "/api/v1/ocr/text",
        files={"file": ("exam.png", png_bytes(), "image/png")},
        data={"document_profile": "exam"},
    )
    assert response.status_code == 200
    job_id = test_client.get("/api/v1/history?status=completed").json()["items"][0]["job_id"]

    local = test_client.post(f"/api/v1/history/{job_id}/analysis", json={"consent_to_cloud": False})
    assert local.status_code == 200
    assert local.json()["model"]["consent_to_cloud"] is False
    assert local.json()["ocr_result_sha256"]

    loaded = test_client.get(f"/api/v1/history/{job_id}/analysis")
    assert loaded.status_code == 200
    assert loaded.json()["ocr_result_sha256"] == local.json()["ocr_result_sha256"]
    assert test_client.get(f"/api/v1/history/{job_id}/analysis/practice").json() == {
        "document_id": job_id,
        "model": None,
        "consent_to_cloud": False,
        "items": [],
        "review_required": True,
    }
    practice = test_client.post(f"/api/v1/history/{job_id}/analysis/practice")
    assert practice.status_code == 200
    assert practice.json()["items"] == []
    assert test_client.get(f"/api/v1/history/{job_id}/source-pages/1").status_code == 503

    markdown = test_client.get(f"/api/v1/history/{job_id}/analysis/download?format=markdown")
    assert markdown.status_code == 200
    assert "Teacher-reviewable" in markdown.text
    assert test_client.post(
        f"/api/v1/history/{job_id}/analysis", json={"consent_to_cloud": True}
    ).status_code == 503


def test_teaching_workspace_routes_preserve_existing_ocr_contracts(client):
    test_client, _ = client
    assert test_client.get("/review").status_code == 200
    assert test_client.get("/teacher").status_code == 200
    assert test_client.get("/revision").status_code == 200

    response = test_client.post(
        "/api/v1/ocr/text",
        files={"file": ("exam.png", png_bytes(), "image/png")},
        data={"document_profile": "exam", "language": "en"},
    )
    assert response.status_code == 200
    job_id = test_client.get("/api/v1/history?status=completed").json()["items"][0]["job_id"]
    workspace = test_client.get(f"/api/v1/history/{job_id}/review-workspace")
    assert workspace.status_code == 200
    assert workspace.json()["source_hash"]
    assert workspace.json()["language_reliability"]["analysis"] == "supported_with_consent"

    correction = test_client.post(
        f"/api/v1/history/{job_id}/corrections",
        json={
            "target_kind": "reconstructed_text",
            "target_id": "document",
            "original": {"text": "hello"},
            "replacement": {"text": "Hello."},
            "reason": "Teacher verified punctuation",
            "status": "approved",
        },
    )
    assert correction.status_code == 201
    assert correction.json()["status"] == "approved"
    assert test_client.get(f"/api/v1/history/{job_id}/corrections/audit").json()["events"]
    capabilities = test_client.get("/api/v1/capabilities").json()
    assert any(item["language"] == "en" for item in capabilities["language_reliability"])


def test_unexpected_workflow_failure_returns_safe_error(client, monkeypatch):
    test_client, _ = client

    def fail(path, **kwargs):
        raise RuntimeError(r"D:\private\secret.pdf traceback details")

    monkeypatch.setattr(api, "run_text_recognition", fail)
    response = test_client.post(
        "/api/v1/ocr/text",
        files={"file": ("scan.png", png_bytes(), "image/png")},
    )
    assert response.status_code == 500
    assert response.json() == {"detail": "OCR processing failed unexpectedly"}
    assert "private" not in response.text.lower()


def test_health_reports_cudnn_mismatch(client):
    test_client, _ = client
    response = test_client.get("/api/v1/health")
    assert response.status_code == 200
    health = response.json()
    assert health["status"] in {"ready", "degraded"}
    assert isinstance(health["ready"], bool)
    assert "cudnn_compiled" in health["gpu"]
    assert "cudnn_runtime" in health["gpu"]
    assert health["document_export"]["wordfreq"] == "3.1.1"
    assert health["document_export"]["libreoffice"]["renderer"] == "libreoffice"
    assert health["document_export"]["libreoffice"]["mode"] == "project-local-headless"
    assert health["document_export"]["libreoffice"]["validation_required"] is True

    compatibility_response = test_client.get("/health")
    assert compatibility_response.status_code == 200
    assert compatibility_response.json()["status"] == health["status"]
