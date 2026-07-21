"""Deterministic question segmentation over immutable OCR evidence.

This module deliberately does not use a language model.  It turns ordered OCR
lines into conservative question records that retain enough evidence for a
teacher to inspect or correct every later AI conclusion.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from typing import Any

_QUESTION_PREFIX = re.compile(
    r"^\s*(?:(?:question|q)\s*)?(?P<number>\d{1,3}(?:\s*[.(]\s*[a-zA-ZivxIVX]{1,6}\s*\))?|\d{1,3}(?:\s*[.(]\s*\d{1,3}\s*\))?|[a-zA-Z]\s*\))"
    r"\s*(?:[.:)\-–—]\s*)?",
    re.IGNORECASE,
)
_MARKS = re.compile(
    r"(?:\(\s*(?P<wrapped>\d{1,3})\s*(?:marks?|m)\s*\)|\[\s*(?P<bracket>\d{1,3})\s*\]|(?P<plain>\d{1,3})\s*(?:marks?|m)\b)",
    re.IGNORECASE,
)
_WHITESPACE = re.compile(r"\s+")


def _clean_text(value: object) -> str:
    return _WHITESPACE.sub(" ", str(value or "")).strip()


def _question_number(text: str) -> tuple[str, str] | None:
    match = _QUESTION_PREFIX.match(text)
    if not match:
        return None
    display = _clean_text(match.group("number"))
    if not display or len(display) > 20:
        return None
    remaining = text[match.end() :].strip()
    return display, remaining


def _marks(text: str) -> tuple[int | None, str]:
    match = _MARKS.search(text)
    if not match:
        return None, "not_detected"
    value = next((item for item in match.groups() if item), None)
    if value is None:
        return None, "not_detected"
    marks = int(value)
    if not 0 < marks <= 100:
        return None, "not_detected"
    return marks, "explicit"


def _source_bbox(lines: list[dict[str, Any]]) -> list[float] | None:
    boxes: list[tuple[float, float, float, float]] = []
    for line in lines:
        raw = line.get("normalized_bbox")
        if not isinstance(raw, Sequence) or isinstance(raw, (str, bytes)) or len(raw) != 4:
            continue
        try:
            left, top, right, bottom = (float(value) for value in raw)
        except (TypeError, ValueError):
            continue
        if 0 <= left <= right <= 1 and 0 <= top <= bottom <= 1:
            boxes.append((left, top, right, bottom))
    if not boxes:
        return None
    return [
        round(min(item[0] for item in boxes), 5),
        round(min(item[1] for item in boxes), 5),
        round(max(item[2] for item in boxes), 5),
        round(max(item[3] for item in boxes), 5),
    ]


def _confidence(lines: list[dict[str, Any]]) -> float | None:
    values: list[float] = []
    for line in lines:
        try:
            value = float(line.get("retry_confidence", line.get("confidence")))
        except (TypeError, ValueError):
            continue
        if 0 <= value <= 1:
            values.append(value)
    return round(sum(values) / len(values), 4) if values else None


def _record(
    *,
    identifier: str,
    display_number: str | None,
    lines: list[dict[str, Any]],
    text_parts: list[str],
    ordinal: int,
) -> dict[str, Any]:
    text = _clean_text(" ".join(text_parts))
    marks, marks_status = _marks(text)
    first_page = next(
        (
            int(line.get("page_number", int(line.get("page_index", 0)) + 1))
            for line in lines
            if isinstance(line, Mapping)
        ),
        1,
    )
    return {
        "question_id": identifier,
        "display_number": display_number or f"Unnumbered {ordinal}",
        "text": text,
        "source_page": first_page,
        "source_line_ids": [str(line["line_id"]) for line in lines if line.get("line_id")],
        "source_bbox": _source_bbox(lines),
        "ocr_confidence": _confidence(lines),
        "marks": marks,
        "marks_status": marks_status,
        "review_status": "needs_teacher_review",
        "segmentation_source": "deterministic_ocr_evidence",
    }


def segment_questions(result: Mapping[str, Any]) -> dict[str, Any]:
    """Segment source-linked questions without mutating ``result``.

    The parser intentionally favors traceability over aggressive inference: text
    before the first recognisable question is retained as a warning rather than
    silently attached to a question, and records without a number stay visible.
    """

    raw_lines = result.get("lines")
    lines = [dict(line) for line in raw_lines if isinstance(line, Mapping)] if isinstance(raw_lines, Sequence) and not isinstance(raw_lines, (str, bytes)) else []
    questions: list[dict[str, Any]] = []
    warnings: list[str] = []
    current_lines: list[dict[str, Any]] = []
    current_text: list[str] = []
    current_number: str | None = None
    current_id: str | None = None
    seen: dict[str, int] = {}
    preamble: list[str] = []

    def finish() -> None:
        nonlocal current_lines, current_text, current_number, current_id
        if not current_lines or not current_id:
            return
        record = _record(
            identifier=current_id,
            display_number=current_number,
            lines=current_lines,
            text_parts=current_text,
            ordinal=len(questions) + 1,
        )
        if not record["text"]:
            warnings.append(f"{record['display_number']} has no readable OCR text.")
        questions.append(record)
        current_lines, current_text, current_number, current_id = [], [], None, None

    for line in lines:
        text = _clean_text(line.get("text"))
        if not text:
            continue
        detected = _question_number(text)
        if detected:
            finish()
            display_number, remaining = detected
            normalized = re.sub(r"[^a-z0-9]+", "-", display_number.casefold()).strip("-")
            base_id = f"q-{normalized or len(questions) + 1}"
            seen[base_id] = seen.get(base_id, 0) + 1
            current_id = base_id if seen[base_id] == 1 else f"{base_id}-{seen[base_id]}"
            current_number = display_number
            current_lines = [line]
            current_text = [remaining or text]
            continue
        if current_id:
            current_lines.append(line)
            current_text.append(text)
        else:
            preamble.append(text)

    finish()
    if preamble:
        warnings.append("OCR text before the first detected question remains unclassified.")
    if not questions and lines:
        fallback = _record(
            identifier="q-unstructured-1",
            display_number=None,
            lines=lines,
            text_parts=[_clean_text(line.get("text")) for line in lines],
            ordinal=1,
        )
        questions.append(fallback)
        warnings.insert(0, "No reliable question numbering was detected; review the unstructured record.")
    return {
        "version": 1,
        "questions": questions,
        "segmentation_warnings": warnings,
        "preamble_text": " ".join(preamble),
    }
