"""PredixaLearn institution service application factory."""

from __future__ import annotations

import asyncio
import logging
import re
import time
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import urlsplit

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy import text
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.middleware.trustedhost import TrustedHostMiddleware

from . import __version__
from .accessibility import router as accessibility_router
from .answer_analysis import router as answer_analysis_router
from .api import router as api_router
from .api_clients import router as api_client_router
from .artifacts import router as artifact_router
from .catalog import router as catalog_router
from .config import get_settings
from .curriculum import router as curriculum_router
from .database import SessionLocal, create_schema
from .datasets import router as dataset_router
from .events import router as event_router
from .identities import router as identity_router
from .lti import router as lti_router
from .portal_auth import router as auth_router
from .retention import router as retention_router
from .storage import create_storage
from .sync import router as sync_router
from .teaching import router as teaching_router
from .webhooks import delivery_loop
from .workers import router as worker_router

REQUEST_ID = re.compile(r"^[A-Za-z0-9._:-]{8,128}$")
logger = logging.getLogger(__name__)

FEATURE_PATHS = {
    "archive_review": ("/api/v2/archive", "/api/v2/search", "/api/v2/reviews"),
    "curriculum": ("/api/v2/curricula", "/api/v2/rubrics", "/api/v2/objective"),
    "answer_scripts": ("/api/v2/answer-", "/api/v2/feedback", "/api/v2/moderation"),
    "accessibility": ("/api/v2/figures", "/api/v2/accessibility"),
    "datasets": ("/api/v2/datasets", "/api/v2/dataset-"),
    "workers": ("/api/v2/workers", "/api/v2/worker-jobs", "/api/v2/sync"),
    "integrations": ("/api/v2/webhooks",),
    "lti": ("/api/v2/lti",),
    "teaching_workspace": ("/api/v2/teacher", "/api/v2/student", "/api/v2/question-bank", "/api/v2/revision-packs", "/api/v2/revision-progress", "/api/v2/corrections", "/api/v2/taxonomies", "/api/v2/syllabi", "/api/v2/comparisons"),
}


class SecurityMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        supplied = request.headers.get("X-Request-ID", "")
        request.state.request_id = supplied if REQUEST_ID.fullmatch(supplied) else str(uuid.uuid4())
        settings = get_settings()
        for feature, prefixes in FEATURE_PATHS.items():
            if feature not in settings.enabled_features and request.url.path.startswith(prefixes):
                return JSONResponse(
                    status_code=404,
                    content={
                        "detail": {
                            "code": "feature_disabled",
                            "message": "This institutional feature is not enabled for the tenant ring",
                        }
                    },
                    headers={"X-Request-ID": request.state.request_id, "Cache-Control": "no-store"},
                )
        try:
            response = await call_next(request)
        except Exception:
            raise
        response.headers["X-Request-ID"] = request.state.request_id
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
            "connect-src 'self'; object-src 'none'; base-uri 'self'; frame-ancestors 'none'"
        )
        response.headers["Cache-Control"] = "no-store"
        return response


@asynccontextmanager
async def lifespan(_app: FastAPI):
    settings = get_settings()
    if settings.environment != "production":
        create_schema()
    stop = asyncio.Event()
    webhook_task = (
        asyncio.create_task(delivery_loop(stop)) if settings.webhook_signing_secret else None
    )
    yield
    stop.set()
    if webhook_task:
        await webhook_task


