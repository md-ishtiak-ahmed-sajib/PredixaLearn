"""PP-OCRv6 text recognition with bounded PDF rendering."""

from __future__ import annotations

import logging
import math
import re
from collections.abc import Sequence
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any

import numpy as np
from PIL import Image

from app.core.capabilities import validate_document_profile, validate_language
from app.core.config import get_settings
from app.core.engine import get_manager
from app.core.errors import WorkflowResultError
from app.core.geometry import normalized_bbox
from app.core.progress import ProgressCallback, emit_progress
from app.core.serialization import json_safe
from app.workflows.pdf_utils import (
    clear_gpu_cache,
    is_pdf,
    iter_pdf_pages,
    render_pdf_page,
    translate_inference_error,
)
from app.workflows.result_adapters import result_payload

logger = logging.getLogger("predixalearn.workflow.ocr_text")
_FUSED_WORD = re.compile(r"[A-Za-z]{12,}")
_QUALITY_TOKEN = re.compile(r"[A-Za-z]{4,}")


def _bbox_rect(raw: Any) -> tuple[int, int, int, int] | None:
    if not isinstance(raw, Sequence) or isinstance(raw, (str, bytes)):
        return None
    values: list[float] = []
    for value in raw:
        if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
            values.extend(float(item) for item in value if isinstance(item, (int, float)))
        elif isinstance(value, (int, float)):
            values.append(float(value))
    if len(values) == 4:
        x1, y1, x2, y2 = values
    elif len(values) >= 8 and len(values) % 2 == 0:
        x1, y1 = min(values[0::2]), min(values[1::2])
        x2, y2 = max(values[0::2]), max(values[1::2])
    else:
        return None
    if not all(math.isfinite(value) for value in (x1, y1, x2, y2)):
        return None
    rect = math.floor(x1), math.floor(y1), math.ceil(x2), math.ceil(y2)
    return rect if rect[0] < rect[2] and rect[1] < rect[3] else None


def _retry_is_better(
    original_text: str,
    original_confidence: float,
    candidate_text: str,
    candidate_confidence: float,
) -> bool:
    original_key = re.sub(r"\W+", "", original_text).casefold()
    candidate_key = re.sub(r"\W+", "", candidate_text).casefold()
    if not original_key or not candidate_key:
        return False
    restored_boundary = (
        original_key == candidate_key
        and candidate_text.count(" ") > original_text.count(" ")
        and bool(_FUSED_WORD.search(original_text))
    )
    similarity = SequenceMatcher(None, original_key, candidate_key).ratio()
    confidence_improved = candidate_confidence >= original_confidence + 0.08
    return (restored_boundary and candidate_confidence >= original_confidence) or (
        confidence_improved and similarity >= 0.72
    )


def _page_quality(lines: Sequence[dict[str, Any]], *, language: str) -> tuple[float, list[str]]:
    """Score a complete OCR page without treating confidence as semantic correctness."""
    if not lines:
        return 0.0, ["no_text"]
    confidences = [float(line.get("confidence", 0.0) or 0.0) for line in lines]
    low_ratio = sum(value < 0.90 for value in confidences) / len(confidences)
    fused_ratio = sum(bool(_FUSED_WORD.search(str(line.get("text", "")))) for line in lines) / len(
        lines
    )
    improbable_ratio = 0.0
    if language == "en":
        try:
            from wordfreq import zipf_frequency

            tokens = {
                token.casefold()
                for line in lines
                for token in _QUALITY_TOKEN.findall(str(line.get("text", "")))
            }
            if tokens:
                improbable_ratio = sum(
                    zipf_frequency(token, "en", wordlist="best") < 1.5 for token in tokens
                ) / len(tokens)
        except ImportError:
            improbable_ratio = 0.0
    average_confidence = sum(confidences) / len(confidences)
    score = (
        0.58 * average_confidence
        + 0.17 * (1.0 - low_ratio)
        + 0.15 * (1.0 - improbable_ratio)
        + 0.10 * (1.0 - fused_ratio)
    )
    reasons: list[str] = []
    if low_ratio >= 0.12:
        reasons.append("low_confidence_density")
    if improbable_ratio >= 0.12:
        reasons.append("improbable_token_density")
    if fused_ratio >= 0.04:
        reasons.append("fused_word_density")
    return round(max(0.0, min(1.0, score)), 4), reasons


