"""Restart-safe batch coordinator backed by the local teaching SQLite schema."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import shutil
import zipfile
from pathlib import Path
from typing import Any

from app.core.queue import JobStatus, QueueFullError, get_queue
from app.teaching.store import TeachingStore
from app.workflows import (
    run_layout_parsing,
    run_table_extraction,
    run_text_recognition,
    run_vl_processing,
)

logger = logging.getLogger("predixalearn.batches")
_WORKFLOWS = {
    "text_recognition": run_text_recognition,
    "layout_parsing": run_layout_parsing,
    "table_extraction": run_table_extraction,
    "vl_processing": run_vl_processing,
}


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def build_batch_manifest(store: TeachingStore, batch_id: str) -> Path:
    """Export metadata and output references without copying staged sources."""

    batch = store.get_batch(batch_id)
    root = store.settings.history_dir / "batch-exports" / batch_id
    root.mkdir(parents=True, exist_ok=True)
    items = []
    for item in batch["items"]:
        output_hash = None
        if item.get("job_id"):
            try:
                output_hash = store.source_hash(str(item["job_id"]))
            except Exception:
                output_hash = None
        items.append(
            {
                "item_id": item["item_id"],
                "position": item["position"],
                "input_name": item["input_name"],
                "source_sha256": item["source_sha256"],
                "status": item["status"],
                "job_id": item.get("job_id"),
                "ocr_result_sha256": output_hash,
                "error": item.get("error"),
                "overrides": item.get("overrides", {}),
            }
        )
    manifest = {
        "version": 1,
        "batch_id": batch_id,
        "name": batch["name"],
        "workflow": batch["workflow"],
        "language": batch["language"],
        "status": batch["status"],
        "staged_sources_included": False,
        "items": items,
    }
    manifest_path = root / "manifest.json"
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True),
        encoding="utf-8",
    )
    checksum = file_sha256(manifest_path)
    checksums = root / "SHA256SUMS.txt"
    checksums.write_text(f"{checksum}  manifest.json\n", encoding="ascii")
    archive = root / "predixalearn-batch-manifest.zip"
    with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED) as bundle:
        bundle.write(manifest_path, "manifest.json")
        bundle.write(checksums, "SHA256SUMS.txt")
    return archive


class BatchCoordinator:
    def __init__(self, store: TeachingStore | None = None) -> None:
        self.store = store or TeachingStore()
        self._stop = asyncio.Event()
        self._task: asyncio.Task[None] | None = None

    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    def start(self) -> None:
        if self.running:
            return
        recovered = self.store.recover_batches()
        if recovered:
            logger.warning("Recovered %d interrupted batch item(s)", recovered)
        self._stop.clear()
        self._task = asyncio.create_task(self._run(), name="predixalearn-batch-coordinator")

    async def stop(self) -> None:
        self._stop.set()
        if self._task:
            try:
                await asyncio.wait_for(self._task, timeout=3)
            except TimeoutError:
                self._task.cancel()
                await asyncio.gather(self._task, return_exceptions=True)
        self._task = None

    async def _run(self) -> None:
        while not self._stop.is_set():
            candidate = self._next_item()
            if candidate is None:
                try:
                    await asyncio.wait_for(self._stop.wait(), timeout=0.75)
                except TimeoutError:
                    continue
                continue
            batch, item = candidate
            await self._process(batch, item)

    def _next_item(self) -> tuple[dict[str, Any], dict[str, Any]] | None:
        for batch in reversed(self.store.list_batches()):
            if batch["status"] not in {"queued", "processing"}:
                continue
            queued = next((item for item in batch["items"] if item["status"] == "queued"), None)
            if queued:
                return batch, queued
            if batch["status"] == "processing":
                terminal = {item["status"] for item in batch["items"]}
                final = "completed" if terminal <= {"completed"} else "partial"
                self.store.patch_batch(batch["batch_id"], {"status": final, "actor": "batch-coordinator"})
        return None

    def _safe_staged_path(self, value: str) -> Path:
        root = (self.store.settings.upload_dir / "batches").resolve()
        candidate = (root / value).resolve() if not Path(value).is_absolute() else Path(value).resolve()
        try:
            candidate.relative_to(root)
        except ValueError as exc:
            raise ValueError("Stored batch source path is outside the protected batch directory") from exc
        return candidate

    async def _process(self, batch: dict[str, Any], item: dict[str, Any]) -> None:
        batch_id, item_id = batch["batch_id"], item["item_id"]
        self.store.patch_batch(batch_id, {"status": "processing", "actor": "batch-coordinator"})
        path: Path | None = None
        try:
            path = self._safe_staged_path(item["staged_path"])
            if not path.is_file() or file_sha256(path) != item["source_sha256"]:
                raise ValueError("The staged batch source is missing or failed its checksum")
            if shutil.disk_usage(path.parent).free < max(item["size_bytes"] * 2, 100 * 1024 * 1024):
                raise ValueError("Insufficient disk space to process this batch item safely")
            workflow = _WORKFLOWS.get(batch["workflow"])
            if workflow is None:
                raise ValueError("The stored batch workflow is unsupported")
            overrides = item.get("overrides") if isinstance(item.get("overrides"), dict) else {}
            selected_language = str(
                overrides.get("language", batch.get("language", "en"))
            ).strip().casefold()
            if selected_language != "en":
                raise ValueError(
                    "This queued item uses a language that is unavailable in the "
                    "current English-only release; recreate the batch in English"
                )
            job_id = await get_queue().submit(
                workflow,
                path,
                workflow_name=batch["workflow"],
                input_name=item["input_name"],
                delete_input=False,
                remove_terms=list(overrides.get("remove_terms", batch["remove_terms"])),
                language=selected_language,
                document_profile=str(
                    overrides.get("document_profile", batch.get("document_profile") or "auto")
                ),
            )
            self.store.patch_batch_item(batch_id, item_id, {"status": "processing", "job_id": job_id})
            result = await get_queue().wait_for_result(job_id)
            if result.status == JobStatus.COMPLETED:
                self.store.patch_batch_item(batch_id, item_id, {"status": "completed", "job_id": job_id})
                path.unlink(missing_ok=True)
            elif result.status == JobStatus.CANCELLED:
                self.store.patch_batch_item(batch_id, item_id, {"status": "cancelled", "job_id": job_id, "error": result.error})
                path.unlink(missing_ok=True)
            else:
                self.store.patch_batch_item(batch_id, item_id, {"status": "failed", "job_id": job_id, "error": result.error or "OCR processing failed", "retry_pending": True})
        except QueueFullError:
            self.store.patch_batch_item(batch_id, item_id, {"status": "queued", "retry_pending": True, "error": "OCR queue is busy; retrying automatically"})
            await asyncio.sleep(1)
        except asyncio.CancelledError:
            self.store.patch_batch_item(batch_id, item_id, {"status": "interrupted", "retry_pending": True, "error": "Service stopped during processing"})
            raise
        except Exception as exc:
            logger.exception("Batch item %s failed", item_id)
            self.store.patch_batch_item(batch_id, item_id, {"status": "failed", "retry_pending": True, "error": str(exc)[:1000]})


_coordinator: BatchCoordinator | None = None


def get_batch_coordinator() -> BatchCoordinator:
    global _coordinator
    if _coordinator is None:
        _coordinator = BatchCoordinator()
    return _coordinator


def start_batch_coordinator() -> None:
    get_batch_coordinator().start()


async def stop_batch_coordinator() -> None:
    global _coordinator
    if _coordinator is not None:
        await _coordinator.stop()
    _coordinator = None
