from __future__ import annotations

import pytest

from app.documents.text_filters import (
    normalize_remove_terms,
    normalize_remove_text,
    remove_literal_from_markdown,
    remove_literal_text,
    remove_terms_from_text,
)


def test_literal_filter_is_case_insensitive_and_does_not_remove_substrings():
    cleaned, removal = remove_literal_text(
        "CamScanner\nCAMSCANNER\nCamScannerPro\nscanner",
        "camscanner",
    )

    assert cleaned == "\n\nCamScannerPro\nscanner"
    assert removal.text == "camscanner"
    assert removal.count == 2


def test_markdown_filter_protects_metadata_math_and_figure_links():
    markdown = (
        "# CamScanner report\n\n"
        "Source file: `CamScanner report.pdf`\n\n"
        "## Source page 1\n\n"
        "Scanned   with\n\n"
        "Body SCANNED WITH watermark.\n\n"
        "### Scanned with\n\n"
        "$$\n\\operatorname{Scanned with}\n$$\n\n"
        "![Scanned with chart](assets/scanned-with.png)\n"
    )

    cleaned, removal = remove_literal_from_markdown(markdown, "Scanned with")

    assert "# CamScanner report" in cleaned
    assert "Source file: `CamScanner report.pdf`" in cleaned
    assert "Body watermark." in cleaned
    assert "### \n" not in cleaned
    assert "operatorname{Scanned with}" in cleaned
    assert "![chart](assets/scanned-with.png)" in cleaned
    assert removal.count == 4


def test_remove_text_validation_is_bounded_and_rejects_control_characters():
    assert normalize_remove_text("  Scanned   with  ") == "Scanned with"
    assert normalize_remove_text("   ") is None
    with pytest.raises(ValueError, match="200 characters"):
        normalize_remove_text("x" * 201)
    with pytest.raises(ValueError, match="control character"):
        normalize_remove_text("Cam\x00Scanner")


def test_multiple_remove_terms_split_deduplicate_and_report_independent_counts():
    terms = normalize_remove_terms(
        ["CamScanner, Scanned with", "camscanner", "  University watermark; footer  "]
    )
    assert terms == ["CamScanner", "Scanned with", "University watermark", "footer"]

    cleaned, removals = remove_terms_from_text(
        "Scanned with CamScanner. CAMSCANNER footer remains useful.",
        terms,
    )
    assert "Scanned with" not in cleaned
    assert "CamScanner" not in cleaned and "CAMSCANNER" not in cleaned
    assert "footer" not in cleaned
    assert [(item.text, item.count) for item in removals] == [
        ("CamScanner", 2),
        ("Scanned with", 1),
        ("University watermark", 0),
        ("footer", 1),
    ]


def test_multiple_remove_terms_are_strictly_bounded():
    with pytest.raises(ValueError, match="10"):
        normalize_remove_terms([f"term-{index}" for index in range(11)])
    with pytest.raises(ValueError, match="100"):
        normalize_remove_terms(["x" * 101])
    with pytest.raises(ValueError, match="500"):
        normalize_remove_terms([f"{'x' * 50}{index}" for index in range(10)])
