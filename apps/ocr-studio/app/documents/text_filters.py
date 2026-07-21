"""Safe user-requested removal of literal OCR text from readable exports."""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass

MAX_REMOVE_TEXT_LENGTH = 200
MAX_REMOVE_TERMS = 10
MAX_REMOVE_TERM_LENGTH = 100
MAX_REMOVE_TERMS_TOTAL_LENGTH = 500


def normalize_remove_text(value: str | None) -> str | None:
    """Validate and normalize one optional word or phrase supplied by the user."""
    if value is None:
        return None
    if len(value) > MAX_REMOVE_TEXT_LENGTH:
        raise ValueError(
            f"Text to remove must be {MAX_REMOVE_TEXT_LENGTH} characters or fewer"
        )
    if any(
        unicodedata.category(character) == "Cc" and character not in "\t\r\n"
        for character in value
    ):
        raise ValueError("Text to remove contains an unsupported control character")
    normalized = " ".join(value.split())
    return normalized or None


def normalize_remove_terms(values: list[str] | tuple[str, ...] | None) -> list[str]:
    """Validate, split, and deduplicate the new multi-term removal field."""
    if not values:
        return []
    candidates: list[str] = []
    for value in values:
        if any(
            unicodedata.category(character) == "Cc" and character not in "\t\r\n"
            for character in value
        ):
            raise ValueError("Text to remove contains an unsupported control character")
        candidates.extend(re.split(r"[,;\r\n]+", value))
    normalized: list[str] = []
    seen: set[str] = set()
    for candidate in candidates:
        term = " ".join(candidate.split())
        if not term:
            continue
        if len(term) > MAX_REMOVE_TERM_LENGTH:
            raise ValueError(
                f"Each removal term must be {MAX_REMOVE_TERM_LENGTH} characters or fewer"
            )
        key = term.casefold()
        if key not in seen:
            seen.add(key)
            normalized.append(term)
    if len(normalized) > MAX_REMOVE_TERMS:
        raise ValueError(f"At most {MAX_REMOVE_TERMS} removal terms are allowed")
    if sum(len(term) for term in normalized) > MAX_REMOVE_TERMS_TOTAL_LENGTH:
        raise ValueError(
            f"Removal terms must total {MAX_REMOVE_TERMS_TOTAL_LENGTH} characters or fewer"
        )
    return normalized


def _literal_pattern(query: str) -> re.Pattern[str]:
    tokens = query.split()
    expression = r"\s+".join(re.escape(token) for token in tokens)
    if tokens[0][0].isalnum() or tokens[0][0] == "_":
        expression = rf"(?<!\w){expression}"
    if tokens[-1][-1].isalnum() or tokens[-1][-1] == "_":
        expression = rf"{expression}(?!\w)"
    return re.compile(expression, re.IGNORECASE)


def _tidy_removed_line(value: str) -> str:
    """Remove whitespace or punctuation left by a deleted standalone watermark."""
    line_ending = "\n" if value.endswith("\n") else ""
    body = value[:-1] if line_ending else value
    leading = re.match(r"[ \t]*", body).group(0)
    content = re.sub(r"[ \t]{2,}", " ", body[len(leading) :])
    content = re.sub(r"[ \t]+([,.;:!?])", r"\1", content)
    content = re.sub(r"([([{])\s+", r"\1", content)
    body = leading + content
    if re.fullmatch(
        r"\s*(?:#{1,6}|>|[-*+]|\d+[.)])?\s*[.,;:!?-]*\s*",
        body,
    ):
        body = ""
    return body.rstrip() + line_ending


@dataclass(frozen=True, slots=True)
class TextRemoval:
    text: str
    count: int


def remove_literal_text(value: str, query: str) -> tuple[str, TextRemoval]:
    """Remove a case-insensitive literal word/phrase without substring matching."""
    normalized = normalize_remove_text(query)
    if normalized is None:
        return value, TextRemoval(text="", count=0)
    pattern = _literal_pattern(normalized)
    updated, count = pattern.subn("", value)
    return updated, TextRemoval(text=normalized, count=count)


def remove_literal_from_markdown(markdown: str, query: str) -> tuple[str, TextRemoval]:
    """Remove literal prose while protecting document metadata and display mathematics."""
    normalized = normalize_remove_text(query)
    if normalized is None:
        return markdown, TextRemoval(text="", count=0)
    pattern = _literal_pattern(normalized)
    in_display_math = False
    count = 0
    output: list[str] = []
    for line_number, line in enumerate(markdown.splitlines(keepends=True)):
        stripped = line.strip()
        if stripped == "$$":
            in_display_math = not in_display_math
            output.append(line)
            continue
        if stripped.startswith("!["):
            line_ending = "\r\n" if line.endswith("\r\n") else "\n" if line.endswith("\n") else ""
            body = line[: -len(line_ending)] if line_ending else line
            image = re.match(r"^(\s*!\[)(.*?)(\]\([^)]+\).*)$", body)
            if image:
                alt, matches = pattern.subn("", image.group(2))
                count += matches
                output.append(
                    f"{image.group(1)}{alt.strip()}{image.group(3)}{line_ending}"
                )
                continue
        protected = (
            in_display_math
            or line_number == 0
            or stripped.startswith("Source file:")
            or stripped.startswith("## Source page ")
        )
        if protected:
            output.append(line)
            continue
        updated, matches = pattern.subn("", line)
        count += matches
        output.append(_tidy_removed_line(updated) if matches else line)
    cleaned = "".join(output)
    cleaned = re.sub(r"\n{4,}", "\n\n\n", cleaned)
    return cleaned, TextRemoval(text=normalized, count=count)


def remove_terms_from_text(value: str, terms: list[str]) -> tuple[str, list[TextRemoval]]:
    updated = value
    removals: list[TextRemoval] = []
    for term in terms:
        updated, removal = remove_literal_text(updated, term)
        removals.append(removal)
    return updated, removals


def remove_terms_from_markdown(
    markdown: str,
    terms: list[str],
) -> tuple[str, list[TextRemoval]]:
    updated = markdown
    removals: list[TextRemoval] = []
    for term in terms:
        updated, removal = remove_literal_from_markdown(updated, term)
        removals.append(removal)
    return updated, removals
