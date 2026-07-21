"""Atomic local publishing for OCR bundles and private History copies."""

from __future__ import annotations

import os
import shutil
import uuid
from pathlib import Path

from app.core.config import Settings


def copy_history_archive(
    bundle: Path, *, settings: Settings, job_id: str, output_stem: str
) -> str:
    """Copy a completed bundle into History without exposing partial output."""
    job_root = settings.history_dir / "jobs" / job_id
    artifacts_root = job_root / "artifacts"
    target = artifacts_root / output_stem
    temporary = artifacts_root / f".{output_stem}.{uuid.uuid4().hex}.tmp"
    artifacts_root.mkdir(parents=True, exist_ok=True)
    try:
        shutil.copytree(bundle, temporary)
        if target.exists():
            shutil.rmtree(target)
        os.replace(temporary, target)
    finally:
        if temporary.exists():
            shutil.rmtree(temporary, ignore_errors=True)
    return target.relative_to(settings.history_dir).as_posix()


def publish_named_output(
    bundle: Path, *, settings: Settings, output_stem: str, job_id: str
) -> Path:
    """Promote a staged bundle atomically and restore the prior output on failure."""
    target = settings.output_dir / output_stem
    rollback = settings.output_dir / ".staging" / job_id / ".previous"
    if rollback.exists():
        shutil.rmtree(rollback, ignore_errors=True)
    moved_previous = False
    try:
        if target.exists():
            os.replace(target, rollback)
            moved_previous = True
        os.replace(bundle, target)
    except Exception:
        if moved_previous and rollback.exists() and not target.exists():
            os.replace(rollback, target)
        raise
    if rollback.exists():
        shutil.rmtree(rollback, ignore_errors=True)
    return target
