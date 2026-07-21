"""Small, auditable safety primitives shared by document builders."""

from __future__ import annotations

import html


def escape_markdown_text(text: str) -> str:
    """Render untrusted OCR as literal Markdown without changing visible text."""
    escaped = html.escape(text, quote=False)
    # Numeric entities prevent unmatched OCR brackets from becoming links while
    # remaining readable to Pandoc's DOCX renderer.
    return (
        escaped.replace("\\", "&#92;")
        .replace("`", "&#96;")
        .replace("*", "&#42;")
        .replace("_", "&#95;")
        .replace("[", "&#91;")
        .replace("]", "&#93;")
    )
