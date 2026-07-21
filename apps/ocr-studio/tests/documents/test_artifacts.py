from __future__ import annotations

import copy
import json
import zipfile
from dataclasses import replace

import numpy as np
import pytest
from PIL import Image

from app.core.config import get_settings
from app.documents import (
    ConservativeCorrector,
    build_structured_markdown,
    safe_output_stem,
)
from app.documents.artifacts import (
    _figure_quality,
    _remap_retry_table,
    build_and_publish_artifacts,
    extract_figure_assets,
)
from app.documents.libreoffice import DocumentValidation
from app.workflows.table_extract import (
    _cells_to_grid,
    normalize_table_cells,
    verify_table_against_lines,
)


def artifact_settings(tmp_path):
    return replace(
        get_settings(),
        output_dir=tmp_path / "output",
        upload_dir=tmp_path / "uploads",
        history_dir=tmp_path / "app" / "history",
        history_db_path=tmp_path / "app" / "history" / "history.sqlite3",
        log_dir=tmp_path / "logs",
        max_embedded_images=10,
        max_embedded_image_bytes=10 * 1024 * 1024,
    )


def test_windows_safe_output_stem_preserves_readable_unicode():
    assert safe_output_stem(r"folder\CE 501 (2024-2021).pdf") == "CE 501 (2024-2021)"
    assert safe_output_stem("../../বাংলা:notes?.png") == "বাংলা_notes_"
    assert safe_output_stem("CON.pdf") == "_CON"
    assert not safe_output_stem("trailing... .pdf").endswith((".", " "))


def test_conservative_correction_protects_content_and_logs_changes():
    corrector = ConservativeCorrector(enabled=True)
    source = "This sentnce has spacing ,and x=E=mc2 at https://example.com. CE501 東京"
    corrected, audit = corrector.correct(source, page=2, block=7)

    assert "sentence" in corrected
    assert "spacing, and" in corrected
    assert "https://example.com" in corrected
    assert "CE501" in corrected and "東京" in corrected
    assert corrected.split().index("This") < corrected.split().index("sentence")
    assert audit is not None
    assert audit.page == 2 and audit.block == 7


def test_conservative_correction_never_changes_plain_text_equations():
    corrector = ConservativeCorrector(enabled=True)
    original = "Equation: x =(-b + sqrt(b^2 - 4ac)) /(2a)"

    corrected, audit = corrector.correct(original, page=1, block=2)

    assert corrected == original
    assert audit is None


def test_conservative_correction_explains_whitespace_trimming():
    corrector = ConservativeCorrector(enabled=True)

    corrected, audit = corrector.correct("Heading ", page=1, block=0)

    assert corrected == "Heading"
    assert audit is not None
    assert audit.reason == ["leading/trailing whitespace normalization"]


def test_guarded_word_boundaries_and_grammar_review_do_not_rewrite_valid_terms():
    corrector = ConservativeCorrector(enabled=True)

    corrected, audit = corrector.correct(
        "Write shortnotesonthefollowing items using breakpoint and sqrt at "
        "https://example.com CE501.",
        page=1,
        block=4,
    )

    assert corrected == (
        "Write short notes on the following items using breakpoint and sqrt at "
        "https://example.com CE501."
    )
    assert audit is not None and audit.kind == "word_boundary"
    assert audit.confidence >= 0.95
    assert corrector.review_suggestions == []

    grammar, grammar_audit = corrector.correct(
        "Write a short not on Fire hydrants.",
        page=1,
        block=5,
    )
    assert grammar == "Write a short not on Fire hydrants."
    assert grammar_audit is None
    assert any(item["kind"] == "grammar" for item in corrector.review_suggestions)


def test_short_misspelling_never_uses_destructive_word_segmentation():
    corrector = ConservativeCorrector(enabled=True)

    corrected, audit = corrector.correct(
        "The populaton increased rapidly.",
        page=1,
        block=9,
    )

    assert "pop ul at on" not in corrected.casefold()
    assert corrected.split()[0:1] == ["The"]
    if audit is not None:
        assert audit.kind != "word_boundary"


