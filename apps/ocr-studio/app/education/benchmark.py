"""Reproducible, claim-free metrics for Past Paper OCR benchmarks."""

from __future__ import annotations

import json
import platform
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any


def _distance(expected: Sequence[str], actual: Sequence[str]) -> int:
    previous = list(range(len(actual) + 1))
    for row, source in enumerate(expected, start=1):
        current = [row]
        for column, target in enumerate(actual, start=1):
            current.append(min(current[-1] + 1, previous[column] + 1, previous[column - 1] + (source != target)))
        previous = current
    return previous[-1]


def character_error_rate(expected: str, actual: str) -> float | None:
    if not expected:
        return None
    return round(_distance(list(expected), list(actual)) / len(expected), 6)


def word_error_rate(expected: str, actual: str) -> float | None:
    expected_words = expected.split()
    if not expected_words:
        return None
    return round(_distance(expected_words, actual.split()) / len(expected_words), 6)


def _accuracy(expected: Sequence[str], actual: Sequence[str]) -> float | None:
    if not expected:
        return None
    matched = sum(left == right for left, right in zip(expected, actual, strict=False))
    return round(matched / len(expected), 6)


def _number(value: object) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)) and value >= 0:
        return float(value)
    return None


def _feature_count(value: object) -> int:
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int) and value >= 0:
        return value
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        return len(value)
    return 0


def _expected_feature_count(expected: Mapping[str, Any], name: str) -> int | None:
    features = expected.get("features")
    raw = features.get(name) if isinstance(features, Mapping) else expected.get(name)
    if raw is None:
        return None
    if isinstance(raw, bool):
        return int(raw)
    if isinstance(raw, int) and raw >= 0:
        return raw
    return None


def _actual_feature_count(actual: Mapping[str, Any], name: str) -> int:
    explicit = actual.get(f"{name[:-1]}_count") if name.endswith("s") else actual.get(f"{name}_count")
    if isinstance(explicit, int) and explicit >= 0:
        return explicit
    if name == "tables":
        return _feature_count(actual.get("tables"))
    if name == "figures":
        direct = _feature_count(actual.get("figures"))
        if direct:
            return direct
        elements = actual.get("elements")
        return sum(
            str(item.get("type", "")).casefold() in {"figure", "image"}
            for item in elements
            if isinstance(item, Mapping)
        ) if isinstance(elements, Sequence) and not isinstance(elements, (str, bytes)) else 0
    if name == "equations":
        direct = _feature_count(actual.get("latex_formulas") or actual.get("equations"))
        if direct:
            return direct
        elements = actual.get("elements")
        return sum(
            str(item.get("type", "")).casefold() in {"equation", "formula"}
            for item in elements
            if isinstance(item, Mapping)
        ) if isinstance(elements, Sequence) and not isinstance(elements, (str, bytes)) else 0
    return 0


def _handling_accuracy(expected: Mapping[str, Any], actual: Mapping[str, Any], name: str) -> float | None:
    expected_count = _expected_feature_count(expected, name)
    if expected_count is None:
        return None
    actual_count = _actual_feature_count(actual, name)
    if expected_count == 0:
        return 1.0 if actual_count == 0 else 0.0
    return round(min(actual_count, expected_count) / expected_count, 6)


def _timing(actual: Mapping[str, Any], name: str) -> float | None:
    candidates = [
        actual.get(name),
        actual.get("timings", {}).get(name) if isinstance(actual.get("timings"), Mapping) else None,
        actual.get("metrics", {}).get(name) if isinstance(actual.get("metrics"), Mapping) else None,
    ]
    return next((number for item in candidates if (number := _number(item)) is not None), None)


def _ai_grounding_rate(actual: Mapping[str, Any]) -> float | None:
    questions = actual.get("questions")
    if not isinstance(questions, Sequence) or isinstance(questions, (str, bytes)):
        return None
    reviewed = 0
    grounded = 0
    for question in questions:
        if not isinstance(question, Mapping) or not isinstance(question.get("ai"), Mapping):
            continue
        reviewed += 1
        ai = question["ai"]
        source_lines = {str(value) for value in question.get("source_line_ids", [])}
        ai_lines = {str(value) for value in ai.get("source_line_ids", [])}
        if (
            ai_lines
            and ai_lines.issubset(source_lines)
            and ai.get("source_page") == question.get("source_page")
            and ai.get("question_id") == question.get("question_id")
        ):
            grounded += 1
    return round(grounded / reviewed, 6) if reviewed else None


