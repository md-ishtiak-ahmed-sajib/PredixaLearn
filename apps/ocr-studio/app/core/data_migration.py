"""Non-destructive migration of legacy project-local runtime data."""

from __future__ import annotations

import json
import logging
import os
import shutil
import sqlite3
import subprocess
import uuid
from datetime import datetime, timezone
from pathlib import Path

from app.core.config import PROJECT_ROOT, Settings, default_data_root, legacy_data_root

logger = logging.getLogger("predixalearn.data_migration")
_MARKER_NAME = ".runtime-migration-v2.json"
_RUNTIME_OVERRIDE_NAMES = (
    "OCR_OUTPUT_DIR",
    "OCR_UPLOAD_DIR",
    "OCR_HISTORY_DIR",
    "OCR_LOG_DIR",
)


def _copy_missing(source: Path, destination: Path) -> tuple[int, list[str]]:
    copied = 0
    conflicts: list[str] = []
    if not source.exists():
        return copied, conflicts
    for source_file in source.rglob("*"):
        if not source_file.is_file():
            continue
        relative = source_file.relative_to(source)
        target = destination / relative
        if target.exists():
            if target.stat().st_size != source_file.stat().st_size:
                conflicts.append(relative.as_posix())
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source_file, target)
        copied += 1
    return copied, conflicts


def _backup_sqlite(source: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    source_connection = sqlite3.connect(source)
    destination_connection = sqlite3.connect(destination)
    try:
        source_connection.backup(destination_connection)
    finally:
        destination_connection.close()
        source_connection.close()


def migrate_legacy_runtime_data(settings: Settings) -> dict[str, object]:
    """Copy default legacy data once, leaving all original files untouched."""
    data_root = default_data_root()
    marker = data_root / _MARKER_NAME
    if marker.is_file():
        try:
            return json.loads(marker.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            logger.warning("Runtime migration marker is unreadable; preserving it")
            return {"status": "marker_unreadable"}

    previous_data_root = legacy_data_root()
    mappings = (
        (
            "output",
            "OCR_OUTPUT_DIR",
            (previous_data_root / "output", PROJECT_ROOT / "output"),
            settings.output_dir,
        ),
        (
            "history",
            "OCR_HISTORY_DIR",
            (previous_data_root / "history", PROJECT_ROOT / "app" / "history"),
            settings.history_dir,
        ),
        (
            "logs",
            "OCR_LOG_DIR",
            (previous_data_root / "logs", PROJECT_ROOT / "logs"),
            settings.log_dir,
        ),
    )
    eligible = [item for item in mappings if os.getenv(item[1]) is None]
    if not eligible:
        return {"status": "overridden"}

    data_root.mkdir(parents=True, exist_ok=True)
    staging = data_root / f".migration-v1-{uuid.uuid4().hex}"
    staging.mkdir(parents=True)
    copied_total = 0
    conflicts: dict[str, list[str]] = {}
    try:
        for name, _environment_name, legacy_sources, target in eligible:
            staged_target = staging / name
            component_conflicts: list[str] = []
            for legacy in legacy_sources:
                copied, source_conflicts = _copy_missing(legacy, staged_target)
                copied_total += copied
                component_conflicts.extend(source_conflicts)
            if name == "history":
                staged_db = staged_target / "history.sqlite3"
                for legacy in legacy_sources:
                    legacy_db = legacy / "history.sqlite3"
                    if legacy_db.is_file():
                        staged_db.unlink(missing_ok=True)
                        _backup_sqlite(legacy_db, staged_db)
                        break
            target.mkdir(parents=True, exist_ok=True)
            promoted, target_conflicts = _copy_missing(staged_target, target)
            copied_total += promoted
            if component_conflicts or target_conflicts:
                conflicts[name] = sorted(set(component_conflicts + target_conflicts))
        result: dict[str, object] = {
            "version": 2,
            "status": "completed",
            "completed_at": datetime.now(timezone.utc).isoformat(),
            "copied_files": copied_total,
            "conflicts": conflicts,
            "legacy_project_root": str(PROJECT_ROOT),
        }
        temporary_marker = marker.with_suffix(".tmp")
        temporary_marker.write_text(json.dumps(result, indent=2), encoding="utf-8")
        os.replace(temporary_marker, marker)
        logger.info("Runtime data migration completed with %d copied files", copied_total)
        return result
    finally:
        shutil.rmtree(staging, ignore_errors=True)


def create_runtime_directories(settings: Settings) -> None:
    """Create app data directories and restrict default Windows paths to the user."""
    directories = (
        settings.output_dir,
        settings.upload_dir,
        settings.history_dir,
        settings.log_dir,
        settings.maintenance_dir,
        settings.institution_dir,
    )
    for directory in directories:
        directory.mkdir(parents=True, exist_ok=True)
    if os.name != "nt" or any(os.getenv(name) for name in _RUNTIME_OVERRIDE_NAMES):
        return

    account = os.getenv("USERNAME") or os.getenv("USER")
    if not account:
        logger.warning("Could not determine the Windows user for runtime ACL setup")
        return
    icacls = Path(os.getenv("SystemRoot", r"C:\Windows")) / "System32" / "icacls.exe"
    if not icacls.is_file():
        logger.warning("Windows ACL utility is unavailable; runtime permissions were not changed")
        return
    root = default_data_root()
    root.mkdir(parents=True, exist_ok=True)
    for directory in (root, *directories):
        try:
            completed = subprocess.run(  # noqa: S603
                [
                    str(icacls),
                    str(directory),
                    "/inheritance:r",
                    "/grant:r",
                    f"{account}:(OI)(CI)F",
                ],
                check=False,
                capture_output=True,
                text=True,
                timeout=15,
            )
        except (OSError, subprocess.TimeoutExpired):
            logger.warning("Could not set private permissions on %s", directory.name)
            continue
        if completed.returncode != 0:
            logger.warning("Could not set private permissions on %s", directory.name)