def test_markdown_keeps_pages_equations_tables_and_raw_word_order():
    result = {
        "page_count": 2,
        "elements": [
            {
                "type": "text",
                "bbox": [1, 1, 100, 20],
                "content": "First page text.",
                "index": 0,
                "page_index": 0,
            },
            {
                "type": "formula",
                "bbox": [1, 25, 100, 50],
                "content": r"x = \frac{-b}{2a}",
                "index": 1,
                "page_index": 0,
            },
            {
                "type": "table",
                "bbox": [1, 55, 100, 90],
                "content": "<table><tr><th colspan='2'>Header</th></tr>"
                "<tr><td>A</td><td>B</td></tr></table>",
                "index": 2,
                "page_index": 0,
            },
            {
                "type": "text",
                "bbox": [1, 1, 100, 20],
                "content": "Second page text.",
                "index": 3,
                "page_index": 1,
            },
        ],
    }

    markdown, corrections, reviews, warnings, tables = build_structured_markdown(
        result,
        workflow="layout_parsing",
        source_name="source.pdf",
        output_stem="source",
        figure_assets={},
    )

    assert "## Source page 1" in markdown and "## Source page 2" in markdown
    assert r"\newpage" not in markdown
    assert r"\frac{-b}{2a}" in markdown
    assert "[[OCR_TABLE_001]]" not in markdown
    assert '<th colspan="2">Header</th>' in markdown
    assert tables[0]["cells"][0]["col_span"] == 2
    assert corrections == [] and reviews == [] and warnings == []


@pytest.mark.parametrize(
    "formula, expected",
    [
        (r"$$x = \frac{-b \pm \sqrt{b^2-4ac}}{2a}$$", r"x = \frac"),
        (
            r"\[\begin{aligned}a &= b \\ c &= d\end{aligned}\]",
            r"\begin{aligned}",
        ),
        (r"\(E = mc^2\)", "E = mc^2"),
    ],
)
def test_valid_formula_wrappers_are_normalized_once(formula, expected):
    result = {
        "page_count": 1,
        "elements": [
            {
                "type": "formula",
                "bbox": [0, 0, 100, 20],
                "content": formula,
                "index": 3,
                "page_index": 0,
            }
        ],
    }

    markdown, corrections, reviews, warnings, _ = build_structured_markdown(
        result,
        workflow="layout_parsing",
        source_name="formula.pdf",
        output_stem="formula",
        figure_assets={},
    )

    assert markdown.splitlines().count("$$") == 2
    assert expected in markdown
    assert corrections == [] and reviews == [] and warnings == []


def test_malformed_ce504_formula_falls_back_without_consuming_next_page():
    formula = "x01.59×4}$$Q_{'{$D4.7\nH_L=A\nQ=B E-L\nD=D\nH{=C=EA\nF=00"
    result = {
        "page_count": 2,
        "elements": [
            {
                "type": "formula",
                "bbox": [0, 0, 100, 80],
                "content": formula,
                "index": 12,
                "page_index": 0,
            },
            {
                "type": "text",
                "bbox": [0, 0, 100, 20],
                "content": "University of Dhaka",
                "index": 13,
                "page_index": 1,
            },
        ],
    }
    original = copy.deepcopy(result)

    markdown, corrections, reviews, warnings, _ = build_structured_markdown(
        result,
        workflow="layout_parsing",
        source_name="CE 504.pdf",
        output_stem="CE 504",
        figure_assets={},
    )

    assert r"\newpage" not in markdown
    assert markdown.splitlines().count("$$") == 0
    assert "Formula could not be safely rendered" in markdown
    assert "    x01.59×4}$$Q_{'{$D4.7" in markdown
    assert "\n## Source page 2\n" in markdown
    assert markdown.index("## Source page 2") < markdown.index("University of Dhaka")
    assert corrections == []
    assert len(reviews) == 1 and reviews[0]["kind"] == "formula_rendering"
    assert reviews[0]["page"] == 1 and reviews[0]["block"] == 12
    assert warnings and "kept as literal text" in warnings[0]
    assert result["elements"] == original["elements"]
    assert result["document_profile"] == "general"


@pytest.mark.parametrize(
    "formula, reason_fragment",
    [
        (r"x_{1", "unmatched"),
        (r"\begin{aligned}x &= 1\end{matrix}", "mismatched"),
        (r"\newpage x", "unsafe command"),
        (r"\unsupported{<tag>}", "unsupported command"),
        (r"x $$ y", "embedded math delimiter"),
    ],
)
def test_unsafe_formulas_are_visible_escaped_and_flagged(formula, reason_fragment):
    result = {
        "page_count": 1,
        "elements": [
            {
                "type": "formula",
                "content": formula,
                "index": 8,
                "page_index": 0,
            }
        ],
    }

    markdown, _, reviews, warnings, _ = build_structured_markdown(
        result,
        workflow="layout_parsing",
        source_name="unsafe.pdf",
        output_stem="unsafe",
        figure_assets={},
    )

    assert markdown.splitlines().count("$$") == 0
    assert "Formula could not be safely rendered" in markdown
    assert reason_fragment in reviews[0]["reason"]
    assert reason_fragment in warnings[0]
    if "<tag>" in formula:
        assert "&lt;tag&gt;" in markdown and "<tag>" not in markdown


