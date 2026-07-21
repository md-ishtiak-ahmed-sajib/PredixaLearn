from __future__ import annotations

import json
import sqlite3
import zipfile
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from app.core.config import get_settings
from app.storage.history import (
    HistoryConflictError,
    HistoryNotFoundError,
    HistoryResultError,
    HistoryStore,
)
from app.storage.history_archive import import_history_archive


def history_settings(tmp_path):
    return replace(
        get_settings(),
        output_dir=tmp_path / "output",
        upload_dir=tmp_path / "uploads",
        history_dir=tmp_path / "history",
        history_db_path=tmp_path / "history" / "history.sqlite3",
        log_dir=tmp_path / "logs",
    )


def record(store, *, job_id, status="completed", name="scan.pdf", result_path=None):
    created = datetime(2026, 7, 16, 8, 0, tzinfo=timezone.utc)
    completed = created + timedelta(seconds=2) if status == "completed" else None
    store.upsert(
        job_id=job_id,
        input_name=name,
        workflow="text_recognition",
        input_kind="pdf" if name.endswith(".pdf") else "image",
        status=status,
        item_count=3,
        error=None,
        result_path=result_path,
        created_at=created,
        started_at=created,
        completed_at=completed,
        updated_at=completed or created,
    )


def test_history_persists_results_filters_and_times(tmp_path):
    settings = history_settings(tmp_path)
    store = HistoryStore(settings)
    job_id = "a" * 32
    result_path = store.save_result(job_id, {"full_text": "saved text", "page_count": 3})
    record(store, job_id=job_id, result_path=result_path)

    reopened = HistoryStore(settings)
    records, total = reopened.list(query="scan", workflow="text_recognition", status="completed")
    assert total == 1
    assert records[0].summary()["duration_ms"] == 2000
    assert records[0].summary()["item_count"] == 3
    assert reopened.read_result(records[0])["full_text"] == "saved text"


def test_history_recovers_interrupted_jobs_and_protects_active_deletion(tmp_path):
    store = HistoryStore(history_settings(tmp_path))
    job_id = "b" * 32
    record(store, job_id=job_id, status="processing")

    with pytest.raises(HistoryConflictError):
        store.delete(job_id)
    assert store.recover_interrupted() == 1
    recovered = store.get(job_id)
    assert recovered.status == "failed"
    assert recovered.error == "Processing was interrupted by a service restart"
    store.delete(job_id)
    with pytest.raises(HistoryNotFoundError):
        store.get(job_id)


def test_clear_history_removes_terminal_outputs_but_keeps_active(tmp_path):
    store = HistoryStore(history_settings(tmp_path))
    completed_id = "c" * 32
    active_id = "d" * 32
    result_path = store.save_result(completed_id, {"full_text": "remove me"})
    record(store, job_id=completed_id, result_path=result_path)
    record(store, job_id=active_id, status="pending", name="active.png")

    assert store.clear_terminal() == 1
    assert not (store.settings.history_dir / "jobs" / completed_id).exists()
    assert store.get(active_id).status == "pending"


