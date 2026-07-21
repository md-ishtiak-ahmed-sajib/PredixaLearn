"""Structured document construction and artifact publishing."""

from app.documents.corrections import ConservativeCorrector, Correction
from app.documents.docx import convert_markdown_to_docx, create_reference_docx, pandoc_executable
from app.documents.markdown import (
    build_structured_markdown,
    suppress_adjacent_duplicates,
)
from app.documents.naming import safe_output_stem

__all__ = [
    "ConservativeCorrector",
    "Correction",
    "build_structured_markdown",
    "convert_markdown_to_docx",
    "create_reference_docx",
    "pandoc_executable",
    "safe_output_stem",
    "suppress_adjacent_duplicates",
]
