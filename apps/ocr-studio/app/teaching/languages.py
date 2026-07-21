"""English-only product capability declaration with future extension hooks."""

from __future__ import annotations

from copy import deepcopy
from typing import Any

_REGISTRY: dict[str, dict[str, Any]] = {
    "en": {
        "label": "English",
        "ocr": "supported",
        "question_segmentation": "supported",
        "marks_extraction": "supported",
        "taxonomy_mapping": "supported",
        "duplicate_normalization": "supported",
        "analysis": "supported_with_consent",
        "export_fonts": "supported",
        "right_to_left": False,
    },
}


def language_reliability(language: str | None) -> dict[str, Any]:
    code = (language or "en").strip().casefold()
    profile = deepcopy(
        _REGISTRY.get(
            code,
            {
                "label": code or "Unknown",
                "ocr": "unavailable",
                "question_segmentation": "unavailable",
                "marks_extraction": "unavailable",
                "taxonomy_mapping": "unavailable",
                "duplicate_normalization": "unavailable",
                "analysis": "unavailable",
                "export_fonts": "unavailable",
                "right_to_left": False,
            },
        )
    )
    profile["language"] = code
    profile["analysis_reason"] = (
        None
        if profile["analysis"] == "supported_with_consent"
        else "The current PredixaLearn release accepts English only. Other languages can be implemented in a future release after language-specific validation."
    )
    return profile


def reliability_registry() -> list[dict[str, Any]]:
    return [language_reliability(code) for code in sorted(_REGISTRY)]