def test_history_migrates_old_database_and_legacy_artifacts(tmp_path):
    settings = history_settings(tmp_path)
    settings.output_dir.mkdir(parents=True)
    old_db = settings.output_dir / "history.sqlite3"
    job_id = "e" * 32
    with sqlite3.connect(old_db) as connection:
        connection.execute(
            """
            CREATE TABLE history_jobs (
                job_id TEXT PRIMARY KEY, input_name TEXT NOT NULL, workflow TEXT NOT NULL,
                input_kind TEXT NOT NULL, status TEXT NOT NULL, item_count INTEGER,
                error TEXT, result_path TEXT, created_at TEXT NOT NULL, started_at TEXT,
                completed_at TEXT, updated_at TEXT NOT NULL
            )
            """
        )
        connection.execute(
            "INSERT INTO history_jobs VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                job_id,
                "legacy.pdf",
                "vl_processing",
                "pdf",
                "completed",
                7,
                None,
                f"{job_id}/result.json",
                "2026-07-16T08:00:00+00:00",
                "2026-07-16T08:00:01+00:00",
                "2026-07-16T08:00:05+00:00",
                "2026-07-16T08:00:05+00:00",
            ),
        )
    connection.close()
    old_job = settings.output_dir / job_id
    old_job.mkdir()
    (old_job / "result.json").write_text(
        json.dumps({"markdown": "legacy"}), encoding="utf-8"
    )
    (old_job / "document_vl.md").write_text("legacy", encoding="utf-8")
    unmatched = settings.output_dir / ("f" * 32)
    unmatched.mkdir()
    (unmatched / "orphan.txt").write_text("orphan", encoding="utf-8")

    store = HistoryStore(settings)
    store.initialize()
    migrated = store.get(job_id)

    assert migrated.legacy == 1
    assert store.read_result(migrated)["markdown"] == "legacy"
    assert migrated.artifact_manifest_path is not None
    assert old_db.exists() and old_job.exists()
    assert (settings.history_dir / "legacy" / unmatched.name / "orphan.txt").is_file()
    assert (settings.output_dir / unmatched.name / "orphan.txt").is_file()
    assert (settings.history_dir / "migration-backups" / "history.sqlite3").is_file()


def test_history_schema_migrations_are_numbered_and_idempotent(tmp_path):
    settings = history_settings(tmp_path)
    settings.history_dir.mkdir(parents=True)
    with sqlite3.connect(settings.history_db_path) as connection:
        connection.execute(
            """
            CREATE TABLE history_jobs (
                job_id TEXT PRIMARY KEY, input_name TEXT NOT NULL, workflow TEXT NOT NULL,
                input_kind TEXT NOT NULL, status TEXT NOT NULL, item_count INTEGER,
                error TEXT, result_path TEXT, created_at TEXT NOT NULL, started_at TEXT,
                completed_at TEXT, updated_at TEXT NOT NULL
            )
            """
        )

    store = HistoryStore(settings)
    store.initialize()
    with sqlite3.connect(settings.history_db_path) as connection:
        version = connection.execute("PRAGMA user_version").fetchone()[0]
        columns = {row[1] for row in connection.execute("PRAGMA table_info(history_jobs)")}
    assert version == 7
    assert {"artifact_manifest_path", "pinned", "quality_score", "warning_count", "analysis_status"} <= columns

    HistoryStore(settings).initialize()


@pytest.mark.parametrize("legacy_version", [1, 2])
def test_history_migrates_each_numbered_schema_version(tmp_path, legacy_version):
    settings = history_settings(tmp_path)
    settings.history_dir.mkdir(parents=True)
    with sqlite3.connect(settings.history_db_path) as connection:
        connection.execute(
            """
            CREATE TABLE history_jobs (
                job_id TEXT PRIMARY KEY, input_name TEXT NOT NULL, workflow TEXT NOT NULL,
                input_kind TEXT NOT NULL, status TEXT NOT NULL, item_count INTEGER,
                error TEXT, result_path TEXT, created_at TEXT NOT NULL, started_at TEXT,
                completed_at TEXT, updated_at TEXT NOT NULL
            )
            """
        )
        if legacy_version == 2:
            connection.execute("ALTER TABLE history_jobs ADD COLUMN artifact_manifest_path TEXT")
            connection.execute("ALTER TABLE history_jobs ADD COLUMN legacy INTEGER NOT NULL DEFAULT 0")
        connection.execute(f"PRAGMA user_version = {legacy_version}")

    HistoryStore(settings).initialize()
    with sqlite3.connect(settings.history_db_path) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 7
        columns = {row[1] for row in connection.execute("PRAGMA table_info(history_jobs)")}
    assert {"artifact_manifest_path", "legacy", "pinned", "imported_from_job_id"} <= columns