def test_figure_crop_uses_source_colors_and_invalid_box_fallback(tmp_path):
    settings = artifact_settings(tmp_path)
    source = np.zeros((100, 120, 3), dtype=np.uint8)
    source[:, :] = [10, 20, 200]
    image_path = tmp_path / "colors.png"
    Image.fromarray(source[:, :, ::-1], mode="RGB").save(image_path)
    result = {
        "elements": [
            {
                "type": "figure",
                "bbox": [10, 10, 70, 70],
                "content": "",
                "index": 1,
                "page_index": 0,
            },
            {
                "type": "chart",
                "bbox": [50, 50, 20, 20],
                "content": "",
                "index": 2,
                "page_index": 0,
            },
        ]
    }

    assets, supplemental, tables, warnings, retries = extract_figure_assets(
        image_path,
        result,
        workflow="layout_parsing",
        assets_dir=tmp_path / "bundle" / "assets",
        settings=settings,
        progress_callback=None,
    )

    assert supplemental == []
    assert tables == [] and retries == 0
    assert len(assets) == 2
    first = Image.open(tmp_path / "bundle" / assets["0:1"])
    assert first.getpixel((0, 0)) == (200, 20, 10)
    first.close()
    assert any("full page" in warning for warning in warnings)


def test_tiny_checkmark_is_rejected_as_a_false_figure():
    page = np.full((1000, 800, 3), 255, dtype=np.uint8)
    page[20:30, 20:30] = 0

    accepted, reason = _figure_quality(
        {"type": "figure", "bbox": [18, 18, 32, 32]},
        page,
        (18, 18, 32, 32),
    )

    assert accepted is False
    assert "too small" in str(reason)


def test_skewed_shuffled_ce504_cells_are_restored_to_visual_grid():
    first_row = ["1970", "1980", "1990", "Year", "1950", "1960"]
    second_row = ["32", "37", "43", "Population (Thousand)", "25", "28"]
    visual_columns = {
        "Year": 0,
        "1950": 1,
        "1960": 2,
        "1970": 3,
        "1980": 4,
        "1990": 5,
        "Population (Thousand)": 0,
        "25": 1,
        "28": 2,
        "32": 3,
        "37": 4,
        "43": 5,
    }
    cells = []
    for row, values in enumerate((first_row, second_row)):
        for column, value in enumerate(values):
            visual_column = visual_columns[value]
            x1 = 20 + visual_column * 100
            y1 = 100 + row * 44 + round(0.08 * x1)
            cells.append(
                {
                    "row": row,
                    "col": column,
                    "row_span": 1,
                    "col_span": 1,
                    "text": value,
                    "bbox": [x1, y1, x1 + 80, y1 + 30],
                }
            )

    normalized, changed = normalize_table_cells(cells)

    assert changed is True
    assert _cells_to_grid(normalized) == [
        ["Year", "1950", "1960", "1970", "1980", "1990"],
        ["Population (Thousand)", "25", "28", "32", "37", "43"],
    ]


def test_rotated_retry_cell_boxes_map_back_to_source_page_geometry():
    table = {
        "bbox": [10, 140, 30, 180],
        "cells": [{"bbox": [10, 140, 30, 180]}],
    }

    _remap_retry_table(
        table,
        left=40,
        top=60,
        scale=2.0,
        crop_width=100,
        crop_height=50,
        quarter_turns=1,
    )

    assert table["bbox"] == [50.0, 65.0, 70.0, 75.0]
    assert table["cells"][0]["bbox"] == [50.0, 65.0, 70.0, 75.0]


