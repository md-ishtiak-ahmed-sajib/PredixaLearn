"""Common OCR artifact pipeline: crops, Markdown, DOCX, archive, and named publish."""

from __future__ import annotations

import hashlib
import io
import logging
import re
import shutil
from collections import defaultdict
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np
from PIL import Image, ImageDraw, ImageSequence

from app.core.config import Settings, get_settings
from app.core.engine import get_manager
from app.core.geometry import bbox_iou, bbox_values, normalized_bbox
from app.core.progress import ProgressCallback, emit_progress
from app.core.serialization import atomic_write_json, atomic_write_text
from app.documents.artifact_manifest import named_output_is_complete, validate_bundle
from app.documents.artifact_publishing import (
    copy_history_archive,
)
from app.documents.artifact_publishing import (
    publish_named_output as publish_output_bundle,
)
from app.documents.artifact_quality import docx_conversion_coverage, safe_docx_issue
from app.documents.docx import convert_markdown_to_docx
from app.documents.libreoffice import validate_docx_with_libreoffice
from app.documents.markdown import _bbox_values, build_structured_markdown
from app.documents.naming import safe_output_stem
from app.documents.text_filters import (
    normalize_remove_terms,
    normalize_remove_text,
    remove_literal_from_markdown,
    remove_literal_text,
    remove_terms_from_markdown,
    remove_terms_from_text,
)
from app.workflows.pdf_utils import is_pdf, iter_pdf_pages
from app.workflows.result_adapters import parsing_elements, result_payload
from app.workflows.table_extract import (
    extract_tables_from_prediction,
    table_quality_score,
    verify_table_against_lines,
)

logger = logging.getLogger("predixalearn.artifacts")
_JOB_ID = re.compile(r"^[0-9a-f]{32}$")
_FIGURE_LABELS = ("image", "figure", "chart", "illustration", "graphic", "photo", "diagram")
_DECORATIVE_LABELS = ("header", "footer", "page_number", "page number")
_SUPPLEMENTAL_LABELS = (
    "formula",
    "equation",
    "image",
    "figure",
    "chart",
    "illustration",
    "graphic",
    "photo",
    "diagram",
)
_SOURCE_PREVIEW_MAX_EDGE = 1600


@dataclass(frozen=True, slots=True)
class ArtifactOutcome:
    result: dict[str, Any]
    manifest_path: str | None
    archive_saved: bool


@dataclass(frozen=True, slots=True)
class SourcePage:
    index: int
    count: int
    image: np.ndarray
    render_scale: float | None = None


def _iter_image_pages(path: Path) -> Iterator[SourcePage]:
    with Image.open(path) as source:
        count = int(getattr(source, "n_frames", 1))
        for index, frame in enumerate(ImageSequence.Iterator(source)):
            rgb = frame.convert("RGB")
            array = np.asarray(rgb, dtype=np.uint8)
            bgr = np.ascontiguousarray(array[:, :, ::-1]).copy()
            yield SourcePage(index=index, count=count, image=bgr)


def iter_source_pages(path: Path, settings: Settings) -> Iterator[SourcePage]:
    if is_pdf(path):
        for page in iter_pdf_pages(path, settings=settings):
            yield SourcePage(page.index, page.count, page.image, page.render_scale)
        return
    yield from _iter_image_pages(path)


def _is_figure(element: Mapping[str, Any]) -> bool:
    label = str(element.get("type", "")).lower()
    return any(value in label for value in _FIGURE_LABELS) and not any(
        value in label for value in _DECORATIVE_LABELS
    )


def _element_key(element: Mapping[str, Any]) -> str:
    return f"{int(element.get('page_index', 0))}:{int(element.get('index', 0))}"


def _normalize_element_geometry(element: dict[str, Any], *, width: int, height: int) -> None:
    element["normalized_bbox"] = normalized_bbox(element.get("bbox"), width, height)


def _normalize_table_geometry(table: dict[str, Any], *, width: int, height: int) -> None:
    table["normalized_bbox"] = normalized_bbox(table.get("bbox"), width, height)
    cells = table.get("cells")
    if isinstance(cells, list):
        for cell in cells:
            if isinstance(cell, dict):
                cell["normalized_bbox"] = normalized_bbox(cell.get("bbox"), width, height)


def _remap_retry_table(
    table: dict[str, Any],
    *,
    left: int,
    top: int,
    scale: float,
    crop_width: int,
    crop_height: int,
    quarter_turns: int = 0,
) -> None:
    if scale <= 0:
        return

    base_width = crop_width * scale
    base_height = crop_height * scale

    def point(x_value: float, y_value: float) -> tuple[float, float]:
        rotation = quarter_turns % 4
        if rotation == 1:
            return base_width - y_value, x_value
        if rotation == 2:
            return base_width - x_value, base_height - y_value
        if rotation == 3:
            return y_value, base_height - x_value
        return x_value, y_value

    def map_bbox(raw: Any) -> list[float] | None:
        values = bbox_values(raw)
        if values is None:
            return None
        corners = [
            point(values[0], values[1]),
            point(values[2], values[1]),
            point(values[2], values[3]),
            point(values[0], values[3]),
        ]
        x_values = [value[0] / scale + left for value in corners]
        y_values = [value[1] / scale + top for value in corners]
        return [min(x_values), min(y_values), max(x_values), max(y_values)]

    mapped_table_bbox = map_bbox(table.get("bbox") or table.get("derived_bbox"))
    if mapped_table_bbox is not None:
        table["bbox"] = mapped_table_bbox
        table["derived_bbox"] = mapped_table_bbox
    cells = table.get("cells")
    if isinstance(cells, list):
        for cell in cells:
            if not isinstance(cell, dict):
                continue
            mapped = map_bbox(cell.get("bbox"))
            if mapped is not None:
                cell["bbox"] = mapped


