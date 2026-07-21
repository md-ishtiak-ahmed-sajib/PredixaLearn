"""
Workflow package — each module implements one feature capability.
"""

from app.workflows.layout_parse import run_layout_parsing
from app.workflows.ocr_text import run_text_recognition
from app.workflows.table_extract import run_table_extraction
from app.workflows.vl_process import run_vl_processing

__all__ = [
    "run_text_recognition",
    "run_layout_parsing",
    "run_table_extraction",
    "run_vl_processing",
]
