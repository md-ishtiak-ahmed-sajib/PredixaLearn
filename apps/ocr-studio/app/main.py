"""FastAPI application factory for the single local UI/API server."""

from __future__ import annotations

import hashlib
import logging
import shutil
from contextlib import asynccontextmanager
from logging.handlers import RotatingFileHandler
from urllib.parse import urlsplit

from fastapi import FastAPI, Request
from fastapi.openapi.docs import get_swagger_ui_html
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.middleware.trustedhost import TrustedHostMiddleware

from app import __version__
from app.api.diagnostics import router as diagnostics_router
from app.api.education import router as education_router
from app.api.history import router as history_router
from app.api.institution import router as institution_router
from app.api.maintenance import router as maintenance_router
from app.api.ocr import health_check
from app.api.ocr import router as api_router
from app.api.runtime import router as runtime_router
from app.api.teaching import router as teaching_router
from app.core.config import get_settings
from app.core.data_migration import create_runtime_directories, migrate_legacy_runtime_data
from app.core.engine import get_manager, reset_manager
from app.core.errors import (
    InputLimitError,
    InvalidInputError,
    OCRServiceError,
    QueueFullError,
    ResourceExhaustedError,
    UnsupportedInputError,
    WorkflowResultError,
)
from app.core.queue import reset_queue
from app.core.warmup import reset_warmup, start_engine_warmup
from app.documents.libreoffice import start_libreoffice_probe
from app.maintenance.service import reset_maintenance_service
from app.storage.history import get_history_store, reset_history_store
from app.teaching.batches import start_batch_coordinator, stop_batch_coordinator

logger = logging.getLogger("predixalearn")


