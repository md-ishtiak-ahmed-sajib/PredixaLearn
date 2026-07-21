from __future__ import annotations

from contextlib import contextmanager
from dataclasses import replace

import numpy as np
import pytest
from openpyxl import load_workbook
from PIL import Image

from app.core.config import get_settings
from app.core.errors import InputLimitError, WorkflowResultError
from app.core.serialization import json_safe
from app.workflows import layout_parse, ocr_text, table_extract, vl_process
from app.workflows.pdf_utils import RenderedPage
from app.workflows.result_adapters import result_payload


class FakeResult(dict):
    def __init__(self, payload, *, markdown=None, direct=None):
        super().__init__(direct or {})
        self._payload = payload
        self._markdown = markdown

    @property
    def json(self):
        return {"res": self._payload}

    @property
    def markdown(self):
        return {"markdown_texts": self._markdown}


class FakeEngine:
    def __init__(self, results):
        self.results = results
        self.inputs = []

    def predict(self, value):
        self.inputs.append(value)
        return self.results


class FakeManager:
    def __init__(self, engine):
        self.engine = engine
        self.sessions = []

    @contextmanager
    def session(self, name, **kwargs):
        self.sessions.append((name, kwargs))
        yield self.engine


@pytest.fixture
def no_cache_clear(monkeypatch):
    for module in (ocr_text, layout_parse, table_extract, vl_process):
        monkeypatch.setattr(module, "clear_gpu_cache", lambda: None)
    monkeypatch.setattr(ocr_text, "_adaptive_retry_page", lambda *args, **kwargs: 0)


def test_json_safe_normalizes_numpy_values():
    value = {"score": np.float32(0.75), "box": np.array([1, 2, 3, 4])}
    assert json_safe(value) == {"score": 0.75, "box": [1, 2, 3, 4]}


def test_strict_result_adapter_rejects_schema_change():
    class ChangedResult:
        json = {"result": {}}

    with pytest.raises(WorkflowResultError, match="missing json.res"):
        result_payload(ChangedResult(), workflow="test")


def test_text_uses_json_res_and_native_batch(monkeypatch, no_cache_clear):
    results = [
        FakeResult(
            {
                "rec_texts": ["one"],
                "rec_scores": np.array([0.98765]),
                "rec_boxes": np.array([[1, 2, 30, 40]]),
                "rec_polys": [],
            }
        ),
        FakeResult(
            {
                "rec_texts": ["two"],
                "rec_scores": [0.5],
                "rec_boxes": [[5, 6, 7, 8]],
                "rec_polys": [],
            }
        ),
    ]
    engine = FakeEngine(results)
    monkeypatch.setattr(ocr_text, "get_manager", lambda: FakeManager(engine))

    output = ocr_text.run_text_recognition(["a.png", "b.png"])

    assert engine.inputs == [["a.png", "b.png"]]
    assert output[0]["lines"][0] == {
        "text": "one",
        "confidence": 0.9877,
        "bbox": [1, 2, 30, 40],
        "page_index": 0,
        "page_number": 1,
        "line_id": "p0001-l00001",
    }
    assert output[1]["full_text"] == "two"
    assert output[1]["pages"][0] == {
        "page_index": 0,
        "page_number": 1,
        "line_start": 0,
        "line_end": 1,
        "full_text": "two",
        "width": None,
        "height": None,
        "render_scale": None,
        "page_retry": None,
    }
    assert output[1]["language"] == "en"
    assert output[1]["requested_document_profile"] == "auto"