def create_app() -> FastAPI:
    settings = get_settings()
    root = Path(__file__).resolve().parent
    templates = Jinja2Templates(directory=root / "web" / "templates")
    started_at = time.monotonic()
    app = FastAPI(
        title="PredixaLearn Institution API",
        version=__version__,
        description="Institution-controlled archive, review, curriculum, dataset, worker, and LMS services.",
        lifespan=lifespan,
    )
    hostname = urlsplit(settings.public_base_url).hostname or "localhost"
    app.add_middleware(
        TrustedHostMiddleware,
        allowed_hosts=[hostname, "127.0.0.1", "localhost", "testserver"],
    )
    app.add_middleware(SecurityMiddleware)
    app.include_router(auth_router)
    app.include_router(api_client_router)
    app.include_router(api_router)
    app.include_router(answer_analysis_router)
    app.include_router(accessibility_router)
    app.include_router(curriculum_router)
    app.include_router(dataset_router)
    app.include_router(artifact_router)
    app.include_router(event_router)
    app.include_router(lti_router)
    app.include_router(identity_router)
    app.include_router(retention_router)
    app.include_router(worker_router)
    app.include_router(sync_router)
    app.include_router(teaching_router)
    # The catalog router intentionally comes last because it provides the
    # bounded collection fallback for organization/course resources.
    app.include_router(catalog_router)
    app.mount("/static", StaticFiles(directory=root / "web" / "static"), name="static")

    @app.get("/", response_class=HTMLResponse, include_in_schema=False)
    async def landing(request: Request) -> HTMLResponse:
        return templates.TemplateResponse(
            request=request,
            name="landing.html",
            context={"version": __version__, "oidc_ready": settings.auth_mode == "oidc"},
        )

    @app.get("/portal", response_class=HTMLResponse, include_in_schema=False)
    async def portal(request: Request) -> HTMLResponse:
        return templates.TemplateResponse(
            request=request,
            name="portal.html",
            context={"version": __version__},
        )

    @app.get("/revision", response_class=HTMLResponse, include_in_schema=False)
    async def revision_dashboard(request: Request) -> HTMLResponse:
        return templates.TemplateResponse(
            request=request,
            name="revision.html",
            context={"version": __version__},
        )

    @app.get("/health")
    async def health() -> dict:
        components: dict[str, object] = {}
        healthy = True
        try:
            with SessionLocal() as session:
                session.execute(text("SELECT 1"))
            components["database"] = {"ready": True}
        except Exception:
            components["database"] = {"ready": False}
            healthy = False
        try:
            storage = create_storage(settings)
            storage.healthcheck()
            components["storage"] = {
                "ready": True,
                "provider": settings.storage_provider,
                "adapter": storage.__class__.__name__,
            }
        except Exception:
            components["storage"] = {"ready": False, "provider": settings.storage_provider}
            healthy = False
        if settings.redis_url:
            try:
                import redis

                redis_client = redis.Redis.from_url(
                    settings.redis_url, socket_timeout=1, socket_connect_timeout=1
                )
                redis_client.ping()
                redis_client.close()
                components["redis"] = {
                    "ready": True,
                    "required": settings.environment == "production",
                }
            except Exception:
                components["redis"] = {
                    "ready": False,
                    "required": settings.environment == "production",
                }
                if settings.environment == "production":
                    healthy = False
        components["semantic_search"] = {
            "enabled": settings.semantic_search_enabled,
            "external_calls": False,
        }
        return {
            "status": "ready" if healthy else "degraded",
            "ready": healthy,
            "version": __version__,
            "components": components,
            "enabled_features": sorted(settings.enabled_features),
        }

    @app.get("/metrics", response_class=PlainTextResponse, include_in_schema=False)
    async def metrics() -> PlainTextResponse:
        """Document-content-free operational metrics for institution monitoring."""

        uptime = max(0.0, time.monotonic() - started_at)
        body = (
            "# HELP predixalearn_institution_info Build information.\n"
            "# TYPE predixalearn_institution_info gauge\n"
            f'predixalearn_institution_info{{version="{__version__}"}} 1\n'
            "# HELP predixalearn_institution_uptime_seconds Process uptime.\n"
            "# TYPE predixalearn_institution_uptime_seconds gauge\n"
            f"predixalearn_institution_uptime_seconds {uptime:.3f}\n"
        )
        return PlainTextResponse(body, media_type="text/plain; version=0.0.4")

    @app.exception_handler(Exception)
    async def unhandled(request: Request, exc: Exception):
        logger.exception(
            "Unhandled institution request error request_id=%s",
            request.state.request_id,
            exc_info=exc,
        )
        return JSONResponse(
            status_code=500,
            content={
                "detail": {
                    "code": "internal_error",
                    "message": "Institution service request failed safely",
                    "request_id": str(request.state.request_id),
                }
            },
        )

    return app


app = create_app()
