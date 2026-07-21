"""Quality classification and conversion checks for local OCR artifacts."""

from __future__ import annotations

import html
import re
import zipfile
from collections import Counter
from pathlib import Path
from typing import Any


def safe_docx_issue(exc: Exception) -> tuple[str, str]:
    message = str(exc).casefold()
    if "timed out" in message or "exceeded" in message:
        return (
            "docx_generation_timeout",
            "DOCX generation exceeded its safe processing time. Other OCR artifacts were retained.",
        )
    if any(term in message for term in ("markdown", "hyperlink", "external relationship")):
        return (
            "unsafe_markdown_conversion",
            "DOCX generation could not safely convert malformed Markdown. Other OCR artifacts were retained.",
        )
    return (
        "docx_generation_failed",
        "DOCX generation failed safely. Markdown, JSON, images, and tables were retained.",
    )


def docx_conversion_coverage(
    markdown: str,
    docx_path: Path,
    *,
    expected_tables: int,
    expected_images: int,
) -> dict[str, Any]:
    """Verify that Pandoc/OOXML conversion retained readable artifact content."""
    with zipfile.ZipFile(docx_path) as archive:
        document_xml = archive.read("word/document.xml").decode("utf-8", errors="replace")
        text = " ".join(
            html.unescape(re.sub(r"<[^>]+>", "", value))
            for value in re.findall(r"<w:t(?:\s[^>]*)?>(.*?)</w:t>", document_xml, re.DOTALL)
        )
        media_count = sum(name.startswith("word/media/") for name in archive.namelist())
    markdown_plain = re.sub(r"(?ms)^\$\$\s*.*?^\$\$\s*$", " ", markdown)
    markdown_plain = re.sub(r"!\[[^\]]*]\([^)]+\)", " ", markdown_plain)
    markdown_plain = re.sub(r"<[^>]+>", " ", markdown_plain)
    markdown_plain = re.sub(r"[`#>*_|$\[\]()]", " ", markdown_plain)
    source_tokens = Counter(re.findall(r"[\w]{2,}", markdown_plain.casefold()))
    docx_tokens = Counter(re.findall(r"[\w]{2,}", text.casefold()))
    total = sum(source_tokens.values())
    matched = sum(min(count, docx_tokens.get(token, 0)) for token, count in source_tokens.items())
    token_coverage = matched / total if total else 1.0
    table_count = document_xml.count("<w:tbl>")
    passed = token_coverage >= 0.95 and table_count >= expected_tables and media_count >= expected_images
    return {
        "status": "passed" if passed else "failed",
        "token_coverage_percent": round(token_coverage * 100.0, 2),
        "expected_tables": expected_tables,
        "docx_tables": table_count,
        "expected_images": expected_images,
        "docx_images": media_count,
    }
