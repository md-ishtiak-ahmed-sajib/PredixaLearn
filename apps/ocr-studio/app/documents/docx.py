"""Deterministic local Markdown-to-DOCX conversion interfaces."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any


def pandoc_executable() -> Path:
    """Resolve only the locally packaged Pandoc executable."""
    import pypandoc

    executable = Path(pypandoc.get_pandoc_path()).resolve()
    if not executable.is_file() and executable.with_suffix(".exe").is_file():
        executable = executable.with_suffix(".exe")
    if not executable.is_file():
        raise RuntimeError("The packaged Pandoc executable is unavailable")
    return executable


def convert_markdown_to_docx(
    markdown_path: Path,
    docx_path: Path,
    *,
    source_name: str,
    settings: Any | None = None,
    table_specs: Sequence[Mapping[str, Any]] = (),
) -> list[str]:
    """Compatibility façade for the legacy document builder."""
    from app.documents._builder import convert_markdown_to_docx as convert

    return convert(
        markdown_path,
        docx_path,
        source_name=source_name,
        settings=settings,
        table_specs=table_specs,
    )


def create_reference_docx(path: Path, source_name: str) -> None:
    """Compatibility façade for the shared DOCX style builder."""
    from app.documents._builder import create_reference_docx as create

    create(path, source_name)

__all__ = [
    "convert_markdown_to_docx",
    "create_reference_docx",
    "pandoc_executable",
]
