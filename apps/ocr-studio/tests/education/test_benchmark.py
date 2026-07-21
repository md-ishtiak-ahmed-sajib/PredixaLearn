from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pypdfium2 as pdfium

from app.core.config import get_settings
from app.education.benchmark import build_report, score_case
from app.education.demo import create_judge_demo


def test_benchmark_scores_traceability_features_timings_and_grounding():
    expected = {
        "case_id": "synthetic",
        "text": "1(a) Explain continuity.",
        "question_numbers": ["1(a)"],
        "reading_order": ["p0001-l00001"],
        "features": {"tables": 1, "figures": 1, "equations": 1},
    }
    actual = {
        "full_text": "1(a) Explain continuity.",
        "line_order": ["p0001-l00001"],
        "tables": [{"status": "accepted"}],
        "figures": [{"id": "figure-1"}],
        "latex_formulas": ["Q=A\\times V"],
        "processing_time_ms": 1234,
        "correction_time_ms": 75,
        "questions": [
            {
                "question_id": "q-1-a",
                "display_number": "1(a)",
                "source_page": 1,
                "source_line_ids": ["p0001-l00001"],
                "ai": {
                    "question_id": "q-1-a",
                    "source_page": 1,
                    "source_line_ids": ["p0001-l00001"],
                },
            }
        ],
    }
    score = score_case(expected, actual)
    assert score["table_handling_accuracy"] == 1.0
    assert score["figure_handling_accuracy"] == 1.0
    assert score["equation_handling_accuracy"] == 1.0
    assert score["source_traceability"] == 1.0
    assert score["ai_grounding_rate"] == 1.0
    assert score["processing_time_ms"] == 1234.0
    assert score["correction_time_ms"] == 75.0
    assert build_report([score])["averages"]["ai_grounding_rate"] == 1.0


def test_synthetic_judge_demo_expected_evidence_is_versioned_and_claim_free():
    fixture = Path(__file__).parents[1] / "fixtures" / "education" / "judge-demo.expected.json"
    expected = json.loads(fixture.read_text(encoding="utf-8"))
    assert expected["fixture_id"] == "predixalearn-judge-demo-v1"
    assert expected["question_numbers"] == ["1(a)", "1(b)", "2(a)", "2(b)", "3(a)", "3(b)", "4(a)", "4(b)"]
    assert expected["features"] == {"tables": 1, "figures": 2, "equations": 2}
    assert "not a claim" in expected["limitations"]


def test_judge_demo_is_an_original_two_page_pdf_ready_for_the_ocr_queue(tmp_path):
    settings = replace(get_settings(), upload_dir=tmp_path / "uploads")
    document_path = create_judge_demo(settings)
    document = pdfium.PdfDocument(str(document_path))
    assert document_path.name == "predixalearn-judge-demo.synthetic.pdf"
    assert len(document) == 2