def test_pdf_text_keeps_original_page_numbers(monkeypatch, no_cache_clear):
    first = FakeResult(
        {
            "rec_texts": ["page one"],
            "rec_scores": [0.9],
            "rec_boxes": [[1, 1, 20, 10]],
            "rec_polys": [],
        }
    )
    second = FakeResult(
        {
            "rec_texts": ["page two", "second line"],
            "rec_scores": [0.8, 0.7],
            "rec_boxes": [[1, 1, 20, 10], [1, 12, 20, 20]],
            "rec_polys": [],
        }
    )

    class PageEngine:
        def __init__(self):
            self.results = iter(([first], [second]))
            self.inputs = []

        def predict(self, image):
            self.inputs.append(image)
            return next(self.results)

    engine = PageEngine()
    rendered_pages = [
        RenderedPage(0, 2, np.zeros((10, 10, 3), dtype=np.uint8)),
        RenderedPage(1, 2, np.ones((10, 10, 3), dtype=np.uint8)),
    ]
    monkeypatch.setattr(ocr_text, "is_pdf", lambda path: True)
    monkeypatch.setattr(
        ocr_text,
        "iter_pdf_pages",
        lambda path, settings, progress_callback=None: rendered_pages,
    )
    monkeypatch.setattr(ocr_text, "get_manager", lambda: FakeManager(engine))
    monkeypatch.setattr(
        ocr_text,
        "_page_quality",
        lambda *args, **kwargs: (1.0, []),
    )

    events = []
    output = ocr_text.run_text_recognition(
        "document.pdf",
        progress_callback=lambda **event: events.append(event),
    )

    assert output["page_count"] == 2
    assert [line["page_number"] for line in output["lines"]] == [1, 2, 2]
    assert output["pages"] == [
        {
            "page_index": 0,
            "page_number": 1,
                "line_start": 0,
                "line_end": 1,
                "full_text": "page one",
                "width": 10,
                "height": 10,
                "render_scale": 1.0,
                "page_retry": None,
            },
        {
            "page_index": 1,
            "page_number": 2,
            "line_start": 1,
                "line_end": 3,
                "full_text": "page two\nsecond line",
                "width": 10,
                "height": 10,
                "render_scale": 1.0,
                "page_retry": None,
            },
    ]
    assert all(image.dtype == np.uint8 and image.flags.c_contiguous for image in engine.inputs)
    assert [event["completed_pages"] for event in events if event["stage"] == "page_complete"] == [
        1,
        2,
    ]
    assert events[-1] == {
        "stage": "finalizing",
        "message": "Assembling the text result",
        "current_page": None,
        "total_pages": 2,
        "completed_pages": 2,
    }


def test_adaptive_text_retry_preserves_raw_text_and_adds_better_display(monkeypatch):
    settings = get_settings()
    retry_settings = replace(
        settings,
        adaptive_retry_enabled=True,
        adaptive_max_scale=2.0,
        low_confidence_threshold=0.90,
        max_retry_regions_per_page=8,
        max_image_pixels=25_000_000,
    )
    monkeypatch.setattr(ocr_text, "get_settings", lambda: retry_settings)
    retry = FakeResult(
        {
            "rec_texts": ["short notes on the following"],
            "rec_scores": [0.93],
            "rec_boxes": [[0, 0, 200, 30]],
            "rec_polys": [],
        }
    )
    engine = FakeEngine([retry])
    line = {
        "text": "shortnotesonthefollowing",
        "confidence": 0.42,
        "bbox": [10, 10, 260, 40],
        "page_index": 0,
        "page_number": 1,
    }

    count = ocr_text._adaptive_retry_page(
        engine,
        np.zeros((80, 300, 3), dtype=np.uint8),
        [line],
        page_index=0,
        progress_callback=None,
    )

    assert count == 1
    assert line["text"] == "shortnotesonthefollowing"
    assert line["confidence"] == 0.42
    assert line["retry_text"] == "short notes on the following"
    assert line["retry_confidence"] == 0.93
    assert isinstance(engine.inputs[0], list) and len(engine.inputs[0]) == 1


def test_full_page_retry_rejects_semantically_unrelated_high_confidence_text():
    original = [
        {"text": "Calculate probable population by least square method.", "confidence": 0.62}
    ]
    unrelated = [
        {"text": "Completely different invoice shipping address.", "confidence": 0.99}
    ]
    restored = [
        {"text": "Calculate probable population by least square method.", "confidence": 0.91}
    ]

    assert not ocr_text._full_page_retry_is_better(
        original,
        unrelated,
        original_score=0.55,
        candidate_score=0.95,
    )
    assert ocr_text._full_page_retry_is_better(
        original,
        restored,
        original_score=0.55,
        candidate_score=0.82,
    )