def test_text_markdown_uses_primary_lines_and_public_structured_table_only():
    cells = [
        {
            "row": row,
            "col": column,
            "row_span": 1,
            "col_span": 1,
            "text": value,
            "bbox": [column * 100, 100 + row * 40, (column + 1) * 100, 140 + row * 40],
        }
        for row, values in enumerate(
            (
                ("Year", "1950.", "1960", "1970", "1980", ".1990"),
                ("Population (Thousand)", "25", "28", "32", "37", "43 。"),
            )
        )
        for column, value in enumerate(values)
    ]
    result = {
        "page_count": 1,
        "lines": [
            {
                "text": "Calculate the probable population.",
                "confidence": 0.98,
                "bbox": [0, 20, 600, 50],
                "page_index": 0,
            },
            {
                "text": "Year",
                "confidence": 0.99,
                "bbox": [0, 100, 100, 140],
                "page_index": 0,
            },
            {
                "text": "25",
                "confidence": 0.99,
                "bbox": [100, 140, 200, 180],
                "page_index": 0,
            },
            {
                "text": "Write shortnotesonthefollowing items.",
                "confidence": 0.97,
                "bbox": [0, 210, 600, 240],
                "page_index": 0,
            },
        ],
        "tables": [
            {
                "table_index": 0,
                "page_index": 0,
                "bbox": [0, 90, 600, 190],
                "cells": cells,
                "status": "accepted",
                "source_line_ids": ["p0001-l00002", "p0001-l00003"],
            }
        ],
    }

    markdown, corrections, reviews, warnings, table_specs = build_structured_markdown(
        result,
        workflow="text_recognition",
        source_name="CE 504.pdf",
        output_stem="CE 504",
        figure_assets={},
        supplemental_elements=[
            {
                "type": "text",
                "bbox": [0, 205, 600, 245],
                "content": "Writeshortnotesonthefollowingbadduplicate",
                "index": 99,
                "page_index": 0,
            }
        ],
    )

    assert "Calculate the probable population." in markdown
    assert "Writeshortnotesonthefollowingbadduplicate" not in markdown
    assert "| Year | 1950 | 1960 | 1970 | 1980 | 1990 |" in markdown
    assert "| Population (Thousand) | 25 | 28 | 32 | 37 | 43 |" in markdown
    assert "short notes on the following" in markdown
    assert markdown.count("\nYear\n") == 0
    assert "[[OCR_TABLE_" not in markdown
    assert any(item["kind"] == "word_boundary" for item in corrections)
    assert sum(item["kind"] == "table_numeric_punctuation" for item in corrections) == 3
    assert reviews == [] and warnings == [] and len(table_specs) == 1
    assert result["content_coverage"]["status"] == "passed"
    assert result["content_coverage"]["unaccounted_line_count"] == 0


def test_unverified_table_and_false_figure_overlap_cannot_delete_primary_text():
    result = {
        "page_count": 1,
        "lines": [
            {
                "line_id": "p0001-l00001",
                "text": "Question text crossing an uncertain region.",
                "confidence": 0.98,
                "bbox": [10, 20, 580, 55],
                "normalized_bbox": [0.02, 0.04, 0.97, 0.11],
                "page_index": 0,
            },
            {
                "line_id": "p0001-l00002",
                "text": "25",
                "confidence": 0.99,
                "bbox": [20, 70, 60, 95],
                "normalized_bbox": [0.03, 0.14, 0.10, 0.19],
                "page_index": 0,
            },
        ],
        "tables": [
            {
                "table_index": 0,
                "page_index": 0,
                "bbox": [0, 0, 600, 120],
                "normalized_bbox": [0, 0, 1, 0.24],
                "cells": [
                    {
                        "row": 0,
                        "col": 0,
                        "row_span": 1,
                        "col_span": 1,
                        "text": "Wrong table text",
                    }
                ],
                "status": "needs_review",
                "source_line_ids": ["p0001-l00001", "p0001-l00002"],
            }
        ],
    }

    markdown, _, _, _, table_specs = build_structured_markdown(
        result,
        workflow="text_recognition",
        source_name="lossless.pdf",
        output_stem="lossless",
        figure_assets={},
        supplemental_elements=[
            {
                "type": "figure",
                "index": 99,
                "page_index": 0,
                "bbox": [0, 0, 600, 100],
                "normalized_bbox": [0, 0, 1, 0.2],
                "figure_status": "rejected",
            }
        ],
    )

    assert "Question text crossing an uncertain region." in markdown
    assert "25" in markdown
    assert "Wrong table text" not in markdown
    assert table_specs == []
    assert result["content_coverage"]["coverage_percent"] == 100.0
    assert set(result["content_coverage"]["line_dispositions"].values()) == {"prose"}


def test_only_safe_structured_formula_replaces_overlapping_primary_ocr():
    result = {
        "page_count": 2,
        "lines": [
            {
                "line_id": "p0001-l00001",
                "text": "x = (-b + sqrt(b^2 - 4ac)) / 2a",
                "confidence": 0.91,
                "bbox": [10, 20, 400, 50],
                "normalized_bbox": [0.02, 0.10, 0.80, 0.25],
                "page_index": 0,
            },
            {
                "line_id": "p0001-l00002",
                "text": "unsafe formula OCR remains visible",
                "confidence": 0.80,
                "bbox": [10, 70, 400, 100],
                "normalized_bbox": [0.02, 0.35, 0.80, 0.50],
                "page_index": 0,
            },
            {
                "line_id": "p0002-l00001",
                "text": "same coordinates on another page stay visible",
                "confidence": 0.99,
                "bbox": [10, 20, 400, 50],
                "normalized_bbox": [0.02, 0.10, 0.80, 0.25],
                "page_index": 1,
            },
        ],
    }

    markdown, _, reviews, _, _ = build_structured_markdown(
        result,
        workflow="text_recognition",
        source_name="formula.pdf",
        output_stem="formula",
        figure_assets={},
        supplemental_elements=[
            {
                "type": "formula",
                "content": r"x = \frac{-b + \sqrt{b^2-4ac}}{2a}",
                "bbox": [10, 20, 400, 50],
                "normalized_bbox": [0.02, 0.10, 0.80, 0.25],
                "page_index": 0,
                "index": 50,
            },
            {
                "type": "formula",
                "content": r"\unsupported{broken",
                "bbox": [10, 70, 400, 100],
                "normalized_bbox": [0.02, 0.35, 0.80, 0.50],
                "page_index": 0,
                "index": 51,
            },
        ],
    )

    assert markdown.count(r"\frac{-b + \sqrt{b^2-4ac}}{2a}") == 1
    assert "unsafe formula OCR remains visible" in markdown
    assert result["content_coverage"]["line_dispositions"] == {
        "p0001-l00001": "verified_duplicate",
        "p0001-l00002": "prose",
        "p0002-l00001": "prose",
    }
    assert result["structured_formulas"][0]["source_line_ids"] == ["p0001-l00001"]
    assert any(review["kind"] == "formula_rendering" for review in reviews)


