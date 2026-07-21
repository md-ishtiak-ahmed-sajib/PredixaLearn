"""Non-blocking OCR engine warmup state shared with health reporting."""

from __future__ import annotations

import logging
import threading
from datetime import datetime, timezone
from typing import Any, Callable

from app.core.engine import get_manager

logger = logging.getLogger("predixalearn.warmup")
_lock = threading.Lock()
_thread: threading.Thread | None = None
_state: dict[str, Any] = {
    "status": "not_started",
    "message": "OCR engine warmup has not started",
    "started_at": None,
    "completed_at": None,
}


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


def _run(manager_factory: Callable[[], Any]) -> None:
    try:
        with manager_factory().session("ocr"):
            pass
    except Exception:
        logger.exception("OCR engine background warmup failed")
        with _lock:
            _state.update(
                status="failed",
                message="OCR engine initialization failed; see diagnostics for details",
                completed_at=_utcnow(),
            )
    else:
        with _lock:
            _state.update(
                status="ready",
                message="OCR engine ready",
                completed_at=_utcnow(),
            )


def start_engine_warmup(manager_factory: Callable[[], Any] = get_manager) -> None:
    global _thread
    with _lock:
        if _thread is not None and _thread.is_alive():
            return
        if _state["status"] == "ready":
            return
        _state.update(
            status="initializing",
            message="Preparing the OCR engine",
            started_at=_utcnow(),
            completed_at=None,
        )
        _thread = threading.Thread(
            target=_run,
            args=(manager_factory,),
            daemon=True,
            name="ocr-engine-warmup",
        )
        _thread.start()


def get_warmup_status() -> dict[str, Any]:
    with _lock:
        return dict(_state)


def reset_warmup() -> None:
    global _thread
    with _lock:
        _thread = None
        _state.update(
            status="not_started",
            message="OCR engine warmup has not started",
            started_at=None,
            completed_at=None,
        )