def _full_page_retry_is_better(
    original: Sequence[dict[str, Any]],
    candidate: Sequence[dict[str, Any]],
    *,
    original_score: float,
    candidate_score: float,
) -> bool:
    if not candidate or candidate_score < original_score + 0.025:
        return False
    original_key = re.sub(
        r"\W+", "", " ".join(str(line.get("text", "")) for line in original)
    ).casefold()
    candidate_key = re.sub(
        r"\W+", "", " ".join(str(line.get("text", "")) for line in candidate)
    ).casefold()
    if not original_key or not candidate_key:
        return False
    similarity = SequenceMatcher(None, original_key, candidate_key).ratio()
    return similarity >= 0.55 and len(candidate_key) >= int(len(original_key) * 0.85)


def _adaptive_retry_page(
    engine: Any,
    page_image: np.ndarray,
    lines: list[dict[str, Any]],
    *,
    page_index: int,
    progress_callback: ProgressCallback | None,
) -> int:
    settings = get_settings()
    if not settings.adaptive_retry_enabled or settings.max_retry_regions_per_page < 1:
        return 0
    height, width = page_image.shape[:2]
    flagged: list[tuple[dict[str, Any], np.ndarray]] = []
    for line in lines:
        text = str(line.get("text", ""))
        confidence = float(line.get("confidence", 0.0) or 0.0)
        if confidence >= settings.low_confidence_threshold and not _FUSED_WORD.search(text):
            continue
        rect = _bbox_rect(line.get("bbox"))
        if rect is None:
            continue
        padding = 6
        left = max(0, rect[0] - padding)
        top = max(0, rect[1] - padding)
        right = min(width, rect[2] + padding)
        bottom = min(height, rect[3] + padding)
        if left >= right or top >= bottom:
            continue
        crop = page_image[top:bottom, left:right]
        pixels = crop.shape[0] * crop.shape[1]
        scale = min(
            settings.adaptive_max_scale,
            math.sqrt(settings.max_image_pixels / max(1, pixels)),
        )
        if scale <= 1.01:
            continue
        rgb = Image.fromarray(crop[:, :, ::-1], mode="RGB")
        resized = rgb.resize(
            (max(1, round(rgb.width * scale)), max(1, round(rgb.height * scale))),
            Image.Resampling.LANCZOS,
        )
        retry_bgr = np.asarray(resized, dtype=np.uint8)[:, :, ::-1].copy()
        rgb.close()
        resized.close()
        flagged.append((line, retry_bgr))
        if len(flagged) >= settings.max_retry_regions_per_page:
            break
    if not flagged:
        return 0

    emit_progress(
        progress_callback,
        stage="adaptive_retry",
        message=f"Retrying {len(flagged)} uncertain region(s) on page {page_index + 1}",
        current_page=page_index + 1,
    )
    predictions = list(engine.predict([crop for _, crop in flagged]))
    if len(predictions) != len(flagged):
        logger.warning(
            "Adaptive OCR retry returned %d results for %d regions",
            len(predictions),
            len(flagged),
        )
        return len(flagged)
    for (line, _), prediction in zip(flagged, predictions, strict=True):
        parsed = _parse_ocr_result(prediction, page_index=page_index)
        retry_lines = parsed["lines"]
        candidate_text = " ".join(str(item["text"]).strip() for item in retry_lines).strip()
        if not candidate_text:
            continue
        candidate_confidence = sum(float(item["confidence"]) for item in retry_lines) / len(
            retry_lines
        )
        original_text = str(line.get("text", ""))
        original_confidence = float(line.get("confidence", 0.0) or 0.0)
        if _retry_is_better(
            original_text,
            original_confidence,
            candidate_text,
            candidate_confidence,
        ):
            line["retry_text"] = candidate_text
            line["retry_confidence"] = round(candidate_confidence, 4)
    return len(flagged)


def _load_image_page(path: str, page_index: int) -> np.ndarray | None:
    try:
        with Image.open(path) as image:
            image.seek(page_index)
            rgb = image.convert("RGB")
            return np.asarray(rgb, dtype=np.uint8)[:, :, ::-1].copy()
    except (OSError, EOFError):
        logger.warning("Adaptive OCR retry could not reload image page %d", page_index + 1)
        return None


