"""Bounded in-process queue for the single local GPU."""

from __future__ import annotations

import asyncio
import enum
import inspect
import logging
import shutil
import threading
import uuid
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

from app.core.config import Settings, get_settings
from app.core.errors import OCRServiceError, QueueFullError
from app.core.progress import ProcessingCancelled, ProgressCallback
from app.documents.artifacts import build_and_publish_artifacts
from app.storage.history import HistoryStore, get_history_store

logger = logging.getLogger("predixalearn.queue")
_ACTIVE_GPU_JOBS = 1


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class JobStatus(str, enum.Enum):
    PENDING = "pending"
    PROCESSING = "processing"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


@dataclass(slots=True)
class JobProgress:
    stage: str = "queued"
    message: str = "Upload verified; waiting for GPU"
    current_page: int | None = None
    total_pages: int | None = None
    completed_pages: int = 0
    updated_at: datetime = field(default_factory=_utcnow)


@dataclass(slots=True)
class JobResult:
    job_id: str
    status: JobStatus = JobStatus.PENDING
    workflow: str = ""
    result: dict[str, Any] | list[Any] | None = None
    error: str | None = None
    error_code: str | None = None
    input_name: str = ""
    input_kind: str = "image"
    language: str | None = None
    document_profile: str | None = None
    progress: JobProgress = field(default_factory=JobProgress)
    started_at: datetime | None = None
    completed_at: datetime | None = None
    history_saved: bool = False
    history_warning: str | None = None
    cancel_requested: bool = False
    created_at: datetime = field(default_factory=_utcnow)
    updated_at: datetime = field(default_factory=_utcnow)


@dataclass(slots=True)
class _JobEntry:
    result: JobResult
    workflow_fn: Callable[..., Any]
    input_path: Path
    delete_input: bool
    remove_text: str | None = None
    remove_terms: list[str] = field(default_factory=list)
    language: str | None = None
    document_profile: str = "auto"
    artifact_manifest_path: str | None = None
    future: asyncio.Future[None] | None = field(default=None, repr=False)
    cancel_event: threading.Event = field(default_factory=threading.Event, repr=False)


