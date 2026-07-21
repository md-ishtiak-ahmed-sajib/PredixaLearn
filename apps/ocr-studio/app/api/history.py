"""Persistent OCR history API with bounded reads and safe downloads."""

from __future__ import annotations

import json
import os
import re
import uuid
from datetime import datetime
from typing import Annotated, Any, Literal

import aiofiles
from fastapi import APIRouter, File, HTTPException, Query, UploadFile, status
from fastapi.responses import FileResponse, Response
from pydantic import BaseModel, Field
from starlette.background import BackgroundTask

from app.storage.history import (
    HistoryConflictError,
    HistoryNotFoundError,
    HistoryRecord,
    HistoryResultError,
    get_history_store,
)
from app.storage.history_archive import build_history_archive, import_history_archive

router = APIRouter(prefix="/api/v1/history", tags=["history"])
_FILENAME_UNSAFE = re.compile(r"[^A-Za-z0-9._-]+")


class PinUpdate(BaseModel):
    pinned: bool


class BulkHistoryRequest(BaseModel):
    job_ids: list[str] = Field(min_length=1, max_length=100)


def _primary_output(record: HistoryRecord, result: Any) -> tuple[str, str, str]:
    if record.workflow == "text_recognition" and isinstance(result, dict):
        return str(result.get("full_text", "")), "text", "txt"
    if record.workflow in {"layout_parsing", "vl_processing"} and isinstance(result, dict):
        return str(result.get("markdown", "")), "markdown", "md"
    return (
        json.dumps(result, ensure_ascii=False, indent=2),
        "json",
        "json",
    )


def _safe_download_stem(record: HistoryRecord) -> str:
    original_stem = record.input_name.rsplit(".", 1)[0]
    sanitized = _FILENAME_UNSAFE.sub("_", original_stem).strip("._")[:80]
    return sanitized or f"ocr-{record.job_id[:8]}"


def _get_record(job_id: str) -> HistoryRecord:
    try:
        return get_history_store().get(job_id)
    except HistoryNotFoundError as exc:
        raise HTTPException(status_code=404, detail="History record not found") from exc


def _get_result(record: HistoryRecord) -> Any:
    try:
        return get_history_store().read_result(record)
    except HistoryResultError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.get("")
async def list_history(
    q: Annotated[str, Query(max_length=200)] = "",
    workflow: Annotated[str | None, Query()] = None,
    status_filter: Annotated[str | None, Query(alias="status")] = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
    offset: Annotated[int, Query(ge=0)] = 0,
    created_from: Annotated[datetime | None, Query()] = None,
    created_to: Annotated[datetime | None, Query()] = None,
    pinned: Annotated[bool | None, Query()] = None,
    sort: Annotated[
        Literal["created_at", "duration", "input_name", "quality"], Query()
    ] = "created_at",
    order: Annotated[Literal["asc", "desc"], Query()] = "desc",
) -> dict[str, Any]:
    records, total = get_history_store().list(
        query=q.strip(),
        workflow=workflow,
        status=status_filter,
        limit=limit,
        offset=offset,
        created_from=created_from.isoformat() if created_from else None,
        created_to=created_to.isoformat() if created_to else None,
        pinned=pinned,
        sort=sort,
        order=order,
    )
    return {
        "items": [record.summary() for record in records],
        "total": total,
        "limit": limit,
        "offset": offset,
    }


@router.get("/{job_id}/download")
async def download_history_result(
    job_id: str,
    format_name: Annotated[
        Literal["primary", "markdown", "docx", "json", "csv", "xlsx"],
        Query(alias="format"),
    ] = "primary",
    table_index: Annotated[int, Query(alias="table", ge=1, le=10_000)] = 1,
) -> Response:
    record = _get_record(job_id)
    stem = _safe_download_stem(record)
    if format_name in {"csv", "xlsx"}:
        try:
            path = get_history_store().table_artifact_path(
                record,
                table_index=table_index,
                extension=format_name,
            )
        except HistoryResultError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        media_type = (
            "text/csv; charset=utf-8"
            if format_name == "csv"
            else "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
        )
        return FileResponse(
            path,
            media_type=media_type,
            filename=f"{stem}-table-{table_index:03d}.{format_name}",
        )
    if format_name in {"markdown", "docx"}:
        try:
            path = get_history_store().artifact_path(record, format_name)
        except HistoryResultError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        media_type = (
            "text/markdown; charset=utf-8"
            if format_name == "markdown"
            else "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
        )
        extension = "md" if format_name == "markdown" else "docx"
        return FileResponse(
            path,
            media_type=media_type,
            filename=f"{stem}.{extension}",
        )

    result = _get_result(record)
    if format_name == "json":
        content = json.dumps(result, ensure_ascii=False, indent=2)
        extension = "json"
        media_type = "application/json"
    else:
        content, kind, extension = _primary_output(record, result)
        media_type = "application/json" if kind == "json" else "text/plain; charset=utf-8"
    return Response(
        content=content.encode("utf-8"),
        media_type=media_type,
        headers={"Content-Disposition": f'attachment; filename="{stem}.{extension}"'},
    )


