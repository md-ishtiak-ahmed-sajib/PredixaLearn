"""Project-scoped server lock and safe local service discovery."""

from __future__ import annotations

import json
import os
import socket
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import BinaryIO, Literal

SERVICE_ID = "predixalearn"
ProbeResult = Literal["predixalearn", "unknown_http", "unavailable"]


class ServerInstanceLock:
    """Hold one OS-level byte lock for the Uvicorn process lifetime."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._handle: BinaryIO | None = None
        self.acquired = False

    def acquire(self) -> bool:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        handle = self.path.open("a+b", buffering=0)
        if handle.seek(0, os.SEEK_END) == 0:
            handle.write(b"0")
        handle.seek(0)
        try:
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except (OSError, BlockingIOError):
            handle.close()
            return False
        self._handle = handle
        self.acquired = True
        return True

    def release(self) -> None:
        handle = self._handle
        if handle is None:
            return
        try:
            handle.seek(0)
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        finally:
            handle.close()
            self._handle = None
            self.acquired = False

    def __enter__(self) -> ServerInstanceLock:
        if not self.acquire():
            raise RuntimeError("The PredixaLearn server lock is already held")
        return self

    def __exit__(self, *_: object) -> None:
        self.release()


def probe_local_service(health_url: str, *, timeout_seconds: float) -> ProbeResult:
    try:
        with urllib.request.urlopen(  # noqa: S310
            health_url,
            timeout=timeout_seconds,
        ) as response:
            if response.status != 200:
                return "unknown_http"
            payload = response.read(65_537)
            if len(payload) > 65_536:
                return "unknown_http"
    except (OSError, urllib.error.URLError):
        return "unavailable"
    try:
        data = json.loads(payload)
    except (UnicodeDecodeError, json.JSONDecodeError):
        return "unknown_http"
    return "predixalearn" if data.get("service_id") == SERVICE_ID else "unknown_http"


def port_is_occupied(host: str, port: int, *, timeout_seconds: float) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout_seconds):
            return True
    except OSError:
        return False


def wait_for_existing_service(
    health_url: str,
    *,
    timeout_seconds: float,
    poll_seconds: float,
    request_timeout_seconds: float,
) -> ProbeResult:
    deadline = time.monotonic() + timeout_seconds
    while True:
        state = probe_local_service(
            health_url,
            timeout_seconds=request_timeout_seconds,
        )
        if state != "unavailable" or time.monotonic() >= deadline:
            return state
        time.sleep(min(poll_seconds, max(0.0, deadline - time.monotonic())))
