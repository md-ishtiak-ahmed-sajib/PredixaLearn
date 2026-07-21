"""Versioned, bounded History archive import and export."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import stat
import uuid
import zipfile
from datetime import datetime
from pathlib import Path, PurePosixPath
from typing import Any

from app.core.serialization import json_safe
from app.storage.history import HistoryResultError, HistoryStore

ARCHIVE_VERSION = 1
_MAX_ARCHIVE_FILES = 10_000
_ALLOWED_SUFFIXES = {
    ".json",
    ".md",
    ".txt",
    ".docx",
    ".csv",
    ".xlsx",
    ".png",
    ".jpg",
    ".jpeg",
    ".webp",
    ".bmp",
    ".tif",
    ".tiff",
}


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def build_history_archive(store: HistoryStore, job_ids: list[str], destination: Path) -> Path:
    records = [store.get(job_id) for job_id in dict.fromkeys(job_ids)]
    manifest_records: list[dict[str, Any]] = []
    manifest_files: list[dict[str, Any]] = []
    destination.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(destination, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for record in records:
            job_root = store.settings.history_dir / "jobs" / record.job_id
            record_files: list[str] = []
            if job_root.is_dir():
                for source in sorted(job_root.rglob("*")):
                    if not source.is_file():
                        continue
                    relative = source.relative_to(job_root).as_posix()
                    archive_name = f"records/{record.job_id}/{relative}"
                    archive.write(source, archive_name)
                    record_files.append(archive_name)
                    manifest_files.append(
                        {
                            "path": archive_name,
                            "size": source.stat().st_size,
                            "sha256": _sha256(source),
                        }
                    )
            manifest_records.append(
                {
                    **record.summary(),
                    "legacy": bool(record.legacy),
                    "files": record_files,
                }
            )
        manifest = {
            "archive_version": ARCHIVE_VERSION,
            "created_at": datetime.now().astimezone().isoformat(),
            "records": manifest_records,
            "files": manifest_files,
        }
        archive.writestr(
            "manifest.json",
            json.dumps(json_safe(manifest), ensure_ascii=False, indent=2),
        )
    return destination


def _safe_archive_path(value: str) -> PurePosixPath:
    path = PurePosixPath(value.replace("\\", "/"))
    if path.is_absolute() or ".." in path.parts or not path.parts:
        raise HistoryResultError("History archive contains an unsafe path")
    return path


def _validate_member(info: zipfile.ZipInfo, max_bytes: int) -> PurePosixPath:
    path = _safe_archive_path(info.filename)
    mode = info.external_attr >> 16
    if stat.S_ISLNK(mode):
        raise HistoryResultError("History archive symlinks are not allowed")
    if info.file_size > max_bytes:
        raise HistoryResultError("A History archive entry exceeds the size limit")
    if info.is_dir():
        return path
    if path.name != "manifest.json" and path.suffix.lower() not in _ALLOWED_SUFFIXES:
        raise HistoryResultError("History archive contains an unsupported file type")
    return path


def import_history_archive(store: HistoryStore, source: Path, max_bytes: int) -> dict[str, int]:
    staging_root = store.settings.history_dir / ".imports" / uuid.uuid4().hex
    staging_root.mkdir(parents=True)
    imported = 0
    reassigned = 0
    try:
        with zipfile.ZipFile(source) as archive:
            infos = archive.infolist()
            if len(infos) > _MAX_ARCHIVE_FILES:
                raise HistoryResultError("History archive contains too many files")
            names: set[str] = set()
            total_size = 0
            for info in infos:
                safe = _validate_member(info, max_bytes)
                normalized = safe.as_posix()
                if normalized in names:
                    raise HistoryResultError("History archive contains duplicate paths")
                names.add(normalized)
                total_size += info.file_size
                if total_size > max_bytes:
                    raise HistoryResultError("Expanded History archive exceeds the size limit")
            try:
                manifest = json.loads(archive.read("manifest.json"))
            except (KeyError, json.JSONDecodeError, UnicodeError) as exc:
                raise HistoryResultError("History archive manifest is invalid") from exc
            if not isinstance(manifest, dict) or manifest.get("archive_version") != ARCHIVE_VERSION:
                raise HistoryResultError("History archive version is unsupported")
            records = manifest.get("records")
            file_entries = manifest.get("files")
            if not isinstance(records, list) or not isinstance(file_entries, list):
                raise HistoryResultError("History archive manifest is malformed")
            expected = {
                str(item.get("path")): item
                for item in file_entries
                if isinstance(item, dict) and isinstance(item.get("path"), str)
            }
            for archive_name, metadata in expected.items():
                if archive_name not in names:
                    raise HistoryResultError("History archive is missing a declared file")
                payload = archive.read(archive_name)
                if len(payload) != metadata.get("size"):
                    raise HistoryResultError("History archive file size does not match")
                if hashlib.sha256(payload).hexdigest() != metadata.get("sha256"):
                    raise HistoryResultError("History archive checksum validation failed")

            for raw_record in records:
                if not isinstance(raw_record, dict):
                    raise HistoryResultError("History archive record is malformed")
                original_id = str(raw_record.get("job_id", ""))
                store._validate_job_id(original_id)
                new_id = original_id
                try:
                    store.get(new_id)
                except LookupError:
                    pass
                else:
                    new_id = uuid.uuid4().hex
                    reassigned += 1
                target_root = store.settings.history_dir / "jobs" / new_id
                staged_job = staging_root / new_id
                declared_files = raw_record.get("files")
                if not isinstance(declared_files, list):
                    raise HistoryResultError("History archive record has no file list")
                for archive_name in declared_files:
                    if not isinstance(archive_name, str) or archive_name not in expected:
                        raise HistoryResultError("History archive record references an invalid file")
                    path = _safe_archive_path(archive_name)
                    expected_prefix = PurePosixPath("records") / original_id
                    try:
                        relative = path.relative_to(expected_prefix)
                    except ValueError as exc:
                        raise HistoryResultError("History archive record path is invalid") from exc
                    target = staged_job.joinpath(*relative.parts)
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_bytes(archive.read(archive_name))
                manifest_path = staged_job / "manifest.json"
                if manifest_path.is_file() and new_id != original_id:
                    value = json.loads(manifest_path.read_text(encoding="utf-8"))
                    if isinstance(value, dict):
                        value["job_id"] = new_id
                        archive_root = value.get("archive_root")
                        if isinstance(archive_root, str):
                            value["archive_root"] = archive_root.replace(original_id, new_id, 1)
                        manifest_path.write_text(
                            json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8"
                        )
                if target_root.exists():
                    raise HistoryResultError("History archive target already exists")
                os.replace(staged_job, target_root)
                result_path = f"jobs/{new_id}/result.json"
                artifact_manifest_path = f"jobs/{new_id}/manifest.json"
                created_at = datetime.fromisoformat(str(raw_record["created_at"]))
                started_at = (
                    datetime.fromisoformat(str(raw_record["started_at"]))
                    if raw_record.get("started_at")
                    else None
                )
                completed_at = (
                    datetime.fromisoformat(str(raw_record["completed_at"]))
                    if raw_record.get("completed_at")
                    else None
                )
                updated_at = datetime.fromisoformat(str(raw_record["updated_at"]))
                store.upsert(
                    job_id=new_id,
                    input_name=str(raw_record.get("input_name") or "Imported OCR result"),
                    workflow=str(raw_record["workflow"]),
                    input_kind=str(raw_record["input_kind"]),
                    status=str(raw_record["status"]),
                    item_count=raw_record.get("item_count"),
                    error=str(raw_record["error"]) if raw_record.get("error") else None,
                    result_path=result_path if (target_root / "result.json").is_file() else None,
                    artifact_manifest_path=(
                        artifact_manifest_path if (target_root / "manifest.json").is_file() else None
                    ),
                    legacy=bool(raw_record.get("legacy")),
                    created_at=created_at,
                    started_at=started_at,
                    completed_at=completed_at,
                    updated_at=updated_at,
                    pinned=bool(raw_record.get("pinned")),
                    language=raw_record.get("language"),
                    document_profile=raw_record.get("document_profile"),
                    remove_terms=raw_record.get("remove_terms"),
                    quality_score=raw_record.get("quality_score"),
                    warning_count=int(raw_record.get("warning_count") or 0),
                    imported_from_job_id=original_id,
                )
                imported += 1
        return {"imported_count": imported, "reassigned_count": reassigned}
    except (OSError, zipfile.BadZipFile) as exc:
        raise HistoryResultError("History archive is unreadable or corrupted") from exc
    finally:
        shutil.rmtree(staging_root, ignore_errors=True)