def test_layout_uses_upstream_markdown_and_structured_blocks(monkeypatch, tmp_path, no_cache_clear):
    result = FakeResult(
        {
            "parsing_res_list": [
                {
                    "block_label": "doc_title",
                    "block_content": "Actual document title",
                    "block_bbox": np.array([1, 2, 100, 20]),
                },
                {
                    "block_label": "text",
                    "block_content": "Body content",
                    "block_bbox": [1, 30, 100, 60],
                },
            ],
            "table_res_list": [],
        },
        markdown="# Actual document title\n\nBody content",
    )
    engine = FakeEngine([result])
    monkeypatch.setattr(layout_parse, "get_manager", lambda: FakeManager(engine))
    image_path = tmp_path / "page.png"
    Image.new("RGB", (120, 80), "white").save(image_path)

    output = layout_parse.run_layout_parsing(image_path, output_dir=tmp_path)

    assert output["markdown"].startswith("# Actual document title")
    assert [item["content"] for item in output["elements"]] == [
        "Actual document title",
        "Body content",
    ]
    assert output["elements"][0]["page_index"] == 0
    assert (tmp_path / "document.md").read_text(encoding="utf-8") == output["markdown"]
    assert (tmp_path / "elements.json").is_file()


def test_vl_formulas_come_from_structured_blocks(monkeypatch, tmp_path, no_cache_clear):
    result = FakeResult(
        {
            "parsing_res_list": [
                {
                    "block_label": "formula",
                    "block_content": r"x = \frac{-b}{2a}",
                    "block_bbox": [0, 0, 100, 40],
                },
                {
                    "block_label": "text",
                    "block_content": "$not-a-structured-formula$",
                    "block_bbox": [0, 50, 100, 90],
                },
            ]
        },
        markdown=r"$$x = \frac{-b}{2a}$$\n\n$not-a-structured-formula$",
    )
    monkeypatch.setattr(vl_process, "get_manager", lambda: FakeManager(FakeEngine([result])))
    image_path = tmp_path / "page.png"
    Image.new("RGB", (120, 100), "white").save(image_path)

    output = vl_process.run_vl_processing(image_path, output_dir=tmp_path)

    assert output["latex_formulas"] == [r"x = \frac{-b}{2a}"]
    assert (tmp_path / "document_vl.md").is_file()
    assert (tmp_path / "elements_vl.json").is_file()


def test_table_spans_exports_and_formula_injection(monkeypatch, tmp_path, no_cache_clear):
    child = FakeResult(
        {
            "pred_html": (
                "<table><tr><th colspan='2'>Header</th></tr>"
                "<tr><td>=2+2</td><td>Safe</td></tr></table>"
            ),
            "cell_box_list": [[0, 0, 100, 20], [0, 20, 50, 40], [50, 20, 100, 40]],
        }
    )
    parent = FakeResult(
        {
            "parsing_res_list": [
                {
                    "block_label": "table",
                    "block_content": "",
                    "block_bbox": [0, 0, 100, 40],
                }
            ],
            "table_res_list": [{"pred_html": "serialized"}],
        },
        direct={"table_res_list": [child]},
    )
    monkeypatch.setattr(table_extract, "get_manager", lambda: FakeManager(FakeEngine([parent])))
    monkeypatch.setattr(
        ocr_text,
        "run_text_recognition",
        lambda *args, **kwargs: {
            "lines": [
                {
                    "line_id": "p0001-l00001",
                    "page_index": 0,
                    "text": "Header",
                    "confidence": 0.99,
                    "bbox": [0, 0, 100, 20],
                    "normalized_bbox": [0, 0, 1, 0.5],
                },
                {
                    "line_id": "p0001-l00002",
                    "page_index": 0,
                    "text": "=2+2",
                    "confidence": 0.99,
                    "bbox": [0, 20, 50, 40],
                    "normalized_bbox": [0, 0.5, 0.5, 1],
                },
                {
                    "line_id": "p0001-l00003",
                    "page_index": 0,
                    "text": "Safe",
                    "confidence": 0.99,
                    "bbox": [50, 20, 100, 40],
                    "normalized_bbox": [0.5, 0.5, 1, 1],
                },
            ],
            "full_text": "Header\n=2+2\nSafe",
        },
    )

    image_path = tmp_path / "table.png"
    Image.new("RGB", (100, 40), "white").save(image_path)
    output = table_extract.run_table_extraction(image_path, output_dir=tmp_path)

    assert output["table_count"] == 1
    table = output["tables"][0]
    assert table["page_index"] == 0
    assert table["cells"][0]["col_span"] == 2
    workbook = load_workbook(tmp_path / "table_0.xlsx", data_only=False)
    sheet = workbook.active
    assert str(sheet.merged_cells) == "A1:B1"
    assert sheet["A2"].value == "'=2+2"
    assert sheet["A2"].data_type == "s"
    workbook.close()
    assert "'=2+2" in (tmp_path / "table_0.csv").read_text(encoding="utf-8")