def test_normalized_figure_overlap_never_consumes_text_from_another_page():
    result = {
        "page_count": 2,
        "lines": [
            {
                "line_id": "p0001-l00001",
                "text": "A diagram label",
                "confidence": 0.99,
                "normalized_bbox": [0.1, 0.1, 0.8, 0.3],
                "page_index": 0,
            },
            {
                "line_id": "p0002-l00001",
                "text": "Unrelated prose on source page two.",
                "confidence": 0.99,
                "normalized_bbox": [0.1, 0.1, 0.8, 0.3],
                "page_index": 1,
            },
        ],
    }
    figure = {
        "type": "figure",
        "index": 7,
        "page_index": 0,
        "normalized_bbox": [0.0, 0.0, 0.9, 0.4],
        "figure_status": "accepted",
    }

    markdown, _, _, _, _ = build_structured_markdown(
        result,
        workflow="text_recognition",
        source_name="pages.pdf",
        output_stem="pages",
        figure_assets={"0:7": "assets/page-001-image-001.png"},
        supplemental_elements=[figure],
    )

    assert "Recognized figure labels:** A diagram label" in markdown
    assert "Unrelated prose on source page two." in markdown
    assert result["content_coverage"]["line_dispositions"] == {
        "p0001-l00001": "figure_label",
        "p0002-l00001": "prose",
    }


def test_auto_exam_profile_attaches_marks_but_keeps_page_number_visible():
    result = {
        "page_count": 1,
        "requested_document_profile": "auto",
        "lines": [
            {
                "line_id": "p0001-l00001",
                "text": "Subject Code: CE 504",
                "confidence": 0.99,
                "normalized_bbox": [0.05, 0.05, 0.45, 0.08],
                "page_index": 0,
            },
            {
                "line_id": "p0001-l00002",
                "text": "Full Marks: 70",
                "confidence": 0.99,
                "normalized_bbox": [0.60, 0.05, 0.82, 0.08],
                "page_index": 0,
            },
            {
                "line_id": "p0001-l00003",
                "text": "Answer Any Five of the Following Questions",
                "confidence": 0.99,
                "normalized_bbox": [0.15, 0.14, 0.78, 0.18],
                "page_index": 0,
            },
            {
                "line_id": "p0001-l00004",
                "text": "1. (a) Explain the water supply system.",
                "confidence": 0.98,
                "normalized_bbox": [0.06, 0.30, 0.78, 0.34],
                "page_index": 0,
            },
            {
                "line_id": "p0001-l00005",
                "text": "5",
                "confidence": 0.99,
                "normalized_bbox": [0.90, 0.30, 0.94, 0.34],
                "page_index": 0,
            },
            {
                "line_id": "p0001-l00006",
                "text": "2",
                "confidence": 0.99,
                "normalized_bbox": [0.90, 0.97, 0.94, 0.99],
                "page_index": 0,
            },
        ],
    }

    markdown, _, _, _, _ = build_structured_markdown(
        result,
        workflow="text_recognition",
        source_name="exam.pdf",
        output_stem="exam",
        figure_assets={},
    )

    assert result["document_profile"] == "exam"
    assert "**[05 marks]**" in markdown
    assert "\n2\n" in markdown
    assert result["content_coverage"]["unaccounted_line_count"] == 0


