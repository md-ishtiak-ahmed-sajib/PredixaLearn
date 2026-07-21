"""PP-StructureV3 layout parsing using PaddleOCR's native formatter."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from PIL import Image

from app.core.capabilities import validate_document_profile, validate_language
from app.core.config import get_settings
from app.core.engine import get_manager
from app.core.errors import WorkflowResultError
from app.core.geometry import normalized_bbox
from app.core.progress import ProgressCallback, emit_progress
from app.workflows.pdf_utils import (
    clear_gpu_cache,
    is_pdf,
    iter_pdf_pages,
    translate_inference_error,
)
from app.workflows.result_adapters import (
    markdown_text,
    parsing_elements,
    persist_document,
    resolve_output_directory,
    result_payload,
)

logger = logging.getLogger("predixalearn.workflow.layout_parse")


def run_layout_parsing(
    image_input: str | Path,
    save_output: bool = True,
    output_dir: str | Path | None = None,
    *,
    job_id: str | None = None,
    progress_callback: ProgressCallback | None = None,
    language: str | None = None,
    document_profile: str = "auto",
) -> dict[str, Any]:
    settings = get_settings()
    selected_language = validate_language(
        language,
        ocr_version=settings.structure_ocr_version,
        default=settings.ocr_lang,
    )
    selected_profile = validate_document_profile(document_profile)
    input_path = str(image_input)
    elements: list[dict[str, Any]] = []
    markdown_pages: list[str] = []
    pages: list[dict[str, Any]] = []

    def consume(
        predictions: object,
        page_index: int,
        *,
        width: int | None = None,
        height: int | None = None,
        render_scale: float | None = None,
    ) -> None:
        result_count = 0
        for result in predictions:
            result_count += 1
            payload = result_payload(result, workflow="Layout parsing")
            markdown_pages.append(markdown_text(result, workflow="Layout parsing"))
            parsed = parsing_elements(
                payload,
                workflow="Layout parsing",
                page_index=page_index,
                first_index=len(elements),
            )
            if width and height:
                for element in parsed:
                    element["normalized_bbox"] = normalized_bbox(
                        element.get("bbox"), width, height
                    )
            elements.extend(parsed)
        if result_count != 1:
            raise WorkflowResultError(
                "Layout parsing returned an unexpected number of results for one page"
            )
        pages.append(
            {
                "page_index": page_index,
                "page_number": page_index + 1,
                "width": width,
                "height": height,
                "render_scale": render_scale,
            }
        )

    emit_progress(
        progress_callback,
        stage="initializing",
        message="Loading the layout analysis engine",
        completed_pages=0,
    )
    try:
        with get_manager().session(
            "structure", language=selected_language, document_profile=selected_profile
        ) as engine:
            if is_pdf(input_path):
                for page in iter_pdf_pages(
                    input_path,
                    settings=settings,
                    progress_callback=progress_callback,
                ):
                    logger.info("Layout PDF page %d/%d", page.index + 1, page.count)
                    emit_progress(
                        progress_callback,
                        stage="layout_analysis",
                        message=f"Analyzing layout on page {page.number} of {page.count}",
                        current_page=page.number,
                        total_pages=page.count,
                        completed_pages=page.index,
                    )
                    consume(
                        engine.predict(page.image),
                        page.index,
                        width=page.image.shape[1],
                        height=page.image.shape[0],
                        render_scale=page.render_scale,
                    )
                    emit_progress(
                        progress_callback,
                        stage="page_complete",
                        message=f"Finished page {page.number} of {page.count}",
                        current_page=page.number,
                        total_pages=page.count,
                        completed_pages=page.number,
                    )
            else:
                emit_progress(
                    progress_callback,
                    stage="layout_analysis",
                    message="Analyzing image layout",
                    current_page=1,
                    total_pages=1,
                    completed_pages=0,
                )
                with Image.open(input_path) as image:
                    width, height = image.size
                consume(engine.predict(input_path), 0, width=width, height=height)
                emit_progress(
                    progress_callback,
                    stage="page_complete",
                    message="Finished image",
                    current_page=1,
                    total_pages=1,
                    completed_pages=1,
                )
    except Exception as exc:
        translated = translate_inference_error(exc)
        if translated is not exc:
            raise translated from exc
        raise
    finally:
        if settings.clear_gpu_cache_after_document:
            clear_gpu_cache()

    emit_progress(
        progress_callback,
        stage="finalizing",
        message="Formatting Markdown and layout elements",
        completed_pages=len(markdown_pages),
        total_pages=len(markdown_pages),
    )
    markdown = "\n\n".join(part for part in markdown_pages if part).strip()
    saved_files: dict[str, str | None] = {
        "markdown_path": None,
        "json_path": None,
    }
    if save_output:
        saved_files = persist_document(
            output_dir=resolve_output_directory(output_dir, job_id, settings),
            markdown=markdown,
            json_value=elements,
            markdown_name="document.md",
            json_name="elements.json",
            settings=settings,
        )
    return {
        "markdown": markdown,
        "elements": elements,
        "saved_files": saved_files,
        "page_count": len(markdown_pages) or 1,
        "language": selected_language,
        "requested_document_profile": selected_profile,
        "pages": pages,
    }
