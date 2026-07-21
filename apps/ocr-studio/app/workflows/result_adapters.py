"""Strict PaddleOCR 3.7 result adapters shared by document workflows."""

from __future__ import annotations

import uuid
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from app.core.config import Settings, get_settings
from app.core.errors import WorkflowResultError
from app.core.serialization import atomic_write_json, atomic_write_text, json_safe, public_path


def result_payload(result: object, *, workflow: str) -> Mapping[str, Any]:
    """Return the documented ``result.json['res']`` mapping or fail clearly."""
    serialized = getattr(result, "json", None)
    if not isinstance(serialized, Mapping):
        raise WorkflowResultError(
            f"{workflow} returned an unsupported PaddleOCR result schema: missing json mapping"
        )
    payload = serialized.get("res")
    if not isinstance(payload, Mapping):
        raise WorkflowResultError(
            f"{workflow} returned an unsupported PaddleOCR result schema: missing json.res"
        )
    return payload


def markdown_text(result: object, *, workflow: str) -> str:
    """Return PaddleOCR's formatter output without reconstructing Markdown."""
    markdown = getattr(result, "markdown", None)
    if not isinstance(markdown, Mapping):
        raise WorkflowResultError(
            f"{workflow} returned an unsupported PaddleOCR result schema: missing markdown mapping"
        )
    text = markdown.get("markdown_texts")
    if not isinstance(text, str):
        raise WorkflowResultError(
            f"{workflow} returned an unsupported PaddleOCR result schema: missing markdown_texts"
        )
    return text.strip()


def parsing_elements(
    payload: Mapping[str, Any],
    *,
    workflow: str,
    page_index: int,
    first_index: int,
) -> list[dict[str, Any]]:
    blocks = payload.get("parsing_res_list")
    if not isinstance(blocks, Sequence) or isinstance(blocks, (str, bytes)):
        raise WorkflowResultError(
            f"{workflow} returned an unsupported PaddleOCR result schema: "
            "parsing_res_list is missing"
        )

    elements: list[dict[str, Any]] = []
    for offset, raw_block in enumerate(blocks):
        if not isinstance(raw_block, Mapping):
            raise WorkflowResultError(f"{workflow} returned a malformed parsing block")
        label = raw_block.get("block_label")
        content = raw_block.get("block_content", "")
        if not isinstance(label, str):
            raise WorkflowResultError(f"{workflow} returned a parsing block without block_label")
        if isinstance(content, Sequence) and not isinstance(content, (str, bytes)):
            content = " ".join(str(value) for value in content)
        elements.append(
            {
                "type": label,
                "bbox": json_safe(raw_block.get("block_bbox", [])),
                "content": str(content),
                "index": first_index + offset,
                "page_index": page_index,
            }
        )
    return elements


def resolve_output_directory(
    output_dir: str | Path | None,
    job_id: str | None,
    settings: Settings | None = None,
) -> Path:
    cfg = settings or get_settings()
    identifier = job_id or uuid.uuid4().hex
    return Path(output_dir) if output_dir else cfg.output_dir / identifier


def persist_document(
    *,
    output_dir: Path,
    markdown: str,
    json_value: Any,
    markdown_name: str,
    json_name: str,
    settings: Settings | None = None,
) -> dict[str, str]:
    cfg = settings or get_settings()
    markdown_path = output_dir / markdown_name
    json_path = output_dir / json_name
    atomic_write_text(markdown_path, markdown)
    atomic_write_json(json_path, json_value)
    return {
        "markdown_path": public_path(markdown_path, cfg.project_root),
        "json_path": public_path(json_path, cfg.project_root),
    }
