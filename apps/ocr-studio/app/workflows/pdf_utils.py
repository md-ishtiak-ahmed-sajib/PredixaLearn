"""Bounded, streaming PDF rendering utilities."""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator

import numpy as np

from app.core.config import Settings, get_settings
from app.core.errors import InputLimitError, InvalidInputError, ResourceExhaustedError
from app.core.progress import ProgressCallback, emit_progress

logger = logging.getLogger("predixalearn.workflow.pdf")

try:
    import pypdfium2 as pdfium

    HAS_PYPDFIUM2 = True
except ImportError:
    pdfium = None
    HAS_PYPDFIUM2 = False


@dataclass(frozen=True, slots=True)
class RenderedPage:
    index: int
    count: int
    image: np.ndarray
    render_scale: float = 1.0

    @property
    def number(self) -> int:
        """Return the original document's human-readable, 1-based page number."""
        return self.index + 1


def is_pdf(path: str | Path) -> bool:
    return Path(path).suffix.lower() == ".pdf"


def _require_pdfium() -> None:
    if not HAS_PYPDFIUM2:
        raise InvalidInputError("pypdfium2 is required for PDF processing")


def inspect_pdf(
    path: str | Path,
    settings: Settings | None = None,
    *,
    render_scale: float | None = None,
) -> int:
    _require_pdfium()
    cfg = settings or get_settings()
    scale = render_scale if render_scale is not None else cfg.pdf_render_scale
    try:
        document = pdfium.PdfDocument(str(path))
    except Exception as exc:
        raise InvalidInputError("The uploaded PDF is invalid or unreadable") from exc
    try:
        page_count = len(document)
        if page_count < 1:
            raise InvalidInputError("The PDF contains no pages")
        if page_count > cfg.max_pdf_pages:
            raise InputLimitError(f"PDF has {page_count} pages; maximum is {cfg.max_pdf_pages}")
        for index in range(page_count):
            page = None
            try:
                page = document[index]
                width, height = page.get_size()
                pixels = math.ceil(width * scale) * math.ceil(height * scale)
                if pixels > cfg.max_image_pixels:
                    raise InputLimitError(
                        f"PDF page {index + 1} exceeds the {cfg.max_image_pixels:,}-pixel limit"
                    )
            except InputLimitError:
                raise
            except Exception as exc:
                raise InvalidInputError(f"Failed to inspect PDF page {index + 1}") from exc
            finally:
                if page is not None:
                    page.close()
        return page_count
    finally:
        document.close()


def _bitmap_to_bgr(bitmap: object) -> np.ndarray:
    array = bitmap.to_numpy()
    mode = str(getattr(bitmap, "mode", "BGR")).upper()
    if array.ndim == 2:
        array = np.repeat(array[:, :, None], 3, axis=2)
    elif mode.startswith("RGB") and not mode.startswith("BGR"):
        array = array[:, :, :3][:, :, ::-1]
    else:
        array = array[:, :, :3]
    return np.ascontiguousarray(array, dtype=np.uint8).copy()


def iter_pdf_pages(
    pdf_path: str | Path,
    *,
    settings: Settings | None = None,
    progress_callback: ProgressCallback | None = None,
    render_scale: float | None = None,
) -> Iterator[RenderedPage]:
    """Stream lossless BGR uint8 page images without intermediate image files."""
    _require_pdfium()
    cfg = settings or get_settings()
    emit_progress(
        progress_callback,
        stage="pdf_inspection",
        message="Inspecting PDF pages",
        completed_pages=0,
    )
    scale = render_scale if render_scale is not None else cfg.pdf_render_scale
    page_count = inspect_pdf(pdf_path, cfg, render_scale=scale)
    try:
        document = pdfium.PdfDocument(str(pdf_path))
    except Exception as exc:
        raise InvalidInputError("The uploaded PDF is invalid or unreadable") from exc
    try:
        for index in range(page_count):
            emit_progress(
                progress_callback,
                stage="pdf_rendering",
                message=f"Converting PDF page {index + 1} of {page_count}",
                current_page=index + 1,
                total_pages=page_count,
                completed_pages=index,
            )
            page = None
            bitmap = None
            try:
                page = document[index]
                bitmap = page.render(scale=scale)
                image = _bitmap_to_bgr(bitmap)
            except Exception as exc:
                raise InvalidInputError(f"Failed to render PDF page {index + 1}") from exc
            finally:
                if bitmap is not None:
                    bitmap.close()
                if page is not None:
                    page.close()
            yield RenderedPage(index=index, count=page_count, image=image, render_scale=scale)
            del image
    finally:
        document.close()


def render_pdf_page(
    pdf_path: str | Path,
    page_index: int,
    *,
    render_scale: float,
    settings: Settings | None = None,
) -> np.ndarray:
    """Render one bounded page for an adaptive full-page retry."""
    _require_pdfium()
    cfg = settings or get_settings()
    page_count = inspect_pdf(pdf_path, cfg, render_scale=render_scale)
    if page_index < 0 or page_index >= page_count:
        raise InvalidInputError("The requested PDF page is outside the document")
    document = pdfium.PdfDocument(str(pdf_path))
    page = None
    bitmap = None
    try:
        page = document[page_index]
        bitmap = page.render(scale=render_scale)
        image = _bitmap_to_bgr(bitmap)
        if image.shape[0] * image.shape[1] > cfg.max_image_pixels:
            raise InputLimitError(
                f"PDF page {page_index + 1} exceeds the {cfg.max_image_pixels:,}-pixel limit"
            )
        return image
    except (InputLimitError, InvalidInputError):
        raise
    except Exception as exc:
        raise InvalidInputError(f"Failed to render PDF page {page_index + 1}") from exc
    finally:
        if bitmap is not None:
            bitmap.close()
        if page is not None:
            page.close()
        document.close()


def clear_gpu_cache() -> None:
    try:
        import paddle

        if paddle.device.is_compiled_with_cuda():
            paddle.device.cuda.empty_cache()
    except (ImportError, RuntimeError):
        logger.debug("CUDA cache cleanup unavailable", exc_info=True)


def translate_inference_error(exc: Exception) -> Exception:
    message = str(exc).lower()
    if "out of memory" in message or "resource exhausted" in message:
        clear_gpu_cache()
        return ResourceExhaustedError(
            "GPU memory was exhausted; reduce PDF scale or document complexity"
        )
    return exc
