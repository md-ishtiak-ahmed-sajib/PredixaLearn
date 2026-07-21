"""Small, redacted diagnostics bundle for local support and release checks."""

from __future__ import annotations

import json
import os
import re
import tempfile
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fastapi import APIRouter
from fastapi.responses import FileResponse
from starlette.background import BackgroundTask

from app import __version__
from app.api.ocr import health_check
from app.core.config import get_settings

router = APIRouter(prefix="/api/v1", tags=["diagnostics"])
_LOG_PREFIX = re.compile(
    r"^(?P<timestamp>\d{4}-\d{2}-\d{2}[^ ]* [^ ]+)\s+"
    r"(?P<level>DEBUG|INFO|WARNING|ERROR|CRITICAL)\s+(?P<logger>[^ ]+)"
)


def _safe_log_excerpt(log_dir: Path, limit: int = 200) -> list[str]:
    lines: list[str] = []
    for path in sorted(log_dir.glob("*.log")):
        try:
            content = path.read_text(encoding="utf-8", errors="replace").splitlines()[-limit:]
        except OSError:
            continue
        for line in content:
            match = _LOG_PREFIX.match(line)
            if match:
                lines.append(
                    f"{match.group('timestamp')} {match.group('level')} "
                    f"{match.group('logger')} <message redacted>"
                )
            else:
                lines.append("<non-structured log line redacted>")
    return lines[-limit:]


@router.post("/diagnostics", response_class=FileResponse)
async def download_diagnostics() -> FileResponse:
    settings = get_settings()
    health = await health_check()
    payload: dict[str, Any] = {
        "schema_version": 1,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "service_version": __version__,
        "health": health,
        "configuration": {
            "device": settings.device,
            "ocr_version": settings.ocr_version,
            "structure_ocr_version": settings.structure_ocr_version,
            "max_upload_bytes": settings.max_upload_bytes,
            "max_pdf_pages": settings.max_pdf_pages,
            "max_image_pixels": settings.max_image_pixels,
            "max_queued_jobs": settings.max_queued_jobs,
            "paths": {
                "output": settings.output_dir.name,
                "uploads": settings.upload_dir.name,
                "history": settings.history_dir.name,
                "logs": settings.log_dir.name,
            },
        },
        "component_timings": {},
    }
    descriptor, temporary_name = tempfile.mkstemp(prefix="predixalearn-diagnostics-", suffix=".zip")
    os.close(descriptor)
    temporary = Path(temporary_name)
    try:
        with zipfile.ZipFile(temporary, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            archive.writestr("diagnostics.json", json.dumps(payload, indent=2, ensure_ascii=False))
            archive.writestr("logs/redacted-excerpt.txt", "\n".join(_safe_log_excerpt(settings.log_dir)))
    except Exception:
        try:
            temporary.unlink(missing_ok=True)  # noqa: ASYNC240
        except OSError:
            pass
        raise
    return FileResponse(
        temporary,
        media_type="application/zip",
        filename="predixalearn-diagnostics.zip",
        background=BackgroundTask(temporary.unlink, missing_ok=True),
    )
