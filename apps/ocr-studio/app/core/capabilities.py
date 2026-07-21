"""Version-aware OCR languages and document profile validation."""

from __future__ import annotations

from collections.abc import Iterable

DOCUMENT_PROFILES = ("auto", "general", "exam")
ENABLED_PRODUCT_LANGUAGES = frozenset({"en"})

# The lists intentionally follow PaddleOCR's public per-version language table.  Keeping the
# validation local prevents a typo from triggering an unexpected model download at job time.
_LATIN_LANGUAGES = {
    "af",
    "az",
    "bs",
    "ca",
    "cs",
    "cy",
    "da",
    "de",
    "en",
    "es",
    "et",
    "eu",
    "fi",
    "fr",
    "ga",
    "gl",
    "hr",
    "hu",
    "id",
    "is",
    "it",
    "ku",
    "la",
    "lb",
    "lt",
    "lv",
    "mi",
    "ms",
    "mt",
    "nl",
    "no",
    "oc",
    "pl",
    "pt",
    "qu",
    "rm",
    "ro",
    "rs_latin",
    "sk",
    "sl",
    "sq",
    "sv",
    "sw",
    "tl",
    "tr",
    "uz",
    "vi",
}
_V6_LANGUAGES = _LATIN_LANGUAGES | {"ch", "chinese_cht", "japan", "french", "german"}
_V5_EXTRA_LANGUAGES = {
    "ab",
    "ady",
    "ar",
    "av",
    "ba",
    "be",
    "bg",
    "bgc",
    "bh",
    "bho",
    "bua",
    "cv",
    "el",
    "fa",
    "gom",
    "hi",
    "japan",
    "kaa",
    "kbd",
    "kk",
    "korean",
    "ky",
    "mah",
    "mai",
    "mhr",
    "mk",
    "mn",
    "mo",
    "mr",
    "ne",
    "new",
    "os",
    "pi",
    "ps",
    "ru",
    "sa",
    "sah",
    "sck",
    "sd",
    "sr",
    "ta",
    "te",
    "tg",
    "th",
    "tt",
    "tyv",
    "udm",
    "ug",
    "uk",
    "ur",
    "xal",
}
_V3_LANGUAGES = (_V6_LANGUAGES | _V5_EXTRA_LANGUAGES) - {
    "ca",
    "eu",
    "fi",
    "gl",
    "lb",
    "qu",
    "rm",
    "th",
    "el",
}
OCR_LANGUAGES: dict[str, frozenset[str]] = {
    "PP-OCRv3": frozenset(_V3_LANGUAGES),
    "PP-OCRv4": frozenset({"ch", "en"}),
    "PP-OCRv5": frozenset(_V6_LANGUAGES | _V5_EXTRA_LANGUAGES),
    "PP-OCRv6": frozenset(_V6_LANGUAGES),
}

_LANGUAGE_LABELS = {
    "en": "English",
    "ch": "Chinese (Simplified)",
    "chinese_cht": "Chinese (Traditional)",
    "japan": "Japanese",
    "korean": "Korean",
    "fr": "French",
    "french": "French (legacy model)",
    "de": "German",
    "german": "German (legacy model)",
    "es": "Spanish",
    "pt": "Portuguese",
    "it": "Italian",
    "ru": "Russian",
    "ar": "Arabic",
    "hi": "Hindi",
    "ur": "Urdu",
}


def supported_languages(
    ocr_version: str,
    structure_version: str | None = None,
) -> tuple[str, ...]:
    """Return languages supported by every pipeline participating in a workflow."""
    supported = set(OCR_LANGUAGES.get(ocr_version, ()))
    if structure_version:
        supported.intersection_update(OCR_LANGUAGES.get(structure_version, ()))
    # The vendor catalogs above are retained as an implementation reference for
    # a future multilingual release. The current product intentionally exposes
    # and accepts English only so OCR, analysis, exports, and support behavior
    # have one tested language contract.
    supported.intersection_update(ENABLED_PRODUCT_LANGUAGES)
    return tuple(sorted(supported, key=lambda value: (value != "en", value)))


def validate_language(
    language: str | None,
    *,
    ocr_version: str,
    structure_version: str | None = None,
    default: str = "en",
) -> str:
    value = (language or default).strip().lower()
    allowed = supported_languages(ocr_version, structure_version)
    if value not in allowed:
        raise ValueError(
            f"Language {value!r} is not supported; this PredixaLearn release "
            "accepts English ('en') only"
        )
    return value


def validate_document_profile(profile: str | None) -> str:
    value = (profile or "auto").strip().lower()
    if value not in DOCUMENT_PROFILES:
        raise ValueError("Document profile must be auto, general, or exam")
    return value


def language_capabilities(languages: Iterable[str]) -> list[dict[str, str]]:
    return [
        {"code": code, "label": _LANGUAGE_LABELS.get(code, code.replace("_", " ").title())}
        for code in languages
    ]