def test_repeated_header_consensus_is_audited_without_reordering_text():
    lines = []
    values = [
        ("University of Diala", 0.62),
        ("University of Dhaka", 0.99),
        ("University of Dhaka", 0.98),
        ("University of Dhaka", 0.97),
    ]
    for page_index, (text, confidence) in enumerate(values):
        lines.append(
            {
                "line_id": f"p{page_index + 1:04d}-l00001",
                "text": text,
                "confidence": confidence,
                "normalized_bbox": [0.25, 0.04, 0.75, 0.08],
                "page_index": page_index,
            }
        )
    result = {"page_count": 4, "lines": lines}

    markdown, corrections, _, _, _ = build_structured_markdown(
        result,
        workflow="text_recognition",
        source_name="headers.pdf",
        output_stem="headers",
        figure_assets={},
    )

    assert "University of Diala" not in markdown
    assert markdown.count("University of Dhaka") == 4
    consensus = [item for item in corrections if item["kind"] == "cross_page_consensus"]
    assert consensus == [
        {
            "original": "University of Diala",
            "corrected": "University of Dhaka",
            "page": 1,
            "block": 0,
            "kind": "cross_page_consensus",
            "confidence": 0.99,
            "reason": ["repeated high-confidence header text on other source pages"],
        }
    ]


def test_table_requires_primary_source_coverage_even_with_perfect_geometry():
    table = {
        "bbox": [0, 0, 1, 1],
        "normalized_bbox": [0, 0, 1, 1],
        "bbox_identity_score": 1.0,
        "geometry_normalized": True,
        "cells": [
            {
                "row": 0,
                "col": 0,
                "row_span": 1,
                "col_span": 1,
                "text": "Year",
                "normalized_bbox": [0, 0, 0.5, 0.5],
            },
            {
                "row": 0,
                "col": 1,
                "row_span": 1,
                "col_span": 1,
                "text": "1950",
                "normalized_bbox": [0.5, 0, 1, 0.5],
            },
            {
                "row": 1,
                "col": 0,
                "row_span": 1,
                "col_span": 1,
                "text": "Population",
                "normalized_bbox": [0, 0.5, 0.5, 1],
            },
            {
                "row": 1,
                "col": 1,
                "row_span": 1,
                "col_span": 1,
                "text": "25",
                "normalized_bbox": [0.5, 0.5, 1, 1],
            },
        ],
    }
    incomplete_primary = [
        {
            "line_id": "p0001-l00001",
            "text": "Year",
            "confidence": 0.99,
            "normalized_bbox": [0.02, 0.05, 0.45, 0.40],
        },
        {
            "line_id": "p0001-l00002",
            "text": "1691538",
            "confidence": 0.99,
            "normalized_bbox": [0.55, 0.55, 0.95, 0.90],
        },
    ]

    verify_table_against_lines(
        table,
        incomplete_primary,
        quality_threshold=0.85,
    )

    assert table["status"] == "needs_review"
    assert table["quality_components"]["source_coverage"] == 1.0
    assert table["quality_components"]["table_token_coverage"] < 0.90


def test_table_coverage_excludes_nearby_prose_outside_cell_geometry():
    table = {
        "bbox": [0, 0, 1, 1],
        "normalized_bbox": [0, 0, 1, 1],
        "bbox_identity_score": 1.0,
        "geometry_normalized": True,
        "cell_html_correspondence": 1.0,
        "cells": [
            {
                "row": 0,
                "col": 0,
                "row_span": 1,
                "col_span": 1,
                "text": "Year",
                "normalized_bbox": [0, 0.25, 0.45, 0.55],
            },
            {
                "row": 0,
                "col": 1,
                "row_span": 1,
                "col_span": 1,
                "text": "1950",
                "normalized_bbox": [0.55, 0.25, 1, 0.55],
            },
        ],
    }
    lines = [
        {
            "line_id": "p0001-l00001",
            "text": "2000 and 2010 by least square parabola methods.",
            "confidence": 0.99,
            "normalized_bbox": [0.05, 0.02, 0.95, 0.22],
        },
        {
            "line_id": "p0001-l00002",
            "text": "Year",
            "confidence": 0.99,
            "normalized_bbox": [0.05, 0.30, 0.40, 0.50],
        },
        {
            "line_id": "p0001-l00003",
            "text": "1950",
            "confidence": 0.99,
            "normalized_bbox": [0.60, 0.30, 0.95, 0.50],
        },
    ]

    verify_table_against_lines(table, lines, quality_threshold=0.85)

    assert table["status"] == "accepted"
    assert table["source_line_ids"] == ["p0001-l00002", "p0001-l00003"]
    assert table["quality_components"]["source_coverage"] == 1.0