def _configure_logging() -> None:
    settings = get_settings()
    settings.log_dir.mkdir(parents=True, exist_ok=True)
    root = logging.getLogger()
    if any(getattr(handler, "_predixalearn_handler", False) for handler in root.handlers):
        return
    formatter = logging.Formatter(
        "%(asctime)s %(levelname)-8s %(name)s %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    stream = logging.StreamHandler()
    file_handler = RotatingFileHandler(
        settings.log_dir / "predixalearn.log",
        maxBytes=5 * 1024 * 1024,
        backupCount=3,
        encoding="utf-8",
    )
    for handler in (stream, file_handler):
        handler.setFormatter(formatter)
        handler._predixalearn_handler = True  # type: ignore[attr-defined]
        root.addHandler(handler)
    root.setLevel(logging.INFO)


def _migrate_legacy_logs() -> None:
    settings = get_settings()
    settings.log_dir.mkdir(parents=True, exist_ok=True)
    for source in settings.output_dir.glob("*.log"):
        destination = settings.log_dir / source.name
        if destination.exists():
            destination = settings.log_dir / f"legacy-{source.name}"
        try:
            # Keep the legacy log in place so a failed migration is recoverable.
            shutil.copy2(source, destination)
        except OSError:
            logger.warning("Could not move legacy log %s", source.name)


class BrowserSecurityMiddleware(BaseHTTPMiddleware):
    def __init__(self, app: FastAPI) -> None:
        super().__init__(app)
        settings = get_settings()
        port = settings.port
        self.allowed_origins = {
            f"http://127.0.0.1:{port}",
            f"http://localhost:{port}",
            f"http://[::1]:{port}",
        }
        if settings.desktop_mode:
            self.allowed_origins = {settings.browser_origin}

    async def dispatch(self, request: Request, call_next):
        origin = request.headers.get("origin")
        if origin and request.method not in {"GET", "HEAD", "OPTIONS"}:
            normalized = urlsplit(origin)
            candidate = f"{normalized.scheme}://{normalized.netloc}"
            if candidate not in self.allowed_origins:
                response = JSONResponse(status_code=403, content={"detail": "Origin not allowed"})
            else:
                response = await call_next(request)
        else:
            response = await call_next(request)
        if request.url.path.startswith(("/docs", "/openapi.json")):
            csp = (
                "default-src 'self'; script-src 'self' 'unsafe-inline'; "
                "style-src 'self' 'unsafe-inline'; img-src 'self' data:; object-src 'none'; "
                "base-uri 'self'; frame-ancestors 'none'"
            )
        else:
            csp = (
                "default-src 'self'; script-src 'self'; style-src 'self'; "
                "img-src 'self' data: blob:; connect-src 'self'; object-src 'none'; "
                "base-uri 'self'; form-action 'self'; frame-ancestors 'none'"
            )
        response.headers["Content-Security-Policy"] = csp
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
        response.headers["Cross-Origin-Opener-Policy"] = "same-origin"
        response.headers["Cross-Origin-Resource-Policy"] = "same-origin"
        if request.url.path.startswith("/static/"):
            response.headers["Cache-Control"] = (
                "public, max-age=31536000, immutable"
                if request.query_params.get("v")
                else "no-cache"
            )
        else:
            response.headers["Cache-Control"] = "no-store"
        return response


@asynccontextmanager
async def lifespan(app: FastAPI):
    del app
    settings = get_settings()
    migrate_legacy_runtime_data(settings)
    create_runtime_directories(settings)
    _migrate_legacy_logs()
    for stale_upload in settings.upload_dir.iterdir():
        if stale_upload.is_file():
            stale_upload.unlink(missing_ok=True)
    history_store = get_history_store()
    history_store.initialize()
    interrupted = history_store.recover_interrupted()
    if interrupted:
        logger.warning("Marked %d interrupted OCR history record(s) as failed", interrupted)
    logger.info(
        "Starting PredixaLearn Service v%s on %s:%d", __version__, settings.host, settings.port
    )
    start_engine_warmup(get_manager)
    start_libreoffice_probe(settings)
    start_batch_coordinator()
    yield
    logger.info("Shutting down PredixaLearn Service")
    await stop_batch_coordinator()
    reset_queue()
    reset_manager()
    reset_warmup()
    reset_history_store()
    reset_maintenance_service()


def _error_status(exc: OCRServiceError) -> int:
    if isinstance(exc, InputLimitError):
        return 413
    if isinstance(exc, UnsupportedInputError):
        return 415
    if isinstance(exc, InvalidInputError):
        return 422
    if isinstance(exc, QueueFullError):
        return 429
    if isinstance(exc, WorkflowResultError):
        return 502
    if isinstance(exc, ResourceExhaustedError):
        return 503
    return 400


def create_app() -> FastAPI:
    _configure_logging()
    settings = get_settings()
    web_root = settings.project_root / "web"
    static_root = web_root / "static"
    versioned_assets: dict[str, str] = {}
    for asset_name in (
        "app.css",
        "app.js",
        "common.js",
        "history.js",
        "maintenance.js",
        "desktop.js",
        "analysis.js",
        "review.js",
        "teacher.js",
        "revision.js",
        "tips.js",
        "predixalearn-mark-32.png",
        "predixalearn-mark-48.png",
        "predixalearn-mark-180.png",
    ):
        asset_path = static_root / asset_name
        if asset_path.is_file():
            versioned_assets[asset_name] = hashlib.sha256(asset_path.read_bytes()).hexdigest()[:12]
    templates = Jinja2Templates(directory=web_root / "templates")
    app = FastAPI(
        title="PredixaLearn Local Document Service",
        description="Local PP-OCRv6, PP-StructureV3, and PaddleOCR-VL workflows.",
        version=__version__,
        lifespan=lifespan,
        docs_url=None,
        redoc_url=None,
    )
    app.add_middleware(
        TrustedHostMiddleware,
        allowed_hosts=["127.0.0.1", "localhost", "[::1]"]
        + ([settings.desktop_hostname] if settings.desktop_mode else []),
    )
    app.add_middleware(BrowserSecurityMiddleware)
    app.include_router(api_router)
    app.include_router(history_router)
    app.include_router(diagnostics_router)
    app.include_router(education_router)
    app.include_router(institution_router)
    app.include_router(maintenance_router)
    app.include_router(runtime_router)
    app.include_router(teaching_router)
    app.state.request_desktop_shutdown = None
    app.add_api_route("/health", health_check, methods=["GET"], include_in_schema=False)
    app.mount("/static", StaticFiles(directory=static_root), name="static")

    @app.get("/docs", include_in_schema=False)
    async def local_api_docs() -> HTMLResponse:
        docs_version = versioned_assets.get("app.js", __version__)
        favicon_version = versioned_assets.get("predixalearn-mark-32.png", __version__)
        return get_swagger_ui_html(
            openapi_url=app.openapi_url,
            title=f"{app.title} - API docs",
            swagger_js_url=f"/static/vendor/swagger-ui-bundle.js?v={docs_version}",
            swagger_css_url=f"/static/vendor/swagger-ui.css?v={docs_version}",
            swagger_favicon_url=(
                f"/static/predixalearn-mark-32.png?v={favicon_version}"
            ),
        )

    @app.exception_handler(OCRServiceError)
    async def expected_error_handler(request: Request, exc: OCRServiceError):
        del request
        return JSONResponse(status_code=_error_status(exc), content={"detail": str(exc)})

    @app.exception_handler(Exception)
    async def unexpected_error_handler(request: Request, exc: Exception):
        logger.exception("Unhandled request failure for %s", request.url.path, exc_info=exc)
        return JSONResponse(status_code=500, content={"detail": "Internal server error"})

    def template_context(request: Request, page: str) -> dict[str, object]:
        return {
            "request": request,
            "page": page,
            "max_upload_mb": settings.max_upload_bytes // (1024 * 1024),
            "max_pdf_pages": settings.max_pdf_pages,
            "max_image_megapixels": settings.max_image_pixels / 1_000_000,
            "pdf_render_scale": settings.pdf_render_scale,
            "device": settings.device,
            "correction_enabled": settings.correction_enabled,
            "crop_padding_pixels": settings.crop_padding_pixels,
            "max_embedded_images": settings.max_embedded_images,
            "adaptive_max_scale": settings.adaptive_max_scale,
            "max_retry_regions_per_page": settings.max_retry_regions_per_page,
            "asset_versions": versioned_assets,
            "desktop_mode": settings.desktop_mode,
            "desktop_control_token": settings.desktop_control_token or "",
        }

    @app.get("/", response_class=HTMLResponse, include_in_schema=False)
    async def root(request: Request) -> HTMLResponse:
        return templates.TemplateResponse(
            request=request,
            name="index.html",
            context=template_context(request, "home"),
        )

    @app.get("/tips", response_class=HTMLResponse, include_in_schema=False)
    async def tips(request: Request) -> HTMLResponse:
        return templates.TemplateResponse(
            request=request,
            name="tips.html",
            context=template_context(request, "tips"),
        )

    @app.get("/history", response_class=HTMLResponse, include_in_schema=False)
    async def history(request: Request) -> HTMLResponse:
        return templates.TemplateResponse(
            request=request,
            name="history.html",
            context=template_context(request, "history"),
        )

    @app.get("/analyze", response_class=HTMLResponse, include_in_schema=False)
    async def analyze(request: Request) -> HTMLResponse:
        return templates.TemplateResponse(
            request=request,
            name="analyze.html",
            context=template_context(request, "analyze"),
        )

    @app.get("/benchmark", response_class=HTMLResponse, include_in_schema=False)
    async def benchmark(request: Request) -> HTMLResponse:
        return templates.TemplateResponse(
            request=request,
            name="benchmark.html",
            context=template_context(request, "benchmark"),
        )

    @app.get("/review", response_class=HTMLResponse, include_in_schema=False)
    async def review(request: Request) -> HTMLResponse:
        return templates.TemplateResponse(
            request=request,
            name="review.html",
            context=template_context(request, "review"),
        )

    @app.get("/teacher", response_class=HTMLResponse, include_in_schema=False)
    async def teacher(request: Request) -> HTMLResponse:
        return templates.TemplateResponse(
            request=request,
            name="teacher.html",
            context=template_context(request, "teacher"),
        )

    @app.get("/revision", response_class=HTMLResponse, include_in_schema=False)
    async def revision(request: Request) -> HTMLResponse:
        return templates.TemplateResponse(
            request=request,
            name="revision.html",
            context=template_context(request, "revision"),
        )

    return app
