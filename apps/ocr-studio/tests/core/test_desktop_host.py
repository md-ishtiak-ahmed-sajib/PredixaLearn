from __future__ import annotations

from dataclasses import replace

import pytest

from app import desktop_host
from app.core.config import get_settings


def test_desktop_host_rejects_missing_desktop_certificate(tmp_path):
    settings = replace(
        get_settings(),
        desktop_mode=True,
        desktop_control_token="x" * 32,
        desktop_tls_cert_path=tmp_path / "missing.pem",
        desktop_tls_key_path=tmp_path / "missing-key.pem",
    )
    with pytest.raises(RuntimeError, match="certificate"):
        desktop_host._validate_desktop_launch(settings)


def test_second_desktop_launch_reuses_the_verified_local_service(monkeypatch, tmp_path):
    certificate = tmp_path / "server.pem"
    key = tmp_path / "server-key.pem"
    certificate.write_text("certificate", encoding="utf-8")
    key.write_text("key", encoding="utf-8")
    settings = replace(
        get_settings(),
        desktop_mode=True,
        host="127.0.0.1",
        port=443,
        desktop_control_token="x" * 32,
        desktop_tls_cert_path=certificate,
        desktop_tls_key_path=key,
    )

    class HeldLock:
        def __init__(self, *_):
            pass

        def acquire(self):
            return False

        def release(self):
            raise AssertionError("An unacquired lock must not be released")

    opened: list[str] = []
    monkeypatch.setattr(desktop_host, "get_settings", lambda: settings)
    monkeypatch.setattr(desktop_host, "ServerInstanceLock", HeldLock)
    monkeypatch.setattr(desktop_host, "wait_for_existing_service", lambda *_, **__: "predixalearn")
    monkeypatch.setattr(desktop_host.webbrowser, "open", lambda url: opened.append(url))
    desktop_host.run_desktop_host()
    assert opened == ["https://app.predixalearn.com"]


def test_desktop_host_never_falls_back_when_https_port_is_occupied(monkeypatch, tmp_path):
    certificate = tmp_path / "server.pem"
    key = tmp_path / "server-key.pem"
    certificate.write_text("certificate", encoding="utf-8")
    key.write_text("key", encoding="utf-8")
    settings = replace(
        get_settings(),
        desktop_mode=True,
        host="127.0.0.1",
        port=443,
        desktop_control_token="x" * 32,
        desktop_tls_cert_path=certificate,
        desktop_tls_key_path=key,
    )

    class HeldLock:
        def __init__(self, *_):
            pass

        def acquire(self):
            return True

        def release(self):
            return None

    monkeypatch.setattr(desktop_host, "get_settings", lambda: settings)
    monkeypatch.setattr(desktop_host, "ServerInstanceLock", HeldLock)
    monkeypatch.setattr(desktop_host, "probe_local_service", lambda *_, **__: "unknown_http")
    monkeypatch.setattr(desktop_host, "port_is_occupied", lambda *_, **__: True)
    with pytest.raises(SystemExit, match="will not fall back"):
        desktop_host.run_desktop_host(open_browser=False)