def test_complete_bundle_has_native_math_tables_media_and_independent_history(
    tmp_path, monkeypatch
):
    settings = artifact_settings(tmp_path)
    monkeypatch.setattr(
        "app.documents.artifacts.validate_docx_with_libreoffice",
        lambda *args, **kwargs: DocumentValidation(
            status="passed",
            renderer="libreoffice",
            version="26.2.4.2",
            rendered_pages=2,
            duration_seconds=0.25,
        ),
    )
    input_path = tmp_path / "Modern report.tiff"
    image = np.zeros((120, 160, 3), dtype=np.uint8)
    image[20:70, 20:100] = [220, 120, 30]
    first_frame = Image.fromarray(image[:, :, ::-1], mode="RGB")
    second_frame = Image.new("RGB", (160, 120), "white")
    first_frame.save(input_path, save_all=True, append_images=[second_frame])
    result = {
        "page_count": 2,
        "elements": [
            {
                "type": "doc_title",
                "bbox": [5, 2, 150, 18],
                "content": "Modern report",
                "index": 0,
                "page_index": 0,
            },
            {
                "type": "figure",
                "bbox": [20, 20, 100, 70],
                "content": "",
                "index": 1,
                "page_index": 0,
            },
            {
                "type": "formula",
                "bbox": [5, 75, 150, 90],
                "content": r"x = \frac{-b \pm \sqrt{b^2-4ac}}{2a}",
                "index": 2,
                "page_index": 0,
            },
            {
                "type": "table",
                "bbox": [5, 92, 150, 118],
                "content": "<table><tr><th colspan='2'>Header</th></tr>"
                "<tr><td>Alpha</td><td>42</td></tr></table>",
                "index": 3,
                "page_index": 0,
            },
            {
                "type": "text",
                "bbox": [5, 5, 150, 25],
                "content": "Second source page.",
                "index": 4,
                "page_index": 1,
            },
            {
                "type": "formula",
                "bbox": [5, 30, 150, 45],
                "content": r"\unsupported{unsafe}",
                "index": 5,
                "page_index": 1,
            },
        ],
    }
    job_id = "a" * 32

    outcome = build_and_publish_artifacts(
        input_path,
        input_path.name,
        "layout_parsing",
        result,
        job_id=job_id,
        settings=settings,
    )

    assert outcome.result["artifact_status"] == "complete"
    assert outcome.result["docx_conversion_coverage"]["status"] == "passed"
    assert outcome.result["document_validation"] == {
        "status": "passed",
        "renderer": "libreoffice",
        "version": "26.2.4.2",
        "rendered_pages": 2,
        "duration_seconds": 0.25,
        "warning": None,
    }
    assert outcome.result["review_count"] == 1
    assert outcome.result["review_suggestions"][0]["kind"] == "formula_rendering"
    assert any("unsupported command" in item for item in outcome.result["warnings"])
    output_root = settings.output_dir / "Modern report"
    docx = output_root / "Modern report.docx"
    assert docx.is_file() and (output_root / "Modern report.md").is_file()
    assert outcome.archive_saved is True
    archived = settings.history_dir / "jobs" / job_id / "artifacts" / "Modern report"
    assert (archived / "Modern report.docx").is_file()
    manifest = json.loads(
        (settings.history_dir / "jobs" / job_id / "manifest.json").read_text(encoding="utf-8")
    )
    assert manifest["files"]["docx"] == "Modern report.docx"

    with zipfile.ZipFile(docx) as archive:
        document_xml = archive.read("word/document.xml")
        styles_xml = archive.read("word/styles.xml")
        assert b"<m:oMath" in document_xml
        assert document_xml.count(b"<m:oMath>") == 1
        assert b"Formula could not be safely rendered" in document_xml
        assert b"unsupported" in document_xml
        assert b"<w:tbl>" in document_xml
        assert b"w:gridSpan" in document_xml
        assert b"w:pageBreakBefore" in document_xml
        assert b" descr=" in document_xml and b"Content figure from source page 1" in document_xml
        assert b"OCR_TABLE" not in document_xml
        assert b"newpage" not in document_xml
        assert any(name.startswith("word/media/") for name in archive.namelist())
        relationships = b"".join(
            archive.read(name) for name in archive.namelist() if name.endswith(".rels")
        )
        assert b'TargetMode="External"' not in relationships
        assert b"Carlito" in styles_xml


