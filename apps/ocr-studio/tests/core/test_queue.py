from __future__ import annotations

import asyncio
import threading
from dataclasses import replace
from datetime import timedelta

import pytest

from app.core.config import get_settings
from app.core.errors import QueueFullError
from app.core.queue import JobQueue, JobStatus, _utcnow
from app.documents.artifacts import ArtifactOutcome


@pytest.fixture(autouse=True)
def passthrough_artifacts(monkeypatch):
    monkeypatch.setattr(
        "app.core.queue.build_and_publish_artifacts",
        lambda path, name, workflow, result, **kwargs: ArtifactOutcome(
            result=dict(result), manifest_path=None, archive_saved=False
        ),
    )


def queue_settings(tmp_path, **changes):
    return replace(
        get_settings(),
        output_dir=tmp_path / "output",
        history_dir=tmp_path / "history",
        history_db_path=tmp_path / "history" / "history.sqlite3",
        log_dir=tmp_path / "logs",
        **changes,
    )


def test_queue_allows_one_active_job_and_rejects_when_full(tmp_path):
    async def scenario():
        release = threading.Event()
        settings = queue_settings(tmp_path, max_queued_jobs=0)
        queue = JobQueue(settings)

        def slow_workflow(path, **kwargs):
            del path, kwargs
            release.wait(timeout=2)
            return {"ok": True}

        try:
            first = await queue.submit(
                slow_workflow,
                tmp_path / "one.png",
                workflow_name="text_recognition",
            )
            with pytest.raises(QueueFullError):
                await queue.submit(
                    slow_workflow,
                    tmp_path / "two.png",
                    workflow_name="text_recognition",
                )
            release.set()
            result = await queue.wait_for_result(first)
            assert result.status == JobStatus.COMPLETED
        finally:
            release.set()
            queue.shutdown()

    asyncio.run(scenario())


def test_completed_job_expires_after_ttl(tmp_path):
    async def scenario():
        settings = queue_settings(tmp_path, job_ttl_seconds=1)
        queue = JobQueue(settings)
        try:
            job_id = await queue.submit(
                lambda path, **kwargs: {"ok": True},
                tmp_path / "input.png",
                workflow_name="text_recognition",
            )
            await queue.wait_for_result(job_id)
            queue._jobs[job_id].result.updated_at = _utcnow() - timedelta(seconds=2)
            assert queue.get_result(job_id) is None
        finally:
            queue.shutdown()

    asyncio.run(scenario())


def test_job_record_has_one_id_and_no_internal_path(tmp_path):
    async def scenario():
        settings = queue_settings(tmp_path)
        queue = JobQueue(settings)
        try:
            job_id = await queue.submit(
                lambda path, **kwargs: {
                    "job_id_seen": kwargs["job_id"],
                    "output_name": kwargs["output_dir"].name,
                },
                tmp_path / "private" / "input.png",
                input_name="input.png",
                workflow_name="layout_parsing",
            )
            result = await queue.wait_for_result(job_id)
            assert result.result == {"job_id_seen": job_id, "output_name": job_id}
            assert result.input_name == "input.png"
            assert not hasattr(result, "input_path")
        finally:
            queue.shutdown()

    asyncio.run(scenario())


def test_queue_forwards_optional_text_removal_to_artifact_pipeline(tmp_path, monkeypatch):
    captured = {}

    def capture_artifacts(path, name, workflow, result, **kwargs):
        del path, name, workflow
        captured["remove_text"] = kwargs.get("remove_text")
        return ArtifactOutcome(result=dict(result), manifest_path=None, archive_saved=False)

    monkeypatch.setattr("app.core.queue.build_and_publish_artifacts", capture_artifacts)

    async def scenario():
        queue = JobQueue(queue_settings(tmp_path))
        try:
            job_id = await queue.submit(
                lambda path, **kwargs: {"ok": True},
                tmp_path / "input.png",
                workflow_name="text_recognition",
                remove_text="CamScanner",
            )
            await queue.wait_for_result(job_id)
            assert captured["remove_text"] == "CamScanner"
        finally:
            queue.shutdown()

    asyncio.run(scenario())


def test_queue_exposes_real_progress_snapshot(tmp_path):
    async def scenario():
        reported = threading.Event()
        release = threading.Event()
        settings = queue_settings(tmp_path)
        queue = JobQueue(settings)

        def workflow(path, *, progress_callback, **kwargs):
            del path, kwargs
            progress_callback(
                stage="text_recognition",
                message="Extracting text from page 2 of 4",
                current_page=2,
                total_pages=4,
                completed_pages=1,
            )
            reported.set()
            release.wait(timeout=2)
            return {"ok": True}

        try:
            job_id = await queue.submit(
                workflow,
                tmp_path / "input.pdf",
                workflow_name="text_recognition",
            )
            assert await asyncio.to_thread(reported.wait, 1)
            snapshot = queue.get_result(job_id)
            assert snapshot is not None
            assert snapshot.progress.stage == "text_recognition"
            assert snapshot.progress.current_page == 2
            assert snapshot.progress.total_pages == 4
            assert snapshot.progress.completed_pages == 1

            snapshot.progress.message = "changed outside queue"
            assert queue.get_result(job_id).progress.message == "Extracting text from page 2 of 4"

            release.set()
            result = await queue.wait_for_result(job_id)
            assert result.progress.stage == "completed"
            assert result.progress.completed_pages == 4
        finally:
            release.set()
            queue.shutdown()

    asyncio.run(scenario())


def test_history_failure_does_not_erase_successful_result(tmp_path):
    class FailingHistoryStore:
        def initialize(self):
            return None

        def upsert(self, **kwargs):
            del kwargs
            raise OSError("database unavailable")

        def save_result(self, job_id, result):
            del job_id, result
            raise OSError("output unavailable")

    async def scenario():
        settings = queue_settings(tmp_path)
        queue = JobQueue(settings, history_store=FailingHistoryStore())
        try:
            job_id = await queue.submit(
                lambda path, **kwargs: {"text": "OCR result retained"},
                tmp_path / "input.png",
                workflow_name="text_recognition",
            )
            result = await queue.wait_for_result(job_id)
            assert result.status == JobStatus.COMPLETED
            assert result.result == {"text": "OCR result retained"}
            assert result.history_saved is False
            assert result.history_warning == (
                "OCR succeeded, but its result could not be added to history"
            )
        finally:
            queue.shutdown()

    asyncio.run(scenario())


def test_cancellation_is_idempotent_and_stops_at_progress_checkpoint(tmp_path):
    async def scenario():
        entered = threading.Event()
        queue = JobQueue(queue_settings(tmp_path))

        def cancellable_workflow(path, *, progress_callback, **kwargs):
            del path, kwargs
            entered.set()
            for page in range(1, 20):
                progress_callback(
                    stage="text_recognition",
                    message=f"Page {page}",
                    current_page=page,
                    total_pages=20,
                    completed_pages=page - 1,
                )
                threading.Event().wait(0.01)
            return {"ok": True}

        try:
            job_id = await queue.submit(
                cancellable_workflow,
                tmp_path / "cancel.pdf",
                workflow_name="text_recognition",
            )
            assert await asyncio.to_thread(entered.wait, 1)
            first = queue.cancel(job_id)
            second = queue.cancel(job_id)
            assert first is not None and second is not None
            result = await queue.wait_for_result(job_id)
            assert result.status == JobStatus.CANCELLED
            assert result.cancel_requested is True
            assert result.progress.stage == "cancelled"
        finally:
            queue.shutdown()

    asyncio.run(scenario())