def _figure_quality(
    element: Mapping[str, Any],
    page_image: np.ndarray,
    bounds: tuple[int, int, int, int] | None,
) -> tuple[bool, str | None]:
    if bounds is None:
        # A real Structure figure can occasionally arrive with a malformed box.
        # Preserve the previously documented full-page fallback instead of losing
        # possible source content; valid tiny boxes are still rejected below.
        return True, "invalid bounding box"
    left, top, right, bottom = bounds
    height, width = page_image.shape[:2]
    crop_width, crop_height = right - left, bottom - top
    area_ratio = (crop_width * crop_height) / max(1, width * height)
    if crop_width < 32 or crop_height < 32 or area_ratio < 0.0025:
        return False, "the detected region is too small to be a content figure"
    crop = page_image[top:bottom, left:right]
    gray = crop.mean(axis=2)
    ink_ratio = float(np.mean(gray < 238))
    if ink_ratio < 0.008:
        return False, "the detected region contains too little visible content"
    if (top < height * 0.06 or bottom > height * 0.96) and area_ratio < 0.02:
        return False, "the detected region appears decorative"
    return True, None


def _existing_elements(result: Mapping[str, Any]) -> list[dict[str, Any]]:
    raw = result.get("elements")
    if not isinstance(raw, Sequence) or isinstance(raw, (str, bytes)):
        return []
    return [dict(item) for item in raw if isinstance(item, Mapping)]


def _crop_bounds(
    bbox: Any,
    *,
    width: int,
    height: int,
    padding: int,
) -> tuple[int, int, int, int] | None:
    values = _bbox_values(bbox)
    if not values:
        return None
    x1, y1, x2, y2 = values
    left = max(0, int(np.floor(x1)) - padding)
    top = max(0, int(np.floor(y1)) - padding)
    right = min(width, int(np.ceil(x2)) + padding)
    bottom = min(height, int(np.ceil(y2)) + padding)
    if left >= right or top >= bottom:
        return None
    return left, top, right, bottom


def _png_bytes(bgr: np.ndarray) -> bytes:
    rgb = np.ascontiguousarray(bgr[:, :, ::-1])
    stream = io.BytesIO()
    Image.fromarray(rgb, mode="RGB").save(stream, format="PNG", optimize=True)
    return stream.getvalue()


