"""Small, optional progress contract shared by workflows and the job queue."""

from __future__ import annotations

import logging
from collections.abc import Callable

logger = logging.getLogger("predixalearn.progress")

ProgressCallback = Callable[..., None]


class ProcessingCancelled(RuntimeError):
    """Raised at a safe workflow checkpoint after a user cancellation request."""


def emit_progress(
    callback: ProgressCallback | None,
    *,
    stage: str,
    message: str,
    current_page: int | None = None,
    total_pages: int | None = None,
    completed_pages: int | None = None,
) -> None:
    """Publish best-effort progress without making reporting part of OCR correctness."""
    if callback is None:
        return
    try:
        callback(
            stage=stage,
            message=message,
            current_page=current_page,
            total_pages=total_pages,
            completed_pages=completed_pages,
        )
    except ProcessingCancelled:
        raise
    except Exception:
        logger.warning("Progress callback failed", exc_info=True)
