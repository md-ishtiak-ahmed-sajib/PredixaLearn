"""Validation for generated OCR bundles and named-output replacement policy."""

from __future__ import annotations

import json
import zipfile
from pathlib import Path

from app.core.config import Settings


def validate_bundle(bundle: Path, output_stem: str, *, require_docx: bool) -> None:
    markdown = bundle / f"{output_stem}.md"
    result_json = bundle / f"{output_stem}.json"
    if not markdown.is_file() or not markdown.read_text(encoding="utf-8").strip():
        raise RuntimeError("Generated Markdown is missing or empty")
    if not result_json.is_file() or result_json.stat().st_size < 2:
        raise RuntimeError("Generated JSON is missing or empty")
    if require_docx:
        docx = bundle / f"{output_stem}.docx"
        if not docx.is_file() or not zipfile.is_zipfile(docx):
            raise RuntimeError("Generated DOCX is invalid")


def named_output_is_complete(*, settings: Settings, output_stem: str) -> bool:
    """Protect a complete named export from a later partial reprocessing attempt."""
    target = settings.output_dir / output_stem
    docx = target / f"{output_stem}.docx"
    result_json = target / f"{output_stem}.json"
    if not docx.is_file() or not zipfile.is_zipfile(docx):
        return False
    if not result_json.is_file():
        return True
    try:
        saved = json.loads(result_json.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return True
    return str(saved.get("artifact_status", "complete")) == "complete"