def test_libreoffice_failure_warns_without_losing_successful_artifacts(tmp_path, monkeypatch):
    settings = artifact_settings(tmp_path)
    input_path = tmp_path / "validation warning.png"
    Image.new("RGB", (80, 60), "white").save(input_path)
    monkeypatch.setattr(
        "app.documents.artifacts.validate_docx_with_libreoffice",
        lambda *args, **kwargs: DocumentValidation(
            status="failed",
            renderer="libreoffice",
            version=None,
            rendered_pages=None,
            duration_seconds=0.1,
            warning="Project-local LibreOffice is unavailable",
        ),
    )

    outcome = build_and_publish_artifacts(
        input_path,
        input_path.name,
        "layout_parsing",
        {
            "page_count": 1,
            "elements": [
                {
                    "type": "text",
                    "content": "OCR remains successful.",
                    "index": 0,
                    "page_index": 0,
                }
            ],
        },
        job_id="c" * 32,
        settings=settings,
    )

    output_root = settings.output_dir / "validation warning"
    archive_root = (
        settings.history_dir
        / "jobs"
        / ("c" * 32)
        / "artifacts"
        / "validation warning"
    )
    assert outcome.result["artifact_status"] == "complete"
    assert outcome.result["document_validation"]["status"] == "failed"
    assert "Project-local LibreOffice is unavailable" in outcome.result["warnings"]
    assert (output_root / "validation warning.docx").is_file()
    assert (archive_root / "validation warning.docx").is_file()
    assert not list(settings.output_dir.rglob(".libreoffice-*"))


def test_user_requested_text_is_removed_from_exports_but_raw_ocr_is_preserved(
    tmp_path,
    monkeypatch,
):
    settings = artifact_settings(tmp_path)
    input_path = tmp_path / "watermarked.png"
    Image.new("RGB", (100, 60), "white").save(input_path)
    monkeypatch.setattr(
        "app.documents.artifacts.validate_docx_with_libreoffice",
        lambda *args, **kwargs: DocumentValidation(
            status="passed",
            renderer="libreoffice",
            version="26.2.4.2",
            rendered_pages=1,
            duration_seconds=0.1,
        ),
    )
    raw_result = {
        "page_count": 1,
        "full_text": "Useful text.\nScanned with\nCamScanner",
        "elements": [
            {
                "type": "text",
                "content": "Useful text.",
                "index": 0,
                "page_index": 0,
            },
            {
                "type": "text",
                "content": "SCANNED WITH",
                "index": 1,
                "page_index": 0,
            },
            {
                "type": "text",
                "content": "CamScanner",
                "index": 2,
                "page_index": 0,
            },
        ],
    }

    outcome = build_and_publish_artifacts(
        input_path,
        input_path.name,
        "layout_parsing",
        raw_result,
        job_id="d" * 32,
        settings=settings,
        remove_terms=["Scanned with", "CamScanner"],
    )

    assert outcome.result["removed_text_count"] == 2
    assert outcome.result["removed_terms"] == [
        {"term": "Scanned with", "removed_count": 1},
        {"term": "CamScanner", "removed_count": 1},
    ]
    assert "scanned with" not in outcome.result["markdown"].casefold()
    assert "camscanner" not in outcome.result["markdown"].casefold()
    assert "Scanned with" not in outcome.result["full_text"]
    assert "CamScanner" not in outcome.result["full_text"]
    assert outcome.result["raw_full_text"] == raw_result["full_text"]
    assert outcome.result["elements"][1]["content"] == "SCANNED WITH"
    output_json = json.loads(
        (settings.output_dir / "watermarked" / "watermarked.json").read_text(
            encoding="utf-8"
        )
    )
    assert output_json["raw_full_text"] == raw_result["full_text"]
    assert output_json["elements"][1]["content"] == "SCANNED WITH"
    with zipfile.ZipFile(settings.output_dir / "watermarked" / "watermarked.docx") as archive:
        document_xml = archive.read("word/document.xml").decode("utf-8").casefold()
    assert "scanned with" not in document_xml
    assert "camscanner" not in document_xml


def test_failed_replacement_keeps_previous_named_export(tmp_path, monkeypatch):
    settings = artifact_settings(tmp_path)
    input_path = tmp_path / "same.png"
    Image.new("RGB", (20, 20), "white").save(input_path)
    target = settings.output_dir / "same"
    target.mkdir(parents=True)
    (target / "sentinel.txt").write_text("previous", encoding="utf-8")
    with zipfile.ZipFile(target / "same.docx", "w") as archive:
        archive.writestr("[Content_Types].xml", "<Types />")
    (target / "same.json").write_text(
        '{"artifact_status":"complete"}', encoding="utf-8"
    )

    monkeypatch.setattr(
        "app.documents.artifacts.convert_markdown_to_docx",
        lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("conversion failed")),
    )
    outcome = build_and_publish_artifacts(
        input_path,
        input_path.name,
        "layout_parsing",
        {"elements": [], "page_count": 1},
        job_id="b" * 32,
        settings=settings,
    )

    assert outcome.result["artifact_status"] == "partial"
    assert outcome.result["docx_generation"]["status"] == "failed"
    assert outcome.result["document_validation"]["status"] == "not_run"
    assert outcome.result["named_output_published"] is False
    assert outcome.archive_saved is True
    assert (target / "sentinel.txt").read_text(encoding="utf-8") == "previous"
    assert not (settings.output_dir / ".staging" / ("b" * 32)).exists()