@router.get("/{job_id}")
async def get_history_detail(job_id: str) -> dict[str, Any]:
    record = _get_record(job_id)
    store = get_history_store()
    result = _get_result(record) if record.result_path else None
    primary_output = ""
    primary_format = None
    if result is not None:
        primary_output, primary_format, _ = _primary_output(record, result)
    availability = {"markdown": False, "docx": False, "json": result is not None}
    table_downloads: dict[int, dict[str, Any]] = {}
    requires_reupload = bool(record.legacy)
    if record.artifact_manifest_path:
        try:
            manifest = store.read_manifest(record) or {}
            files = manifest.get("files", {})
            if isinstance(files, dict):
                availability["markdown"] = bool(files.get("markdown"))
                availability["docx"] = bool(files.get("docx"))
                table_files = files.get("tables")
                if isinstance(table_files, list):
                    for filename in table_files:
                        if not isinstance(filename, str):
                            continue
                        match = re.search(
                            r"(?:^|/)table-(\d+)\.(csv|xlsx)$",
                            filename.replace("\\", "/"),
                            re.IGNORECASE,
                        )
                        if not match:
                            continue
                        index = int(match.group(1))
                        entry = table_downloads.setdefault(
                            index,
                            {"table_index": index, "csv": False, "xlsx": False},
                        )
                        entry[match.group(2).lower()] = True
            requires_reupload = bool(manifest.get("requires_reupload", False))
        except HistoryResultError:
            pass
    return {
        **record.summary(),
        "primary_output": primary_output,
        "primary_format": primary_format,
        "result": result,
        "artifact_availability": availability,
        "table_downloads": [table_downloads[index] for index in sorted(table_downloads)],
        "requires_reupload": requires_reupload,
    }


@router.delete("/{job_id}", status_code=status.HTTP_200_OK)
async def delete_history_record(job_id: str) -> dict[str, Any]:
    try:
        get_history_store().delete(job_id)
    except HistoryNotFoundError as exc:
        raise HTTPException(status_code=404, detail="History record not found") from exc
    except HistoryConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except HistoryResultError as exc:
        raise HTTPException(status_code=500, detail="History output could not be removed") from exc
    return {"deleted": True, "job_id": job_id}


@router.patch("/{job_id}")
async def update_history_record(job_id: str, update: PinUpdate) -> dict[str, Any]:
    try:
        return get_history_store().set_pinned(job_id, update.pinned).summary()
    except HistoryNotFoundError as exc:
        raise HTTPException(status_code=404, detail="History record not found") from exc


@router.post("/bulk-delete")
async def bulk_delete_history(request: BulkHistoryRequest) -> dict[str, int]:
    try:
        deleted = get_history_store().delete_many(request.job_ids)
    except HistoryNotFoundError as exc:
        raise HTTPException(status_code=404, detail="A History record was not found") from exc
    except HistoryConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return {"deleted_count": deleted}


@router.post("/export", response_class=FileResponse)
async def export_history(request: BulkHistoryRequest) -> FileResponse:
    store = get_history_store()
    destination = store.settings.history_dir / ".exports" / f"{uuid.uuid4().hex}.zip"
    try:
        build_history_archive(store, request.job_ids, destination)
    except HistoryNotFoundError as exc:
        raise HTTPException(status_code=404, detail="A History record was not found") from exc
    except HistoryResultError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return FileResponse(
        destination,
        media_type="application/zip",
        filename="predixalearn-history.zip",
        background=BackgroundTask(destination.unlink, missing_ok=True),
    )


@router.post("/import")
async def import_history(file: Annotated[UploadFile, File()]) -> dict[str, int]:
    store = get_history_store()
    maximum = store.settings.max_history_import_bytes
    temporary = store.settings.history_dir / ".imports" / f".{uuid.uuid4().hex}.upload"
    temporary.parent.mkdir(parents=True, exist_ok=True)
    size = 0
    try:
        async with aiofiles.open(temporary, "wb") as destination:
            while chunk := await file.read(1024 * 1024):
                size += len(chunk)
                if size > maximum:
                    raise HTTPException(status_code=413, detail="History archive is too large")
                await destination.write(chunk)
        if size == 0:
            raise HTTPException(status_code=422, detail="History archive is empty")
        try:
            return import_history_archive(store, temporary, maximum)
        except HistoryResultError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
    finally:
        await file.close()
        temporary.unlink(missing_ok=True)


@router.post("/{job_id}/reveal")
async def reveal_history_output(job_id: str) -> dict[str, bool]:
    if os.name != "nt":
        raise HTTPException(status_code=501, detail="Folder reveal is available on Windows only")
    record = _get_record(job_id)
    try:
        target = get_history_store().reveal_path(record)
        os.startfile(target)  # type: ignore[attr-defined]  # noqa: S606
    except HistoryResultError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except OSError as exc:
        raise HTTPException(status_code=500, detail="The output folder could not be opened") from exc
    return {"opened": True}


@router.delete("", status_code=status.HTTP_200_OK)
async def clear_history(
    older_than_days: Annotated[int | None, Query(ge=1, le=3650)] = None,
) -> dict[str, int]:
    return {
        "deleted_count": get_history_store().clear_terminal(
            older_than_days=older_than_days
        )
    }
