"""Page-aligned Markdown construction interfaces."""

from app.documents._builder import (
    _bbox_values,
    build_structured_markdown,
    suppress_adjacent_duplicates,
)

__all__ = [
    "_bbox_values",
    "build_structured_markdown",
    "suppress_adjacent_duplicates",
]