def _write_source_previews(
    input_path: Path,
    *,
    destination: Path,
    settings: Settings,
) -> tuple[list[str], list[str]]:
    """Retain bounded local review previews without retaining the original upload.

    The preview is a derived, watermarked image in the History/output bundle.  It
    is intentionally not a copy of the source file and exists only so a teacher
    can inspect the exact page behind an OCR or AI conclusion.
    """

    previews: list[str] = []
    warnings: list[str] = []
    destination.mkdir(parents=True, exist_ok=True)
    try:
        for page in iter_source_pages(input_path, settings):
            rgb = np.ascontiguousarray(page.image[:, :, ::-1])
            image = Image.fromarray(rgb, mode="RGB")
            width, height = image.size
            scale = min(1.0, _SOURCE_PREVIEW_MAX_EDGE / max(width, height))
            if scale < 1.0:
                image = image.resize(
                    (max(1, round(width * scale)), max(1, round(height * scale))),
                    Image.Resampling.LANCZOS,
                )
            draw = ImageDraw.Draw(image, "RGBA")
            watermark = f"PredixaLearn local source preview · Page {page.index + 1}"
            padding = max(8, image.width // 80)
            top = max(0, image.height - max(28, image.height // 28))
            draw.rectangle((0, top, image.width, image.height), fill=(8, 47, 73, 145))
            draw.text((padding, top + padding // 2), watermark, fill=(255, 255, 255, 230))
            filename = f"page-{page.index + 1:03d}.png"
            image.save(destination / filename, format="PNG", optimize=True)
            previews.append(f"source-pages/{filename}")
    except (OSError, ValueError) as exc:
        logger.warning("Source preview generation failed: %s", type(exc).__name__)
        warnings.append(
            "Source page previews could not be retained; page/line evidence remains available."
        )
    return previews, warnings


def extract_figure_assets(
    input_path: Path,
    result: Mapping[str, Any],
    *,
    workflow: str,
    assets_dir: Path,
    settings: Settings,
    progress_callback: ProgressCallback | None,
) -> tuple[
    dict[str, str],
    list[dict[str, Any]],
    list[dict[str, Any]],
    list[str],
    int,
]:
    """Stream pages, retain non-prose Structure blocks, tables, and bounded PNG crops."""
    elements = _existing_elements(result)
    by_page: dict[int, list[dict[str, Any]]] = defaultdict(list)
    for element in elements:
        if _is_figure(element):
            by_page[int(element.get("page_index", 0) or 0)].append(element)
    primary_by_page: dict[int, list[dict[str, Any]]] = defaultdict(list)
    raw_lines = result.get("lines")
    if isinstance(raw_lines, Sequence) and not isinstance(raw_lines, (str, bytes)):
        for line in raw_lines:
            if isinstance(line, Mapping):
                primary_by_page[int(line.get("page_index", 0) or 0)].append(dict(line))

    detect_text_figures = workflow == "text_recognition"
    supplemental: list[dict[str, Any]] = []
    warnings: list[str] = []
    assets: dict[str, str] = {}
    digest_paths: dict[str, str] = {}
    total_bytes = 0
    image_number = 0
    next_index = 1_000_000
    next_table_index = 0
    detected_tables: list[dict[str, Any]] = []
    adaptive_retry_count = 0

    emit_progress(
        progress_callback,
        stage="figure_detection",
        message="Detecting figures and charts",
        completed_pages=0,
    )

    session = None
    structure_engine = None
    if detect_text_figures:
        try:
            session = get_manager().session(
                "structure",
                language=str(result.get("language") or settings.ocr_lang),
                document_profile=str(result.get("requested_document_profile") or "auto"),
            )
            structure_engine = session.__enter__()
        except Exception:
            if session is not None:
                session.__exit__(None, None, None)
            session = None
            structure_engine = None
            warnings.append("Text layout pass was unavailable; figure detection was skipped.")
            logger.warning("Text figure-detection engine was unavailable", exc_info=True)

    try:
        for page in iter_source_pages(input_path, settings):
            page_candidates = list(by_page.get(page.index, []))
            for candidate in page_candidates:
                _normalize_element_geometry(candidate, width=page.image.shape[1], height=page.image.shape[0])
            if structure_engine is not None:
                try:
                    emit_progress(
                        progress_callback,
                        stage="structure_analysis",
                        message=(
                            f"Analyzing structure and tables on source page "
                            f"{page.index + 1} of {page.count}"
                        ),
                        current_page=page.index + 1,
                        total_pages=page.count,
                        completed_pages=page.index,
                    )
                    predictions = list(structure_engine.predict(page.image))
                    if len(predictions) != 1:
                        raise RuntimeError("unexpected Structure result count")
                    payload = result_payload(predictions[0], workflow="Text figure detection")
                    detected = parsing_elements(
                        payload,
                        workflow="Text figure detection",
                        page_index=page.index,
                        first_index=next_index,
                    )
                    for item in detected:
                        _normalize_element_geometry(
                            item,
                            width=page.image.shape[1],
                            height=page.image.shape[0],
                        )
                    next_index += len(detected)
                    supplemental.extend(
                        item
                        for item in detected
                        if any(
                            label in str(item.get("type", "")).lower()
                            for label in _SUPPLEMENTAL_LABELS
                        )
                    )
                    page_candidates.extend(item for item in detected if _is_figure(item))
                    page_tables = extract_tables_from_prediction(
                        predictions[0],
                        page_index=page.index,
                        first_table_index=next_table_index,
                    )
                    for table in page_tables:
                        _normalize_table_geometry(
                            table,
                            width=page.image.shape[1],
                            height=page.image.shape[0],
                        )
                    next_table_index += len(page_tables)
                    retries_used = 0
                    for table in page_tables:
                        if table.get("structure_warning"):
                            warnings.append(
                                f"Table {int(table['table_index']) + 1} on source page "
                                f"{page.index + 1}: {table['structure_warning']}"
                            )
                        emit_progress(
                            progress_callback,
                            stage="quality_validation",
                            message=f"Validating table structure on source page {page.index + 1}",
                            current_page=page.index + 1,
                            total_pages=page.count,
                            completed_pages=page.index,
                        )
                        verify_table_against_lines(
                            table,
                            primary_by_page.get(page.index, ()),
                            quality_threshold=settings.table_quality_threshold,
                        )
                        score = table_quality_score(table)
                        if (
                            settings.adaptive_retry_enabled
                            and score < settings.table_quality_threshold
                            and retries_used < settings.max_retry_regions_per_page
                        ):
                            bounds = _crop_bounds(
                                table.get("bbox"),
                                width=page.image.shape[1],
                                height=page.image.shape[0],
                                padding=settings.crop_padding_pixels,
                            )
                            if bounds is not None:
                                left, top, right, bottom = bounds
                                crop = page.image[top:bottom, left:right]
                                scale = min(
                                    settings.adaptive_max_scale,
                                    (
                                        settings.max_image_pixels
                                        / max(1, crop.shape[0] * crop.shape[1])
                                    )
                                    ** 0.5,
                                )
                                if scale > 1.05:
                                    emit_progress(
                                        progress_callback,
                                        stage="adaptive_retry",
                                        message=(
                                            f"Retrying an uncertain table on source page "
                                            f"{page.index + 1} at {scale:.1f}x"
                                        ),
                                        current_page=page.index + 1,
                                        total_pages=page.count,
                                        completed_pages=page.index,
                                    )
                                    adaptive_retry_count += 1
                                    retry_metadata = {
                                        "attempted": True,
                                        "accepted": False,
                                        "scale": round(scale, 3),
                                        "orientations": [0, 90, 180, 270],
                                        "original_score": round(score, 4),
                                    }
                                    table["retry_metadata"] = retry_metadata
                                    try:
                                        with Image.fromarray(
                                            crop[:, :, ::-1],
                                            mode="RGB",
                                        ) as rgb:
                                            resized = rgb.resize(
                                                (
                                                    max(1, round(rgb.width * scale)),
                                                    max(1, round(rgb.height * scale)),
                                                ),
                                                Image.Resampling.LANCZOS,
                                            )
                                            try:
                                                retry_image = np.ascontiguousarray(
                                                    np.asarray(resized, dtype=np.uint8)[:, :, ::-1]
                                                )
                                            finally:
                                                resized.close()
                                        best_candidate: dict[str, Any] | None = None
                                        best_score = score
                                        for quarter_turns in range(4):
                                            oriented_image = np.ascontiguousarray(
                                                np.rot90(retry_image, quarter_turns)
                                            )
                                            retry_predictions = list(
                                                structure_engine.predict(oriented_image)
                                            )
                                            if len(retry_predictions) != 1:
                                                continue
                                            retry_tables = extract_tables_from_prediction(
                                                retry_predictions[0],
                                                page_index=page.index,
                                                first_table_index=int(table["table_index"]),
                                            )
                                            for candidate in retry_tables:
                                                _remap_retry_table(
                                                    candidate,
                                                    left=left,
                                                    top=top,
                                                    scale=scale,
                                                    crop_width=crop.shape[1],
                                                    crop_height=crop.shape[0],
                                                    quarter_turns=quarter_turns,
                                                )
                                                candidate["bbox_identity_score"] = bbox_iou(
                                                    candidate.get("bbox"),
                                                    table.get("bbox"),
                                                )
                                                candidate["orientation"] = quarter_turns * 90
                                                _normalize_table_geometry(
                                                    candidate,
                                                    width=page.image.shape[1],
                                                    height=page.image.shape[0],
                                                )
                                                verify_table_against_lines(
                                                    candidate,
                                                    primary_by_page.get(page.index, ()),
                                                    quality_threshold=settings.table_quality_threshold,
                                                )
                                                candidate_score = table_quality_score(candidate)
                                                if (
                                                    candidate.get("status") == "accepted"
                                                    and candidate_score > best_score
                                                ):
                                                    best_candidate = candidate
                                                    best_score = candidate_score
                                        if best_candidate is not None and best_score >= score + 0.05:
                                            best_candidate["adaptive_retry"] = True
                                            retry_metadata.update(
                                                {
                                                    "accepted": True,
                                                    "candidate_score": round(best_score, 4),
                                                    "selected_orientation": best_candidate[
                                                        "orientation"
                                                    ],
                                                }
                                            )
                                            best_candidate["retry_metadata"] = retry_metadata
                                            table = best_candidate
                                            score = best_score
                                    except Exception:
                                        warnings.append(
                                            f"An adaptive table retry failed on source page "
                                            f"{page.index + 1}; the safer original result was retained."
                                        )
                                        logger.warning(
                                            "Adaptive table retry failed on page %d",
                                            page.index + 1,
                                            exc_info=True,
                                        )
                                    retries_used += 1
                        table["quality_score"] = score
                        if table.get("status") != "accepted":
                            warnings.append(
                                f"Table {int(table['table_index']) + 1} on source page "
                                f"{page.index + 1} needs review because its structure is uncertain."
                            )
                            review_bounds = _crop_bounds(
                                table.get("bbox"),
                                width=page.image.shape[1],
                                height=page.image.shape[0],
                                padding=settings.crop_padding_pixels,
                            )
                            if (
                                review_bounds is not None
                                and image_number < settings.max_embedded_images
                            ):
                                review_left, review_top, review_right, review_bottom = (
                                    review_bounds
                                )
                                review_content = _png_bytes(
                                    page.image[
                                        review_top:review_bottom,
                                        review_left:review_right,
                                    ]
                                )
                                if (
                                    total_bytes + len(review_content)
                                    <= settings.max_embedded_image_bytes
                                ):
                                    image_number += 1
                                    relative = (
                                        f"assets/page-{page.index + 1:03d}-"
                                        f"table-{int(table['table_index']) + 1:03d}-review.png"
                                    )
                                    target = assets_dir.parent / relative
                                    target.parent.mkdir(parents=True, exist_ok=True)
                                    target.write_bytes(review_content)
                                    total_bytes += len(review_content)
                                    table["review_image"] = relative
                        detected_tables.append(table)
                except Exception:
                    warnings.append(
                        f"Supplemental Structure analysis failed on source page {page.index + 1}; "
                        "primary OCR text was retained."
                    )
                    logger.warning(
                        "Text figure detection failed on page %d",
                        page.index + 1,
                        exc_info=True,
                    )

            emit_progress(
                progress_callback,
                stage="cropping",
                message=f"Cropping figures from source page {page.index + 1} of {page.count}",
                current_page=page.index + 1,
                total_pages=page.count,
                completed_pages=page.index,
            )
            page_image_number = 0
            height, width = page.image.shape[:2]
            for element in page_candidates:
                key = _element_key(element)
                if key in assets:
                    continue
                if image_number >= settings.max_embedded_images:
                    warnings.append(
                        f"Figure limit ({settings.max_embedded_images}) reached; remaining figures were omitted."
                    )
                    break
                bounds = _crop_bounds(
                    element.get("bbox"),
                    width=width,
                    height=height,
                    padding=settings.crop_padding_pixels,
                )
                accepted_figure, rejection = _figure_quality(element, page.image, bounds)
                if not accepted_figure:
                    element["figure_status"] = "rejected"
                    element["figure_rejection_reason"] = rejection
                    if isinstance(result, dict):
                        reviews = result.setdefault("review_suggestions", [])
                        if isinstance(reviews, list):
                            reviews.append(
                                {
                                    "kind": "figure_candidate",
                                    "confidence": 0.0,
                                    "original": str(element.get("content", "")),
                                    "suggested": None,
                                    "page": page.index + 1,
                                    "block": int(element.get("index", 0) or 0),
                                    "reason": rejection,
                                    "bbox": element.get("normalized_bbox")
                                    or element.get("bbox", []),
                                }
                            )
                    warnings.append(
                        f"A possible figure on source page {page.index + 1} was not embedded "
                        f"because {rejection}."
                    )
                    continue
                element["figure_status"] = "accepted"
                if bounds is None:
                    crop = page.image
                    warnings.append(
                        f"Figure on source page {page.index + 1} had an invalid box; "
                        "the full page was used."
                    )
                else:
                    left, top, right, bottom = bounds
                    crop = page.image[top:bottom, left:right]
                content = _png_bytes(crop)
                if total_bytes + len(content) > settings.max_embedded_image_bytes:
                    warnings.append(
                        "Embedded-image byte limit reached; remaining figures were omitted."
                    )
                    break
                digest = hashlib.sha256(content).hexdigest()
                existing = digest_paths.get(digest)
                if existing:
                    assets[key] = existing
                    continue
                image_number += 1
                page_image_number += 1
                relative = (
                    f"assets/page-{page.index + 1:03d}-image-{page_image_number:03d}.png"
                )
                target = assets_dir.parent / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(content)
                total_bytes += len(content)
                digest_paths[digest] = relative
                assets[key] = relative
    finally:
        if session is not None:
            session.__exit__(None, None, None)

    return (
        assets,
        supplemental,
        detected_tables,
        list(dict.fromkeys(warnings)),
        adaptive_retry_count,
    )


def _export_tables(
    result: dict[str, Any],
    *,
    bundle: Path,
    output_stem: str,
    settings: Settings,
) -> list[str]:
    from app.workflows.table_extract import _cells_to_grid, _save_csv, _save_excel

    exported: list[str] = []
    raw_tables = result.get("tables")
    if not isinstance(raw_tables, Sequence) or isinstance(raw_tables, (str, bytes)):
        return exported
    for position, raw_table in enumerate(raw_tables, start=1):
        if not isinstance(raw_table, dict):
            continue
        if raw_table.get("status") != "accepted":
            continue
        cells = raw_table.get("cells")
        if not isinstance(cells, list) or not cells:
            continue
        grid = _cells_to_grid(cells, settings.max_table_cells)
        if not grid:
            continue
        xlsx_relative = f"tables/table-{position:03d}.xlsx"
        csv_relative = f"tables/table-{position:03d}.csv"
        _save_excel(cells, bundle / xlsx_relative)
        _save_csv(grid, bundle / csv_relative)
        raw_table["excel_path"] = f"{output_stem}/{xlsx_relative}"
        raw_table["csv_path"] = f"{output_stem}/{csv_relative}"
        exported.extend([xlsx_relative, csv_relative])
    return exported


def _copy_archive(bundle: Path, *, settings: Settings, job_id: str, output_stem: str) -> str:
    """Compatibility façade for callers that used the original helper."""
    return copy_history_archive(
        bundle, settings=settings, job_id=job_id, output_stem=output_stem
    )


def _atomic_publish(bundle: Path, *, settings: Settings, output_stem: str, job_id: str) -> Path:
    """Compatibility façade for callers that used the original helper."""
    return publish_output_bundle(
        bundle, settings=settings, output_stem=output_stem, job_id=job_id
    )


def _validate_bundle(bundle: Path, output_stem: str, *, require_docx: bool) -> None:
    """Compatibility façade for bundle validation."""
    validate_bundle(bundle, output_stem, require_docx=require_docx)


def _named_output_is_complete(*, settings: Settings, output_stem: str) -> bool:
    """Compatibility façade for named-output protection."""
    return named_output_is_complete(settings=settings, output_stem=output_stem)


def _safe_docx_issue(exc: Exception) -> tuple[str, str]:
    """Compatibility façade for DOCX failure classification."""
    return safe_docx_issue(exc)


def _docx_conversion_coverage(
    markdown: str,
    docx_path: Path,
    *,
    expected_tables: int,
    expected_images: int,
) -> dict[str, Any]:
    """Compatibility façade for conversion coverage checks."""
    return docx_conversion_coverage(
        markdown,
        docx_path,
        expected_tables=expected_tables,
        expected_images=expected_images,
    )


def build_and_publish_artifacts(
    input_path: str | Path,
    input_name: str,
    workflow: str,
    raw_result: Mapping[str, Any],
    *,
    job_id: str,
    settings: Settings | None = None,
    progress_callback: ProgressCallback | None = None,
    remove_text: str | None = None,
    remove_terms: Sequence[str] | None = None,
) -> ArtifactOutcome:
    """Create the common bundle without turning an export failure into lost OCR output."""
    cfg = settings or get_settings()
    if not _JOB_ID.fullmatch(job_id):
        raise ValueError("Invalid job identifier")
    result = dict(raw_result)
    normalized_remove_text = normalize_remove_text(remove_text)
    normalized_remove_terms = normalize_remove_terms(list(remove_terms or ()))
    effective_remove_terms = list(normalized_remove_terms)
    if normalized_remove_text and normalized_remove_text.casefold() not in {
        value.casefold() for value in effective_remove_terms
    }:
        effective_remove_terms.insert(0, normalized_remove_text)
    warnings = [str(item) for item in result.get("warnings", []) if str(item)]
    output_stem = safe_output_stem(input_name, fallback=f"document-{job_id[:8]}")
    staging_root = cfg.output_dir / ".staging" / job_id
    bundle = staging_root / output_stem
    manifest_path: str | None = None
    archive_saved = False
    cfg.output_dir.mkdir(parents=True, exist_ok=True)
    if staging_root.exists():
        shutil.rmtree(staging_root)
    bundle.mkdir(parents=True, exist_ok=True)

    try:
        emit_progress(
            progress_callback,
            stage="source_preview",
            message="Preparing local source previews for teacher review",
        )
        source_previews, source_preview_warnings = _write_source_previews(
            Path(input_path),
            destination=bundle / "source-pages",
            settings=cfg,
        )
        warnings.extend(source_preview_warnings)
        result["source_previews"] = source_previews
        (
            figure_assets,
            supplemental,
            detected_tables,
            figure_warnings,
            table_retry_count,
        ) = extract_figure_assets(
            Path(input_path),
            result,
            workflow=workflow,
            assets_dir=bundle / "assets",
            settings=cfg,
            progress_callback=progress_callback,
        )
        warnings.extend(figure_warnings)
        if supplemental:
            result["supplemental_elements"] = supplemental
        if workflow == "text_recognition" and detected_tables:
            result["tables"] = detected_tables
            result["table_count"] = len(detected_tables)
        result["adaptive_retry_attempted"] = int(
            result.get("adaptive_retry_attempted", result.get("adaptive_retry_count", 0)) or 0
        ) + int(table_retry_count)
        result["adaptive_retry_accepted"] = int(
            result.get("adaptive_retry_accepted", 0) or 0
        ) + sum(bool(table.get("adaptive_retry")) for table in detected_tables)
        result["adaptive_retry_count"] = result["adaptive_retry_attempted"]

        emit_progress(
            progress_callback,
            stage="markdown_cleanup",
            message="Reconstructing page-aligned Markdown",
        )
        emit_progress(
            progress_callback,
            stage="correction",
            message="Applying conservative spelling and punctuation corrections",
        )
        (
            markdown,
            corrections,
            review_suggestions,
            markdown_warnings,
            table_specs,
        ) = build_structured_markdown(
            result,
            workflow=workflow,
            source_name=input_name,
            output_stem=output_stem,
            figure_assets=figure_assets,
            settings=cfg,
            supplemental_elements=supplemental,
        )
        if effective_remove_terms:
            if normalized_remove_terms:
                markdown, removals = remove_terms_from_markdown(
                    markdown,
                    effective_remove_terms,
                )
            else:
                markdown, removal = remove_literal_from_markdown(
                    markdown,
                    normalized_remove_text or "",
                )
                removals = [removal]
            result["removed_text"] = ", ".join(value.text for value in removals)
            result["removed_text_count"] = sum(value.count for value in removals)
            result["removed_terms"] = [
                {"term": value.text, "removed_count": value.count} for value in removals
            ]
            result["text_removal"] = {
                "text": result["removed_text"],
                "terms": result["removed_terms"],
                "removed_count": result["removed_text_count"],
                "matching": "case-insensitive literal word or phrase",
            }
            full_text = result.get("full_text")
            if isinstance(full_text, str):
                result.setdefault("raw_full_text", full_text)
                if normalized_remove_terms:
                    result["full_text"], _ = remove_terms_from_text(
                        full_text,
                        effective_remove_terms,
                    )
                else:
                    result["full_text"], _ = remove_literal_text(
                        full_text,
                        normalized_remove_text or "",
                    )
            coverage = result.get("content_coverage")
            lines = result.get("lines")
            if isinstance(coverage, dict) and isinstance(lines, list):
                dispositions = coverage.get("line_dispositions")
                if isinstance(dispositions, dict):
                    fully_removed = 0
                    for line in lines:
                        if not isinstance(line, Mapping) or not line.get("line_id"):
                            continue
                        original = str(line.get("retry_text", line.get("text", "")))
                        filtered, line_removals = remove_terms_from_text(
                            original,
                            effective_remove_terms,
                        )
                        if any(value.count for value in line_removals) and not filtered.strip():
                            dispositions[str(line["line_id"])] = "user_removed"
                            line["disposition"] = "user_removed"
                            fully_removed += 1
                    coverage["user_removed_count"] = fully_removed
                    coverage["emitted_prose_count"] = sum(
                        value == "prose" for value in dispositions.values()
                    )
                    accounted = sum(value != "unresolved" for value in dispositions.values())
                    coverage["unaccounted_line_count"] = len(dispositions) - accounted
                    coverage["coverage_percent"] = round(
                        100.0 * accounted / len(dispositions), 2
                    ) if dispositions else 100.0
                    coverage["status"] = "passed" if accounted == len(dispositions) else "failed"
        else:
            result["removed_terms"] = []
            result.setdefault("removed_text_count", 0)
        warnings.extend(markdown_warnings)
        if isinstance(result.get("markdown"), str):
            result.setdefault("raw_markdown", result["markdown"])
        result["markdown"] = markdown
        result["corrections"] = corrections
        result["correction_count"] = len(corrections)
        existing_reviews = result.get("review_suggestions")
        combined_reviews = (
            [item for item in existing_reviews if isinstance(item, Mapping)]
            if isinstance(existing_reviews, Sequence) and not isinstance(existing_reviews, (str, bytes))
            else []
        )
        combined_reviews.extend(review_suggestions)
        result["review_suggestions"] = combined_reviews
        result["review_count"] = len(combined_reviews)
        result_tables = result.get("tables")
        table_review_images = (
            [
                str(table.get("review_image"))
                for table in result_tables
                if isinstance(table, Mapping) and table.get("review_image")
            ]
            if isinstance(result_tables, Sequence)
            and not isinstance(result_tables, (str, bytes))
            else []
        )
        artifact_images = sorted(
            set(figure_assets.values()).union(table_review_images)
        )
        result["image_count"] = len(artifact_images)
        result["table_count"] = int(result.get("table_count", 0) or 0)
        if not result["table_count"] and isinstance(result.get("tables"), Sequence):
            result["table_count"] = len(result["tables"])
        lines = result.get("lines")
        low_confidence_count = (
            sum(
                isinstance(line, Mapping)
                and float(line.get("retry_confidence", line.get("confidence", 0.0)) or 0.0)
                < cfg.low_confidence_threshold
                for line in lines
            )
            if isinstance(lines, Sequence) and not isinstance(lines, (str, bytes))
            else 0
        )
        tables = result.get("tables")
        unverified_tables = (
            sum(
                isinstance(table, Mapping) and table.get("status") != "accepted"
                for table in tables
            )
            if isinstance(tables, Sequence) and not isinstance(tables, (str, bytes))
            else 0
        )
        coverage = result.get("content_coverage")
        unaccounted = (
            int(coverage.get("unaccounted_line_count", 0) or 0)
            if isinstance(coverage, Mapping)
            else 0
        )
        if unaccounted:
            warnings.append(
                f"{unaccounted} primary OCR line(s) could not be reconciled automatically; "
                "their raw evidence remains in JSON and the export needs review."
            )
            combined_reviews.append(
                {
                    "kind": "content_coverage",
                    "confidence": 0.0,
                    "original": "",
                    "corrected": None,
                    "page": None,
                    "block": None,
                    "reason": f"{unaccounted} primary OCR line(s) are unaccounted",
                }
            )
            result["review_suggestions"] = combined_reviews
            result["review_count"] = len(combined_reviews)
        needs_review = bool(low_confidence_count or unverified_tables or unaccounted or combined_reviews)
        result["recognition_quality"] = {
            "status": "needs_review" if needs_review else "good",
            "low_confidence_count": low_confidence_count,
            "improbable_token_count": sum(
                item.get("kind") in {"word_boundary", "spelling"}
                for item in combined_reviews
                if isinstance(item, Mapping)
            ),
            "unverified_table_count": unverified_tables,
            "unaccounted_line_count": unaccounted,
            "adaptive_retry_attempted": result["adaptive_retry_attempted"],
            "adaptive_retry_accepted": result["adaptive_retry_accepted"],
        }
        result["page_count"] = int(result.get("page_count", 0) or 0) or max(
            (int(item.get("page_index", 0)) + 1 for item in supplemental),
            default=1,
        )

        markdown_relative = f"{output_stem}.md"
        docx_relative = f"{output_stem}.docx"
        json_relative = f"{output_stem}.json"
        atomic_write_text(bundle / markdown_relative, markdown)
        table_files = _export_tables(result, bundle=bundle, output_stem=output_stem, settings=cfg)

        emit_progress(
            progress_callback,
            stage="docx_generation",
            message="Generating the professional DOCX document",
        )
        docx_available = False
        docx_issue_code: str | None = None
        try:
            warnings.extend(
                convert_markdown_to_docx(
                    bundle / markdown_relative,
                    bundle / docx_relative,
                    source_name=input_name,
                    settings=cfg,
                    table_specs=table_specs,
                )
            )
            docx_available = True
            result["docx_generation"] = {
                "status": "passed",
                "attempted": True,
                "issue_code": None,
                "warning": None,
            }
        except Exception as exc:
            logger.exception("DOCX generation failed for job %s", job_id)
            docx_issue_code, safe_warning = safe_docx_issue(exc)
            warnings.append(safe_warning)
            (bundle / docx_relative).unlink(missing_ok=True)
            result["docx_generation"] = {
                "status": "failed",
                "attempted": True,
                "issue_code": docx_issue_code,
                "warning": safe_warning,
            }

        if docx_available:
            conversion_coverage = docx_conversion_coverage(
                markdown,
                bundle / docx_relative,
                expected_tables=len(table_specs),
                expected_images=len(artifact_images),
            )
            result["docx_conversion_coverage"] = conversion_coverage
            if conversion_coverage["status"] != "passed":
                warnings.append(
                    "DOCX conversion coverage needs review because text, table, or figure "
                    "content did not fully reconcile with the generated OOXML."
                )

            emit_progress(
                progress_callback,
                stage="libreoffice_validation",
                message="Validating the DOCX with project-local LibreOffice",
            )
            document_validation = validate_docx_with_libreoffice(
                bundle / docx_relative,
                work_dir=bundle,
                expected_source_pages=result["page_count"],
                settings=cfg,
            )
            result["document_validation"] = document_validation.as_dict()
            if document_validation.warning:
                warnings.append(document_validation.warning)
        else:
            result["docx_conversion_coverage"] = {
                "status": "not_run",
                "issue_code": docx_issue_code,
            }
            result["document_validation"] = {
                "status": "not_run",
                "renderer": "libreoffice",
                "version": None,
                "rendered_pages": None,
                "duration_seconds": 0.0,
                "attempted": False,
                "issue_code": "docx_unavailable",
                "warning": "DOCX render validation was not run because no DOCX was generated.",
            }

        artifact_paths: dict[str, Any] = {
            "markdown": f"{output_stem}/{markdown_relative}",
            "json": f"{output_stem}/{json_relative}",
            "images": [f"{output_stem}/{path}" for path in artifact_images],
            "tables": [f"{output_stem}/{path}" for path in table_files],
            "source_pages": [f"{output_stem}/{path}" for path in source_previews],
        }
        artifact_paths["docx"] = (
            f"{output_stem}/{docx_relative}" if docx_available else None
        )
        result["artifacts"] = artifact_paths
        result["saved_files"] = {
            "markdown_path": artifact_paths["markdown"],
            "docx_path": artifact_paths["docx"],
            "json_path": artifact_paths["json"],
        }
        result["artifact_status"] = "complete" if docx_available else "partial"
        publish_named_output = docx_available or not named_output_is_complete(
            settings=cfg,
            output_stem=output_stem,
        )
        result["named_output_published"] = publish_named_output
        if not publish_named_output:
            warnings.append(
                "This partial run was retained in History; the existing complete named export "
                "was preserved."
            )
        result["warnings"] = list(dict.fromkeys(warnings))
        atomic_write_json(bundle / json_relative, result)
        validate_bundle(bundle, output_stem, require_docx=docx_available)

        emit_progress(
            progress_callback,
            stage="archiving",
            message="Saving this run to private history",
        )
        archive_relative: str | None = None
        try:
            archive_relative = copy_history_archive(
                bundle,
                settings=cfg,
                job_id=job_id,
                output_stem=output_stem,
            )
            manifest = {
                "version": 1,
                "job_id": job_id,
                "input_name": input_name,
                "output_stem": output_stem,
                "archive_root": archive_relative,
                "files": {
                    "markdown": markdown_relative,
                    "docx": docx_relative if docx_available else None,
                    "json": json_relative,
                    "images": artifact_images,
                    "tables": table_files,
                    "source_pages": source_previews,
                },
                "legacy": False,
            }
            manifest_file = cfg.history_dir / "jobs" / job_id / "manifest.json"
            atomic_write_json(manifest_file, manifest)
            manifest_path = manifest_file.relative_to(cfg.history_dir).as_posix()
            archive_saved = True
        except Exception:
            logger.exception("Artifact archive failed for job %s", job_id)
            warnings.append("OCR succeeded, but the generated bundle could not be archived in history.")
            result["warnings"] = list(dict.fromkeys(warnings))
            atomic_write_json(bundle / json_relative, result)

        if publish_named_output:
            emit_progress(
                progress_callback,
                stage="publishing",
                message=f"Publishing output/{output_stem}",
            )
            publish_output_bundle(bundle, settings=cfg, output_stem=output_stem, job_id=job_id)
        else:
            emit_progress(
                progress_callback,
                stage="publishing",
                message="Partial export archived; existing complete named output preserved",
            )
        return ArtifactOutcome(result=result, manifest_path=manifest_path, archive_saved=archive_saved)
    except Exception as exc:
        logger.exception("Artifact pipeline failed for job %s", job_id)
        warnings.append(f"Document export failed safely: {type(exc).__name__}")
        result.setdefault("markdown", str(result.get("full_text", "")))
        result["artifact_status"] = "failed"
        result["artifacts"] = {
            "markdown": None,
            "docx": None,
            "json": None,
            "images": [],
            "tables": [],
        }
        result["image_count"] = 0
        result["correction_count"] = 0
        result["review_suggestions"] = []
        result["review_count"] = 0
        result["adaptive_retry_count"] = int(result.get("adaptive_retry_count", 0) or 0)
        result["table_count"] = int(result.get("table_count", 0) or 0)
        result.setdefault(
            "docx_generation",
            {
                "status": "failed",
                "attempted": False,
                "issue_code": "artifact_pipeline_failed",
                "warning": "DOCX generation was not completed because artifact export failed.",
            },
        )
        result.setdefault(
            "document_validation",
            {
                "status": "not_run",
                "renderer": "libreoffice",
                "version": None,
                "rendered_pages": None,
                "duration_seconds": 0.0,
                "attempted": False,
                "issue_code": "docx_unavailable",
                "warning": "DOCX render validation was not run because document export failed.",
            },
        )
        result["warnings"] = list(dict.fromkeys(warnings))
        return ArtifactOutcome(result=result, manifest_path=None, archive_saved=False)
    finally:
        if staging_root.exists():
            shutil.rmtree(staging_root, ignore_errors=True)
