"""Desktop-host control routes kept separate from the OCR data API."""

from __future__ import annotations

import hmac
from collections.abc import Callable

from fastapi import APIRouter, Header, HTTPException, Request, status

from app.core.config import get_settings
from app.core.queue import get_queue

router = APIRouter(prefix="/api/v1/app", tags=["desktop"])


@router.post("/quit", status_code=status.HTTP_202_ACCEPTED)
async def quit_desktop_application(
    request: Request,
    x_predixalearn_control: str | None = Header(default=None),
) -> dict[str, str]:
    """Request graceful termination from the trusted desktop host only.

    The route is intentionally absent in normal development/server launches.
    The random token is issued by the local launcher and is never written to
    logs or persisted to browser storage.
    """
    settings = get_settings()
    expected = settings.desktop_control_token
    if not settings.desktop_mode or not expected:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
    if not x_predixalearn_control or not hmac.compare_digest(x_predixalearn_control, expected):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Desktop control denied")
    if get_queue().has_active_jobs:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Finish or cancel active OCR work before quitting PredixaLearn.",
        )
    callback = getattr(request.app.state, "request_desktop_shutdown", None)
    if not isinstance(callback, Callable):
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Desktop shutdown is not available in this launch.",
        )
    if callback() is False:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Finish or cancel active OCR work before quitting PredixaLearn.",
        )
    return {"status": "shutting_down"}