def test_institution_outbox_requires_explicit_completed_history_selection(tmp_path):
    store = HistoryStore(history_settings(tmp_path))
    job_id = "9" * 32
    result_path = store.save_result(job_id, {"full_text": "selected evidence"})
    record(store, job_id=job_id, result_path=result_path)
    payload = {
        "tenant_id": "tenant-a",
        "device_id": "device-a",
        "events": [{"event_type": "archive.upsert", "payload": {"text": "selected evidence"}}],
    }

    queued = store.queue_institution_sync(
        job_id,
        tenant_id="tenant-a",
        device_id="device-a",
        payload=payload,
    )
    assert queued.status == "pending"
    assert store.read_sync_payload(queued) == payload
    assert store.queue_institution_sync(
        job_id,
        tenant_id="tenant-a",
        device_id="device-a",
        payload=payload,
    ).event_id == queued.event_id
    assert store.list_sync_outbox()[0].public()["job_id"] == job_id

    active_id = "8" * 32
    record(store, job_id=active_id, status="processing")
    with pytest.raises(HistoryConflictError, match="completed"):
        store.queue_institution_sync(
            active_id,
            tenant_id="tenant-a",
            device_id="device-a",
            payload=payload,
        )


def test_history_rejects_a_database_from_a_newer_application(tmp_path):
    settings = history_settings(tmp_path)
    settings.history_dir.mkdir(parents=True)
    with sqlite3.connect(settings.history_db_path) as connection:
        connection.execute("PRAGMA user_version = 99")

    with pytest.raises(HistoryResultError, match="newer version"):
        HistoryStore(settings).initialize()


def test_history_import_snapshot_and_archived_artifacts(tmp_path):
    store = HistoryStore(history_settings(tmp_path))
    imported_id = "3" * 32
    assert store.import_job_snapshot(
        {
            "job_id": imported_id,
            "workflow": "text_recognition",
            "status": "completed",
            "input_name": "imported.pdf",
            "created_at": "2030-07-16T08:00:00+00:00",
            "updated_at": "2030-07-16T08:00:04+00:00",
            "progress": {"total_pages": 4},
            "result": {"full_text": "restored"},
        }
    )
    imported = store.get(imported_id)
    assert imported.item_count == 4
    assert imported.legacy == 1
    assert store.read_result(imported)["full_text"] == "restored"
    assert not store.import_job_snapshot(
        {
            "job_id": "4" * 32,
            "workflow": "unknown",
            "status": "completed",
        }
    )

    artifact_id = "5" * 32
    archive = store.settings.history_dir / "jobs" / artifact_id / "artifacts"
    table = archive / "tables" / "table-001.csv"
    table.parent.mkdir(parents=True)
    markdown = archive / "output.md"
    markdown.write_text("# Archive", encoding="utf-8")
    table.write_text("a,b\n1,2\n", encoding="utf-8")
    manifest = store.settings.history_dir / "jobs" / artifact_id / "manifest.json"
    manifest.write_text(
        json.dumps(
            {
                "archive_root": (Path("jobs") / artifact_id / "artifacts").as_posix(),
                "files": {"markdown": "output.md", "tables": ["tables/table-001.csv"]},
            }
        ),
        encoding="utf-8",
    )
    created = datetime(2020, 1, 1, tzinfo=timezone.utc)
    store.upsert(
        job_id=artifact_id,
        input_name="archived.pdf",
        workflow="text_recognition",
        input_kind="pdf",
        status="completed",
        item_count=1,
        error=None,
        result_path=None,
        artifact_manifest_path=(Path("jobs") / artifact_id / "manifest.json").as_posix(),
        created_at=created,
        started_at=created,
        completed_at=created,
        updated_at=created,
        language="en",
        document_profile="general",
        quality_score=94.0,
        warning_count=1,
    )
    archived = store.get(artifact_id)
    assert store.artifact_path(archived, "markdown") == markdown
    assert store.table_artifact_path(archived, table_index=1, extension="csv") == table
    assert store.reveal_path(archived) == archive
    assert store.set_pinned(artifact_id, True).pinned == 1
    records, total = store.list(
        workflow="text_recognition",
        status="completed",
        pinned=True,
        created_from="2019-01-01",
        created_to="2021-01-01",
        sort="quality",
    )
    assert total == 1 and records[0].job_id == artifact_id
    stale_id = "6" * 32
    store.upsert(
        job_id=stale_id,
        input_name="stale.pdf",
        workflow="text_recognition",
        input_kind="pdf",
        status="completed",
        item_count=1,
        error=None,
        result_path=None,
        created_at=created,
        started_at=created,
        completed_at=created,
        updated_at=created,
    )
    assert store.clear_terminal(older_than_days=1) == 1
    with pytest.raises(HistoryNotFoundError):
        store.get(stale_id)