def _parse_ocr_result(
    result: object,
    *,
    page_index: int = 0,
    page_width: int | None = None,
    page_height: int | None = None,
) -> dict[str, Any]:
    page_number = page_index + 1
    payload = result_payload(result, workflow="Text recognition")
    texts = payload.get("rec_texts")
    scores = payload.get("rec_scores")
    boxes = payload.get("rec_boxes")
    polygons = payload.get("rec_polys")
    if not isinstance(texts, Sequence) or isinstance(texts, (str, bytes)):
        raise WorkflowResultError(
            "Text recognition returned an unsupported PaddleOCR result schema: rec_texts is missing"
        )
    if scores is None:
        scores = []
    if boxes is None:
        boxes = []
    if polygons is None:
        polygons = []

    lines: list[dict[str, Any]] = []
    for index, text in enumerate(texts):
        confidence = float(scores[index]) if index < len(scores) else 0.0
        if index < len(boxes):
            bbox = json_safe(boxes[index])
        elif index < len(polygons):
            bbox = json_safe(polygons[index])
        else:
            bbox = []
        line = {
            "text": str(text),
            "confidence": round(confidence, 4),
            "bbox": bbox,
            "page_index": page_index,
            "page_number": page_number,
            "line_id": f"p{page_number:04d}-l{index + 1:05d}",
        }
        if page_width and page_height:
            line["normalized_bbox"] = normalized_bbox(bbox, page_width, page_height)
        lines.append(line)
    return {
        "page_index": page_index,
        "page_number": page_number,
        "lines": lines,
        "full_text": "\n".join(line["text"] for line in lines),
    }


def _combine_pages(
    pages: list[dict[str, Any]],
    *,
    language: str,
    document_profile: str,
) -> dict[str, Any]:
    lines = [line for page in pages for line in page["lines"]]
    attempted = sum(int(page.get("adaptive_retry_attempted", 0) or 0) for page in pages)
    accepted = sum(int(page.get("adaptive_retry_accepted", 0) or 0) for page in pages)
    page_summaries: list[dict[str, Any]] = []
    line_cursor = 0
    for page in pages:
        line_count = len(page["lines"])
        page_summaries.append(
            {
                "page_index": page["page_index"],
                "page_number": page["page_number"],
                "line_start": line_cursor,
                "line_end": line_cursor + line_count,
                "full_text": page["full_text"],
                "width": page.get("width"),
                "height": page.get("height"),
                "render_scale": page.get("render_scale"),
                "page_retry": page.get("page_retry"),
            }
        )
        line_cursor += line_count
    return {
        "lines": lines,
        "full_text": "\n".join(line["text"] for line in lines),
        "page_count": len(pages),
        "language": language,
        "requested_document_profile": document_profile,
        "pages": page_summaries,
        "adaptive_retry_attempted": attempted,
        "adaptive_retry_accepted": accepted,
        "adaptive_retry_count": attempted,
    }


def _page_result(
    lines: list[dict[str, Any]],
    *,
    page_index: int,
    width: int,
    height: int,
    render_scale: float | None,
    retry_attempted: int,
    retry_accepted: int,
    page_retry: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "page_index": page_index,
        "page_number": page_index + 1,
        "lines": lines,
        "full_text": "\n".join(line["text"] for line in lines),
        "width": width,
        "height": height,
        "render_scale": render_scale,
        "adaptive_retry_attempted": retry_attempted,
        "adaptive_retry_accepted": retry_accepted,
        "page_retry": page_retry,
    }


