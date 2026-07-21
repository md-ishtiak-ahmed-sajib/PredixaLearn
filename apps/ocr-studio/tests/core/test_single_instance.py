from __future__ import annotations

import json
import socket
import sys
import threading
from dataclasses import replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from app import cli
from app.core import single_instance
from app.core.config import get_settings


class _HealthHandler(BaseHTTPRequestHandler):
    service_id = single_instance.SERVICE_ID

    def do_GET(self) -> None:  # noqa: N802
        payload = json.dumps({"service_id": self.service_id}).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, *_: object) -> None:
        return


def _serve_health(service_id: str):
    handler = type("HealthHandler", (_HealthHandler,), {"service_id": service_id})
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, thread


def test_project_lock_is_exclusive_and_recovers_after_release(tmp_path: Path) -> None:
    lock_path = tmp_path / "logs" / "predixalearn.lock"
    first = single_instance.ServerInstanceLock(lock_path)
    second = single_instance.ServerInstanceLock(lock_path)

    assert first.acquire() is True
    assert second.acquire() is False
    first.release()
    assert second.acquire() is True
    second.release()


def test_health_probe_identifies_only_predixalearn() -> None:
    server, thread = _serve_health(single_instance.SERVICE_ID)
    try:
        url = f"http://127.0.0.1:{server.server_port}/health"
        assert single_instance.probe_local_service(url, timeout_seconds=1) == "predixalearn"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)

    server, thread = _serve_health("unrelated-local-service")
    try:
        url = f"http://127.0.0.1:{server.server_port}/health"
        assert single_instance.probe_local_service(url, timeout_seconds=1) == "unknown_http"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_port_probe_does_not_modify_unknown_listener() -> None:
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    try:
        port = listener.getsockname()[1]
        assert single_instance.port_is_occupied(
            "127.0.0.1", port, timeout_seconds=1
        )
        assert listener.fileno() >= 0
    finally:
        listener.close()


def test_duplicate_cli_launch_reuses_service_before_importing_uvicorn(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    settings = replace(
        get_settings(),
        log_dir=tmp_path / "logs",
        port=49152,
        browser_ready_timeout_seconds=0.05,
        browser_request_timeout_seconds=0.01,
        browser_poll_interval_seconds=0.01,
    )

    class HeldLock:
        def __init__(self, path: Path) -> None:
            self.path = path

        def acquire(self) -> bool:
            return False

        def release(self) -> None:
            return

    monkeypatch.setattr("app.core.config.get_settings", lambda: settings)
    monkeypatch.setattr(single_instance, "ServerInstanceLock", HeldLock)
    monkeypatch.setattr(
        single_instance, "wait_for_existing_service", lambda *args, **kwargs: "predixalearn"
    )
    original_uvicorn = sys.modules.pop("uvicorn", None)
    try:
        cli._run_server(open_browser=False)
        assert "uvicorn" not in sys.modules
    finally:
        if original_uvicorn is not None:
            sys.modules["uvicorn"] = original_uvicorn

    assert "already running" in capsys.readouterr().out


def test_unknown_port_owner_is_not_terminated(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = replace(get_settings(), log_dir=tmp_path / "logs", port=49153)

    class AvailableLock:
        released = False

        def __init__(self, path: Path) -> None:
            self.path = path

        def acquire(self) -> bool:
            return True

        def release(self) -> None:
            self.released = True

    lock = AvailableLock(tmp_path / "unused")
    monkeypatch.setattr("app.core.config.get_settings", lambda: settings)
    monkeypatch.setattr(single_instance, "ServerInstanceLock", lambda path: lock)
    monkeypatch.setattr(
        single_instance, "probe_local_service", lambda *args, **kwargs: "unknown_http"
    )

    with pytest.raises(SystemExit, match="another local application"):
        cli._run_server(open_browser=False)
    assert lock.released is True