class JobQueue:
    def __init__(
        self,
        settings: Settings | None = None,
        history_store: HistoryStore | None = None,
    ) -> None:
        self.settings = settings or get_settings()
        self._history = history_store or HistoryStore(self.settings)
        self._history.initialize()
        self._jobs: OrderedDict[str, _JobEntry] = OrderedDict()
        self._lock = threading.RLock()
        self._pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="gpu-ocr")
        self._closed = False

    @property
    def closed(self) -> bool:
        return self._closed

    @property
    def has_active_jobs(self) -> bool:
        """Whether a shutdown would interrupt pending or running OCR work."""
        with self._lock:
            return self._active_count() > 0

    def _active_count(self) -> int:
        active = {JobStatus.PENDING, JobStatus.PROCESSING}
        return sum(entry.result.status in active for entry in self._jobs.values())

    def _prune(self) -> None:
        cutoff = _utcnow() - timedelta(seconds=self.settings.job_ttl_seconds)
        terminal = {JobStatus.COMPLETED, JobStatus.FAILED, JobStatus.CANCELLED}
        expired = [
            job_id
            for job_id, entry in self._jobs.items()
            if entry.result.status in terminal and entry.result.updated_at < cutoff
        ]
        for job_id in expired:
            self._jobs.pop(job_id, None)
        while len(self._jobs) > self.settings.max_job_history:
            removable = next(
                (job_id for job_id, entry in self._jobs.items() if entry.result.status in terminal),
                None,
            )
            if removable is None:
                break
            self._jobs.pop(removable, None)

    def _set_progress(
        self,
        entry: _JobEntry,
        *,
        stage: str,
        message: str,
        current_page: int | None = None,
        total_pages: int | None = None,
        completed_pages: int | None = None,
    ) -> None:
        with self._lock:
            progress = entry.result.progress
            progress.stage = stage
            progress.message = message
            if current_page is not None:
                progress.current_page = current_page
            if total_pages is not None:
                progress.total_pages = total_pages
            if completed_pages is not None:
                progress.completed_pages = completed_pages
            progress.updated_at = _utcnow()
            entry.result.updated_at = progress.updated_at

    @staticmethod
    def _item_count(result: JobResult) -> int | None:
        if result.progress.total_pages is not None:
            return result.progress.total_pages
        if isinstance(result.result, dict):
            page_count = result.result.get("page_count")
            if isinstance(page_count, int) and page_count >= 0:
                return page_count
        return 1 if result.input_kind == "image" else None

    def _persist_history(self, entry: _JobEntry, result_path: str | None = None) -> None:
        result = entry.result
        quality_score = None
        warning_count = 0
        if isinstance(result.result, dict):
            coverage = result.result.get("content_coverage")
            if isinstance(coverage, dict):
                candidate = coverage.get("coverage_percent")
                if isinstance(candidate, int | float):
                    quality_score = float(candidate)
            warnings = result.result.get("warnings")
            warning_count = len(warnings) if isinstance(warnings, list) else 0
        try:
            self._history.upsert(
                job_id=result.job_id,
                input_name=result.input_name,
                workflow=result.workflow,
                input_kind=result.input_kind,
                status=result.status.value,
                item_count=self._item_count(result),
                error=result.error,
                result_path=result_path,
                artifact_manifest_path=entry.artifact_manifest_path,
                created_at=result.created_at,
                started_at=result.started_at,
                completed_at=result.completed_at,
                updated_at=result.updated_at,
                language=entry.language,
                document_profile=entry.document_profile,
                remove_terms=entry.remove_terms,
                quality_score=quality_score,
                warning_count=warning_count,
            )
            result.history_saved = True
            result.history_warning = None
        except Exception:
            logger.exception("Could not persist history for job %s", result.job_id)
            result.history_saved = False
            result.history_warning = "OCR succeeded, but its history record could not be saved"

    async def submit(
        self,
        workflow_fn: Callable[..., Any],
        input_path: str | Path,
        *,
        workflow_name: str,
        input_name: str | None = None,
        delete_input: bool = False,
        remove_text: str | None = None,
        remove_terms: list[str] | None = None,
        language: str | None = None,
        document_profile: str = "auto",
    ) -> str:
        path = Path(input_path)
        with self._lock:
            if self._closed:
                raise RuntimeError("Job queue is shut down")
            self._prune()
            max_inflight = _ACTIVE_GPU_JOBS + self.settings.max_queued_jobs
            if self._active_count() >= max_inflight:
                raise QueueFullError("The OCR queue is full; retry later")
            job_id = uuid.uuid4().hex
            result = JobResult(
                job_id=job_id,
                workflow=workflow_name,
                input_name=input_name or path.name,
                input_kind="pdf" if path.suffix.lower() == ".pdf" else "image",
                language=language,
                document_profile=document_profile,
            )
            entry = _JobEntry(
                result,
                workflow_fn,
                path,
                delete_input,
                remove_text=remove_text,
                remove_terms=list(remove_terms or ()),
                language=language,
                document_profile=document_profile,
            )
            self._jobs[job_id] = entry
            self._persist_history(entry)

        loop = asyncio.get_running_loop()
        entry.future = loop.run_in_executor(self._pool, self._execute, entry)
        logger.info("Job %s queued for %s", job_id, workflow_name)
        return job_id

    def _execute(self, entry: _JobEntry) -> None:
        if entry.cancel_event.is_set():
            self._finish_cancelled(entry)
            return
        with self._lock:
            entry.result.status = JobStatus.PROCESSING
            entry.result.started_at = _utcnow()
            entry.result.updated_at = entry.result.started_at
            self._persist_history(entry)
        self._set_progress(
            entry,
            stage="initializing",
            message="Preparing the OCR engine",
            completed_pages=0,
        )

        def report_progress(**values: Any) -> None:
            if entry.cancel_event.is_set():
                raise ProcessingCancelled("Processing cancelled by the user")
            self._set_progress(entry, **values)

        progress_callback: ProgressCallback = report_progress
        try:
            output_dir = self.settings.output_dir / ".staging" / entry.result.job_id
            workflow_parameters = inspect.signature(entry.workflow_fn).parameters
            workflow_kwargs: dict[str, Any] = {
                "job_id": entry.result.job_id,
                "output_dir": output_dir,
                "progress_callback": progress_callback,
            }
            if "save_output" in workflow_parameters:
                workflow_kwargs["save_output"] = False
            if "language" in workflow_parameters:
                workflow_kwargs["language"] = entry.language
            if "document_profile" in workflow_parameters:
                workflow_kwargs["document_profile"] = entry.document_profile
            raw_data = entry.workflow_fn(str(entry.input_path), **workflow_kwargs)
            if not isinstance(raw_data, dict):
                raw_data = {"result": raw_data}
            outcome = build_and_publish_artifacts(
                entry.input_path,
                entry.result.input_name,
                entry.result.workflow,
                raw_data,
                job_id=entry.result.job_id,
                settings=self.settings,
                progress_callback=progress_callback,
                remove_text=entry.remove_text,
                remove_terms=entry.remove_terms,
            )
            data = outcome.result
            entry.artifact_manifest_path = outcome.manifest_path
            with self._lock:
                entry.result.result = data
            total_pages = self._item_count(entry.result)
            result_path = None
            result_save_failed = False
            try:
                result_path = self._history.save_result(entry.result.job_id, data)
            except Exception:
                logger.exception("Could not save result for job %s", entry.result.job_id)
                result_save_failed = True
            if entry.delete_input:
                entry.input_path.unlink(missing_ok=True)
                entry.delete_input = False
            with self._lock:
                entry.result.status = JobStatus.COMPLETED
            self._set_progress(
                entry,
                stage="completed",
                message="Processing complete",
                current_page=total_pages,
                total_pages=total_pages,
                completed_pages=total_pages or entry.result.progress.completed_pages,
            )
            with self._lock:
                entry.result.completed_at = entry.result.updated_at
                self._persist_history(entry, result_path)
                if result_save_failed:
                    entry.result.history_saved = False
                    entry.result.history_warning = (
                        "OCR succeeded, but its result could not be added to history"
                    )
        except ProcessingCancelled:
            self._finish_cancelled(entry)
            shutil.rmtree(
                self.settings.output_dir / ".staging" / entry.result.job_id,
                ignore_errors=True,
            )
        except OCRServiceError as exc:
            logger.warning("Job %s failed: %s", entry.result.job_id, exc)
            with self._lock:
                entry.result.status = JobStatus.FAILED
                entry.result.error = str(exc)
                entry.result.error_code = type(exc).__name__
            self._set_progress(entry, stage="failed", message=str(exc))
            with self._lock:
                entry.result.completed_at = entry.result.updated_at
                self._persist_history(entry)
        except Exception:
            logger.exception("Unexpected failure in job %s", entry.result.job_id)
            with self._lock:
                entry.result.status = JobStatus.FAILED
                entry.result.error = "OCR processing failed unexpectedly"
                entry.result.error_code = "InternalError"
            self._set_progress(
                entry,
                stage="failed",
                message="OCR processing failed unexpectedly",
            )
            with self._lock:
                entry.result.completed_at = entry.result.updated_at
                self._persist_history(entry)
        finally:
            if entry.delete_input:
                entry.input_path.unlink(missing_ok=True)
            with self._lock:
                self._prune()

    def _finish_cancelled(self, entry: _JobEntry) -> None:
        with self._lock:
            if entry.result.status in {
                JobStatus.COMPLETED,
                JobStatus.FAILED,
                JobStatus.CANCELLED,
            }:
                return
            entry.result.cancel_requested = True
            entry.result.status = JobStatus.CANCELLED
            self._set_progress(
                entry,
                stage="cancelled",
                message="Processing was cancelled",
            )
            entry.result.completed_at = entry.result.updated_at
            self._persist_history(entry)
            if entry.delete_input:
                entry.input_path.unlink(missing_ok=True)
                entry.delete_input = False

    def cancel(self, job_id: str) -> JobResult | None:
        """Request idempotent cancellation and return the latest job snapshot."""
        with self._lock:
            self._prune()
            entry = self._jobs.get(job_id)
            if entry is None:
                return None
            if entry.result.status in {
                JobStatus.COMPLETED,
                JobStatus.FAILED,
                JobStatus.CANCELLED,
            }:
                return replace(entry.result, progress=replace(entry.result.progress))
            entry.result.cancel_requested = True
            entry.cancel_event.set()
            if entry.result.status == JobStatus.PENDING and entry.future and entry.future.cancel():
                self._finish_cancelled(entry)
            else:
                self._set_progress(
                    entry,
                    stage=entry.result.progress.stage,
                    message="Cancellation requested; finishing the current safe operation",
                )
            return replace(entry.result, progress=replace(entry.result.progress))

    def queue_position(self, job_id: str) -> int | None:
        with self._lock:
            pending = [
                entry.result.job_id
                for entry in self._jobs.values()
                if entry.result.status == JobStatus.PENDING
            ]
            try:
                return pending.index(job_id) + 1
            except ValueError:
                return None

    def get_result(self, job_id: str) -> JobResult | None:
        with self._lock:
            self._prune()
            entry = self._jobs.get(job_id)
            return (
                replace(entry.result, progress=replace(entry.result.progress)) if entry else None
            )

    async def wait_for_result(self, job_id: str) -> JobResult:
        with self._lock:
            entry = self._jobs.get(job_id)
        if entry is None:
            return JobResult(
                job_id=job_id,
                status=JobStatus.FAILED,
                error="Job not found",
            )
        if entry.future is not None:
            try:
                await entry.future
            except asyncio.CancelledError:
                if not entry.cancel_event.is_set():
                    raise
        return self.get_result(job_id) or entry.result

    def list_jobs(self) -> dict[str, JobResult]:
        with self._lock:
            self._prune()
            return {
                job_id: replace(entry.result, progress=replace(entry.result.progress))
                for job_id, entry in self._jobs.items()
            }

    def shutdown(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            for entry in self._jobs.values():
                if entry.result.status in {JobStatus.PENDING, JobStatus.PROCESSING}:
                    entry.cancel_event.set()
                    entry.result.cancel_requested = True
                    if entry.result.status == JobStatus.PENDING and entry.future:
                        if entry.future.cancel():
                            self._finish_cancelled(entry)
        self._pool.shutdown(wait=True, cancel_futures=True)


_queue: JobQueue | None = None
_queue_lock = threading.Lock()


def get_queue() -> JobQueue:
    global _queue
    if _queue is None or _queue.closed:
        with _queue_lock:
            if _queue is None or _queue.closed:
                _queue = JobQueue(history_store=get_history_store())
    return _queue


def reset_queue() -> None:
    global _queue
    with _queue_lock:
        if _queue is not None:
            _queue.shutdown()
        _queue = None
