from __future__ import annotations

from app.education.questions import segment_questions


def test_question_segmentation_preserves_source_evidence_and_marks():
    result = {
        "lines": [
            {
                "line_id": "p0001-l00001",
                "page_number": 1,
                "text": "1(a) Explain flow continuity. (5 marks)",
                "confidence": 0.98,
                "normalized_bbox": [0.1, 0.1, 0.8, 0.16],
            },
            {
                "line_id": "p0001-l00002",
                "page_number": 1,
                "text": "Use one real example.",
                "confidence": 0.92,
                "normalized_bbox": [0.1, 0.18, 0.75, 0.23],
            },
            {
                "line_id": "p0002-l00001",
                "page_number": 2,
                "text": "2. Calculate discharge. [6]",
                "confidence": 0.88,
                "normalized_bbox": [0.12, 0.12, 0.77, 0.18],
            },
        ]
    }
    segmented = segment_questions(result)
    first, second = segmented["questions"]
    assert [item["display_number"] for item in segmented["questions"]] == ["1(a)", "2"]
    assert first["source_line_ids"] == ["p0001-l00001", "p0001-l00002"]
    assert first["marks"] == 5 and first["marks_status"] == "explicit"
    assert first["source_bbox"] == [0.1, 0.1, 0.8, 0.23]
    assert second["source_page"] == 2 and second["marks"] == 6


def test_question_segmentation_keeps_unstructured_ocr_visible_for_review():
    segmented = segment_questions(
        {"lines": [{"line_id": "p0001-l00001", "text": "Damaged OCR without a number", "page_number": 1}]}
    )
    assert segmented["questions"][0]["question_id"] == "q-unstructured-1"
    assert "No reliable question numbering" in segmented["segmentation_warnings"][0]
