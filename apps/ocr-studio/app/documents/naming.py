"""Windows-safe naming for generated document bundles."""

from __future__ import annotations

import re
import unicodedata
from pathlib import Path

_FORBIDDEN_FILENAME = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
_WINDOWS_RESERVED = {
    "CON",
    "PRN",
    "AUX",
    "NUL",
    *(f"COM{index}" for index in range(1, 10)),
    *(f"LPT{index}" for index in range(1, 10)),
}


def safe_output_stem(
    filename: str, *, fallback: str = "document", max_length: int = 120
) -> str:
    """Return a readable, Windows-safe stem without accepting path semantics."""
    leaf = Path(filename.replace("\\", "/")).name
    stem = Path(leaf).stem if Path(leaf).suffix else leaf
    stem = unicodedata.normalize("NFC", stem)
    stem = _FORBIDDEN_FILENAME.sub("_", stem).replace("..", "_")
    stem = re.sub(r"\s+", " ", stem).strip(" .")
    if not stem:
        stem = fallback
    if stem.upper() in _WINDOWS_RESERVED:
        stem = f"_{stem}"
    if len(stem) > max_length:
        stem = stem[:max_length].rstrip(" .") or fallback
    return stem

__all__ = ["safe_output_stem"]