def test_history_storage_boundaries_and_bulk_deletion(tmp_path):
    store = HistoryStore(history_settings(tmp_path))
    empty_id = "7" * 32
    record(store, job_id=empty_id)
    empty = store.get(empty_id)
    with pytest.raises(HistoryResultError, match="no saved result"):
        store.read_result(empty)
    with pytest.raises(HistoryResultError, match="no generated document"):
        store.artifact_path(empty, "markdown")
    with pytest.raises(HistoryResultError, match="table artifact is invalid"):
        store.table_artifact_path(empty, table_index=0, extension="csv")
    with pytest.raises(HistoryResultError, match="invalid"):
        store._resolve_history_path("../outside.json")
    assert store.list(workflow="unsupported") == ([], 0)
    assert store.list(status="unsupported") == ([], 0)

    malformed_id = "8" * 32
    manifest = store.settings.history_dir / "jobs" / malformed_id / "manifest.json"
    manifest.parent.mkdir(parents=True)
    manifest.write_text("[]", encoding="utf-8")
    created = datetime(2026, 1, 1, tzinfo=timezone.utc)
    store.upsert(
        job_id=malformed_id,
        input_name="malformed.pdf",
        workflow="text_recognition",
        input_kind="pdf",
        status="completed",
        item_count=1,
        error=None,
        result_path=None,
        artifact_manifest_path=(Path("jobs") / malformed_id / "manifest.json").as_posix(),
        created_at=created,
        started_at=created,
        completed_at=created,
        updated_at=created,
    )
    with pytest.raises(HistoryResultError, match="malformed"):
        store.read_manifest(store.get(malformed_id))

    removable_id = "9" * 32
    record(store, job_id=removable_id, name="remove.pdf")
    assert store.delete_many([removable_id, removable_id]) == 1
    with pytest.raises(HistoryNotFoundError):
        store.get(removable_id)


def test_history_deletion_does_not_remove_named_output(tmp_path):
    settings = history_settings(tmp_path)
    store = HistoryStore(settings)
    job_id = "1" * 32
    named = settings.output_dir / "scan"
    named.mkdir(parents=True)
    (named / "scan.docx").write_bytes(b"named export")
    result_path = store.save_result(job_id, {"markdown": "saved"})
    record(store, job_id=job_id, result_path=result_path)

    store.delete(job_id)

    assert (named / "scan.docx").read_bytes() == b"named export"


def test_history_import_rejects_path_traversal_before_extracting(tmp_path):
    store = HistoryStore(history_settings(tmp_path))
    store.initialize()
    archive = tmp_path / "unsafe.zip"
    with zipfile.ZipFile(archive, "w") as value:
        value.writestr("../escape.json", "should never be extracted")

    with pytest.raises(HistoryResultError, match="unsafe path"):
        import_history_archive(store, archive, max_bytes=1024 * 1024)

    assert not (tmp_path / "escape.json").exists()
