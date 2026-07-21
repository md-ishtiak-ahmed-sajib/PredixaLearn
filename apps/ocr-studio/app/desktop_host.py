"""Windows desktop host for PredixaLearn's browser-only local application.

The tray icon is a launcher/control surface, not a second product UI. OCR and
all user-facing work remain in the default browser at the local HTTPS origin.
"""

from __future__ import annotations

import threading
import time
import webbrowser
from pathlib import Path
from typing import Any

from app.core.config import Settings, get_settings
from app.core.queue import get_queue
from app.core.single_instance import (
    ServerInstanceLock,
    port_is_occupied,
    probe_local_service,
    wait_for_existing_service,
)


def _tray_image(settings: Settings) -> Any:
    """Load the existing product mark without introducing a native window."""
    from PIL import Image

    path = settings.project_root / "web" / "static" / "predixalearn-mark-48.png"
    with Image.open(path) as source:
        return source.convert("RGBA").copy()


def _validate_desktop_launch(settings: Settings) -> tuple[Path, Path]:
    if not settings.desktop_mode:
        raise RuntimeError("The desktop host requires OCR_DESKTOP_MODE=1")
    certificate = settings.desktop_tls_cert_path
    key = settings.desktop_tls_key_path
    if not certificate or not certificate.is_file() or not key or not key.is_file():
        raise RuntimeError("The local HTTPS certificate is missing or unreadable")
    return certificate, key


def _wait_for_start(server: Any, timeout_seconds: float) -> bool:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        if getattr(server, "started", False):
            return True
        if getattr(server, "should_exit", False):
            return False
        time.sleep(0.1)
    return False


def run_desktop_host(*, open_browser: bool = True) -> None:
    """Serve the local HTTPS app and provide a Windows notification-area menu."""
    settings = get_settings()
    certificate, key = _validate_desktop_launch(settings)
    import uvicorn

    from app.main import create_app

    health_url = f"{settings.browser_origin}/api/v1/health"
    instance_lock = ServerInstanceLock(settings.log_dir / "predixalearn.lock")
    if not instance_lock.acquire():
        state = wait_for_existing_service(
            health_url,
            timeout_seconds=settings.browser_ready_timeout_seconds,
            poll_seconds=settings.browser_poll_interval_seconds,
            request_timeout_seconds=settings.browser_request_timeout_seconds,
        )
        if state == "predixalearn":
            if open_browser:
                webbrowser.open(settings.browser_origin)
            return
        raise SystemExit("Another PredixaLearn launch is still starting or did not become healthy.")

    try:
        state = probe_local_service(
            health_url,
            timeout_seconds=settings.browser_request_timeout_seconds,
        )
        if state == "predixalearn":
            if open_browser:
                webbrowser.open(settings.browser_origin)
            return
        if state == "unknown_http" or port_is_occupied(
            settings.host,
            settings.port,
            timeout_seconds=settings.browser_request_timeout_seconds,
        ):
            raise SystemExit(
                "HTTPS port 443 is already in use. PredixaLearn will not fall back to an insecure URL."
            )

        application = create_app()
        server = uvicorn.Server(
            uvicorn.Config(
                application,
                host=settings.host,
                port=settings.port,
                ssl_certfile=str(certificate),
                ssl_keyfile=str(key),
                log_level="info",
                access_log=False,
            )
        )
        tray: list[Any] = []

        def request_shutdown() -> bool:
            if get_queue().has_active_jobs:
                return False
            server.should_exit = True
            if tray:
                tray[0].stop()
            return True

        application.state.request_desktop_shutdown = request_shutdown
        worker = threading.Thread(target=server.run, name="predixalearn-local-server", daemon=False)
        worker.start()
        if not _wait_for_start(server, settings.browser_ready_timeout_seconds):
            request_shutdown()
            worker.join(timeout=5)
            raise SystemExit("PredixaLearn local HTTPS service did not start.")
        if wait_for_existing_service(
            health_url,
            timeout_seconds=settings.browser_ready_timeout_seconds,
            poll_seconds=settings.browser_poll_interval_seconds,
            request_timeout_seconds=settings.browser_request_timeout_seconds,
        ) != "predixalearn":
            request_shutdown()
            worker.join(timeout=5)
            raise SystemExit("PredixaLearn local HTTPS health checks did not become available.")
        if open_browser:
            webbrowser.open(settings.browser_origin)

        try:
            import pystray
        except ImportError as exc:  # pragma: no cover - package is release-required.
            request_shutdown()
            worker.join(timeout=5)
            raise RuntimeError("The PredixaLearn desktop tray runtime is missing") from exc

        def open_app(_: Any = None, __: Any = None) -> None:
            webbrowser.open(settings.browser_origin)

        def exit_app(_: Any = None, __: Any = None) -> None:
            if not request_shutdown() and tray:
                tray[0].notify("Finish or cancel active OCR work before quitting PredixaLearn.")

        icon = pystray.Icon(
            "PredixaLearnOCR",
            _tray_image(settings),
            "PredixaLearn",
            pystray.Menu(
                pystray.MenuItem("Open PredixaLearn", open_app, default=True),
                pystray.MenuItem("Exit", exit_app),
            ),
        )
        tray.append(icon)
        icon.run()
        request_shutdown()
        worker.join(timeout=30)
        if worker.is_alive():
            server.force_exit = True
            worker.join(timeout=10)
    finally:
        instance_lock.release()