def _run_single(
    input_path: str,
    progress_callback: ProgressCallback | None = None,
    *,
    language: str | None = None,
    document_profile: str = "auto",
) -> dict[str, Any]:
    settings = get_settings()
    selected_language = validate_language(
        language,
        ocr_version=settings.ocr_version,
        structure_version=settings.structure_ocr_version,
        default=settings.ocr_lang,
    )
    selected_profile = validate_document_profile(document_profile)
    manager = get_manager()
    document_pages: list[dict[str, Any]] = []
    emit_progress(
        progress_callback,
        stage="initializing",
        message="Loading the text recognition engine",
        completed_pages=0,
    )
    try:
        with manager.session(
            "ocr", language=selected_language, document_profile=selected_profile
        ) as engine:
            if is_pdf(input_path):
                for page in iter_pdf_pages(
                    input_path,
                    settings=settings,
                    progress_callback=progress_callback,
                ):
                    logger.info("OCR PDF page %d/%d", page.number, page.count)
                    emit_progress(
                        progress_callback,
                        stage="text_recognition",
                        message=f"Extracting text from page {page.number} of {page.count}",
                        current_page=page.number,
                        total_pages=page.count,
                        completed_pages=page.index,
                    )
                    predictions = engine.predict(page.image)
                    page_lines: list[dict[str, Any]] = []
                    result_count = 0
                    for result in predictions:
                        result_count += 1
                        page_lines.extend(
                            _parse_ocr_result(
                                result,
                                page_index=page.index,
                                page_width=page.image.shape[1],
                                page_height=page.image.shape[0],
                            )["lines"]
                        )
                    if result_count != 1:
                        raise WorkflowResultError(
                            "Text recognition returned an unexpected number of results for one PDF page"
                        )
                    page_score, retry_reasons = _page_quality(
                        page_lines, language=selected_language
                    )
                    page_retry: dict[str, Any] | None = None
                    full_page_attempted = 0
                    full_page_accepted = 0
                    selected_image = page.image
                    selected_scale = page.render_scale
                    if (
                        settings.adaptive_retry_enabled
                        and retry_reasons
                        and settings.adaptive_max_scale > page.render_scale + 0.05
                    ):
                        base_pixels = (
                            page.image.shape[0]
                            * page.image.shape[1]
                            / max(page.render_scale**2, 0.01)
                        )
                        pixel_bounded_scale = math.sqrt(
                            settings.max_image_pixels / max(1.0, base_pixels)
                        )
                        retry_scale = min(
                            settings.adaptive_max_scale,
                            2.75,
                            pixel_bounded_scale,
                        )
                    else:
                        retry_scale = page.render_scale
                    if retry_scale > page.render_scale + 0.05:
                        full_page_attempted = 1
                        emit_progress(
                            progress_callback,
                            stage="adaptive_retry",
                            message=f"Retrying source page {page.number} at {retry_scale:.2f}x",
                            current_page=page.number,
                            total_pages=page.count,
                            completed_pages=page.index,
                        )
                        retry_image = render_pdf_page(
                            input_path,
                            page.index,
                            render_scale=retry_scale,
                            settings=settings,
                        )
                        retry_predictions = list(engine.predict(retry_image))
                        if len(retry_predictions) == 1:
                            parsed_retry = _parse_ocr_result(
                                retry_predictions[0],
                                page_index=page.index,
                                page_width=retry_image.shape[1],
                                page_height=retry_image.shape[0],
                            )
                            retry_lines = parsed_retry["lines"]
                            retry_score, _ = _page_quality(
                                retry_lines, language=selected_language
                            )
                            accepted = _full_page_retry_is_better(
                                page_lines,
                                retry_lines,
                                original_score=page_score,
                                candidate_score=retry_score,
                            )
                            page_retry = {
                                "attempted": True,
                                "accepted": accepted,
                                "reason": retry_reasons,
                                "original_score": page_score,
                                "candidate_score": retry_score,
                                "original_scale": page.render_scale,
                                "candidate_scale": retry_scale,
                                "original_lines": [dict(line) for line in page_lines],
                                "candidate_lines": [dict(line) for line in retry_lines],
                            }
                            if accepted:
                                page_lines = retry_lines
                                selected_image = retry_image
                                selected_scale = retry_scale
                                full_page_accepted = 1
                    accepted_before = sum("retry_text" in line for line in page_lines)
                    retry_count = _adaptive_retry_page(
                        engine,
                        selected_image,
                        page_lines,
                        page_index=page.index,
                        progress_callback=progress_callback,
                    )
                    accepted_after = sum("retry_text" in line for line in page_lines)
                    document_pages.append(
                        _page_result(
                            page_lines,
                            page_index=page.index,
                            width=selected_image.shape[1],
                            height=selected_image.shape[0],
                            render_scale=selected_scale,
                            retry_attempted=full_page_attempted + retry_count,
                            retry_accepted=full_page_accepted
                            + max(0, accepted_after - accepted_before),
                            page_retry=page_retry,
                        )
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
                predictions = list(engine.predict(input_path))
                total_pages = len(predictions)
                for page_index, result in enumerate(predictions):
                    emit_progress(
                        progress_callback,
                        stage="text_recognition",
                        message=f"Extracting text from image {page_index + 1} of {total_pages}",
                        current_page=page_index + 1,
                        total_pages=total_pages,
                        completed_pages=page_index,
                    )
                    page_image = _load_image_page(input_path, page_index)
                    parsed = _parse_ocr_result(
                        result,
                        page_index=page_index,
                        page_width=page_image.shape[1] if page_image is not None else None,
                        page_height=page_image.shape[0] if page_image is not None else None,
                    )
                    attempted = 0
                    accepted = 0
                    if page_image is not None:
                        accepted_before = sum("retry_text" in line for line in parsed["lines"])
                        attempted = _adaptive_retry_page(
                            engine,
                            page_image,
                            parsed["lines"],
                            page_index=page_index,
                            progress_callback=progress_callback,
                        )
                        accepted = max(
                            0,
                            sum("retry_text" in line for line in parsed["lines"])
                            - accepted_before,
                        )
                    width = page_image.shape[1] if page_image is not None else 0
                    height = page_image.shape[0] if page_image is not None else 0
                    document_pages.append(
                        _page_result(
                            parsed["lines"],
                            page_index=page_index,
                            width=width,
                            height=height,
                            render_scale=None,
                            retry_attempted=attempted,
                            retry_accepted=accepted,
                        )
                    )
                    emit_progress(
                        progress_callback,
                        stage="page_complete",
                        message=f"Finished image {page_index + 1} of {total_pages}",
                        current_page=page_index + 1,
                        total_pages=total_pages,
                        completed_pages=page_index + 1,
                    )
        emit_progress(
            progress_callback,
            stage="finalizing",
            message="Assembling the text result",
            completed_pages=len(document_pages),
            total_pages=len(document_pages),
        )
        combined = _combine_pages(
            document_pages,
            language=selected_language,
            document_profile=selected_profile,
        )
        combined["adaptive_retry_count"] = combined["adaptive_retry_attempted"]
        return combined
    except Exception as exc:
        translated = translate_inference_error(exc)
        if translated is not exc:
            raise translated from exc
        raise
    finally:
        if settings.clear_gpu_cache_after_document:
            clear_gpu_cache()


def _run_native_image_batch(
    inputs: list[str],
    progress_callback: ProgressCallback | None = None,
    *,
    language: str | None = None,
    document_profile: str = "auto",
) -> list[dict[str, Any]]:
    settings = get_settings()
    selected_language = validate_language(
        language,
        ocr_version=settings.ocr_version,
        structure_version=settings.structure_ocr_version,
        default=settings.ocr_lang,
    )
    selected_profile = validate_document_profile(document_profile)
    emit_progress(
        progress_callback,
        stage="initializing",
        message="Loading the text recognition engine",
        completed_pages=0,
        total_pages=len(inputs),
    )
    try:
        with get_manager().session(
            "ocr", language=selected_language, document_profile=selected_profile
        ) as engine:
            emit_progress(
                progress_callback,
                stage="text_recognition",
                message=f"Extracting text from {len(inputs)} images",
                completed_pages=0,
                total_pages=len(inputs),
            )
            predictions = list(engine.predict(inputs))
        if len(predictions) != len(inputs):
            raise WorkflowResultError(
                "Text recognition batch result count does not match the input count"
            )
        emit_progress(
            progress_callback,
            stage="finalizing",
            message="Assembling the text results",
            completed_pages=len(inputs),
            total_pages=len(inputs),
        )
        return [
            _combine_pages(
                [_parse_ocr_result(result, page_index=0)],
                language=selected_language,
                document_profile=selected_profile,
            )
            for result in predictions
        ]
    except Exception as exc:
        translated = translate_inference_error(exc)
        if translated is not exc:
            raise translated from exc
        raise
    finally:
        if settings.clear_gpu_cache_after_document:
            clear_gpu_cache()


def run_text_recognition(
    image_input: str | Path | Sequence[str | Path],
    *,
    job_id: str | None = None,
    output_dir: str | Path | None = None,
    save_output: bool = False,
    progress_callback: ProgressCallback | None = None,
    language: str | None = None,
    document_profile: str = "auto",
) -> dict[str, Any] | list[dict[str, Any]]:
    """Recognize a document or submit image lists through PaddleOCR's batch API."""
    del job_id, output_dir, save_output
    if isinstance(image_input, (str, Path)):
        return _run_single(
            str(image_input),
            progress_callback,
            language=language,
            document_profile=document_profile,
        )

    paths = [str(path) for path in image_input]
    if not paths:
        return []
    if not any(is_pdf(path) for path in paths):
        return _run_native_image_batch(
            paths,
            progress_callback,
            language=language,
            document_profile=document_profile,
        )
    return [
        _run_single(
            path,
            progress_callback,
            language=language,
            document_profile=document_profile,
        )
        for path in paths
    ]