def score_case(expected: Mapping[str, Any], actual: Mapping[str, Any]) -> dict[str, Any]:
    """Score a fixture with only metrics whose ground truth is provided."""

    expected_text = str(expected.get("text", ""))
    actual_text = str(actual.get("full_text", actual.get("markdown", "")))
    expected_questions = [str(value) for value in expected.get("question_numbers", [])]
    actual_questions = [str(item.get("display_number")) for item in actual.get("questions", []) if isinstance(item, Mapping)]
    expected_order = [str(value) for value in expected.get("reading_order", [])]
    actual_order = [str(value) for value in actual.get("line_order", [])]
    return {
        "case_id": str(expected.get("case_id", "unnamed")),
        "character_error_rate": character_error_rate(expected_text, actual_text) if expected_text else None,
        "word_error_rate": word_error_rate(expected_text, actual_text) if expected_text else None,
        "question_number_accuracy": _accuracy(expected_questions, actual_questions) if expected_questions else None,
        "reading_order_accuracy": _accuracy(expected_order, actual_order) if expected_order else None,
        "table_handling_accuracy": _handling_accuracy(expected, actual, "tables"),
        "figure_handling_accuracy": _handling_accuracy(expected, actual, "figures"),
        "equation_handling_accuracy": _handling_accuracy(expected, actual, "equations"),
        "source_traceability": round(
            sum(bool(item.get("source_page") and item.get("source_line_ids")) for item in actual.get("questions", []) if isinstance(item, Mapping))
            / max(1, len(actual.get("questions", []))),
            6,
        ),
        "processing_time_ms": _timing(actual, "processing_time_ms") or _timing(actual, "processing_ms"),
        "correction_time_ms": _timing(actual, "correction_time_ms"),
        "ai_grounding_rate": _ai_grounding_rate(actual),
    }


def build_report(cases: Sequence[dict[str, Any]], *, metadata: Mapping[str, Any] | None = None) -> dict[str, Any]:
    numeric_fields = [
        "character_error_rate",
        "word_error_rate",
        "question_number_accuracy",
        "reading_order_accuracy",
        "table_handling_accuracy",
        "figure_handling_accuracy",
        "equation_handling_accuracy",
        "source_traceability",
        "processing_time_ms",
        "correction_time_ms",
        "ai_grounding_rate",
    ]
    averages = {
        field: round(sum(float(item[field]) for item in cases if _number(item.get(field)) is not None) / count, 6)
        if (count := sum(_number(item.get(field)) is not None for item in cases))
        else None
        for field in numeric_fields
    }
    return {
        "version": 1,
        "metadata": {
            "python": sys.version.split()[0],
            "platform": platform.platform(),
            **dict(metadata or {}),
        },
        "case_count": len(cases),
        "averages": averages,
        "cases": list(cases),
        "limitations": [
            "Synthetic fixtures are regression tests, not evidence of real-world OCR performance.",
            "Publish real-world metrics only with permission-cleared source papers and manual ground truth.",
        ],
    }


def run_manifest(manifest_path: Path, actual_directory: Path) -> dict[str, Any]:
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    cases = manifest.get("cases", []) if isinstance(manifest, Mapping) else []
    if not isinstance(cases, list):
        raise ValueError("Benchmark manifest cases must be a list")
    scores = []
    for expected in cases:
        if not isinstance(expected, Mapping):
            raise ValueError("Benchmark case must be an object")
        case_id = str(expected.get("case_id", ""))
        if not case_id:
            raise ValueError("Benchmark case is missing case_id")
        actual_path = actual_directory / f"{case_id}.json"
        actual = json.loads(actual_path.read_text(encoding="utf-8"))
        if not isinstance(actual, Mapping):
            raise ValueError(f"Benchmark result {case_id} is not an object")
        scores.append(score_case(expected, actual))
    return build_report(scores, metadata=manifest.get("metadata") if isinstance(manifest, Mapping) else None)