def test_table_span_expansion_is_bounded():
    with pytest.raises(InputLimitError):
        table_extract._parse_html_cells(
            "<table><tr><td rowspan='1000' colspan='1000'>x</td></tr></table>",
            max_cells=100,
        )


def test_multiple_tables_match_parsing_blocks_by_bbox_not_list_position():
    left_table = FakeResult(
        {
            "pred_html": "<table><tr><td>LEFT</td></tr></table>",
            "cell_box_list": [[10, 100, 110, 140]],
        }
    )
    right_table = FakeResult(
        {
            "pred_html": "<table><tr><td>RIGHT</td></tr></table>",
            "cell_box_list": [[300, 500, 420, 540]],
        }
    )
    prediction = FakeResult(
        {
            # Deliberately reverse parsing-block order relative to table_res_list.
            "parsing_res_list": [
                {
                    "block_label": "table",
                    "block_bbox": [290, 490, 430, 550],
                    "block_content": "",
                },
                {
                    "block_label": "table",
                    "block_bbox": [0, 90, 120, 150],
                    "block_content": "",
                },
            ],
            "table_res_list": [{"pred_html": "left"}, {"pred_html": "right"}],
        },
        direct={"table_res_list": [left_table, right_table]},
    )

    tables = table_extract.extract_tables_from_prediction(
        prediction,
        page_index=6,
        first_table_index=3,
    )

    assert tables[0]["cells"][0]["text"] == "LEFT"
    assert tables[0]["bbox"] == [0, 90, 120, 150]
    assert tables[1]["cells"][0]["text"] == "RIGHT"
    assert tables[1]["bbox"] == [290, 490, 430, 550]
    assert all(table["bbox_match"] == "spatial" for table in tables)


def test_table_extraction_accepts_a_page_without_detected_tables():
    prediction = FakeResult({"parsing_res_list": []})

    assert table_extract.extract_tables_from_prediction(
        prediction,
        page_index=1,
        first_table_index=0,
    ) == []


def test_mismatched_html_and_cell_boxes_use_conservative_visual_geometry():
    live_table = FakeResult(
        {
            "pred_html": (
                "<table><tr><td>Year</td><td>1950</td></tr>"
                "<tr><td>Population</td></tr></table>"
            ),
            "cell_box_list": [
                [10, 10, 90, 30],
                [100, 10, 150, 30],
                [10, 35, 90, 55],
                [100, 35, 150, 55],
            ],
        }
    )
    prediction = FakeResult(
        {
            "parsing_res_list": [
                {"block_label": "table", "block_bbox": [5, 5, 155, 60]}
            ],
            "table_res_list": [{"pred_html": "serialized"}],
        },
        direct={"table_res_list": [live_table]},
    )

    tables = table_extract.extract_tables_from_prediction(
        prediction,
        page_index=6,
        first_table_index=0,
    )

    assert len(tables) == 1
    assert len(tables[0]["cells"]) == 4
    assert tables[0]["geometry_normalized"] is True
    assert tables[0]["cell_html_correspondence"] == 0.75
    assert "visual geometry" in tables[0]["structure_warning"]


def test_mismatched_rotated_html_recovers_a_skewed_two_by_eight_grid():
    boxes = []
    for column in range(8):
        left = 20 + column * 90
        top = 80 - column * 3
        boxes.extend(
            [
                [left, top, left + 86, top + 28],
                [left, top + 30, left + 86, top + 70],
            ]
        )
    html_cells = [
        {"row": 0, "col": index, "row_span": 1, "col_span": 1, "text": str(index)}
        for index in range(17)
    ]

    cells = table_extract._geometry_cells_for_mismatched_html(boxes, html_cells, 100)

    assert len(cells) == 16
    assert {cell["row"] for cell in cells} == {0, 1}
    assert [sum(cell["row"] == row for cell in cells) for row in (0, 1)] == [8, 8]
    assert all(cell["text"] == "" for cell in cells)


def test_overlapping_cell_borders_assign_a_heading_to_the_following_row():
    cells = [
        {"normalized_bbox": [0.0, 0.0, 1.0, 0.55]},
        {"normalized_bbox": [0.0, 0.54, 1.0, 1.0]},
    ]
    line = {
        "line_id": "p0001-l00001",
        "text": "Parking and Play ground",
        "normalized_bbox": [0.1, 0.50, 0.9, 0.60],
    }

    assigned = table_extract._assign_lines_to_cells(cells, [line])

    assert assigned[0] == []
    assert assigned[1] == [line]
