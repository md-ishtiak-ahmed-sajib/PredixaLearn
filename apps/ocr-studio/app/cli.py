"""Run the single local UI/API server or execute one OCR workflow."""

from __future__ import annotations

import argparse
import asyncio
import inspect
import json
import sys
import threading
import time
import urllib.error
import urllib.request
import webbrowser
from pathlib import Path
from typing import Any


def _open_browser_when_ready(url: str) -> None:
    from app.core.config import get_settings

    settings = get_settings()
    deadline = time.monotonic() + settings.browser_ready_timeout_seconds
    health_url = f"{url}/api/v1/health"
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(  # noqa: S310
                health_url,
                timeout=settings.browser_request_timeout_seconds,
            ) as response:
                if response.status == 200:
                    webbrowser.open(url)
                    return
        except (OSError, urllib.error.URLError):
            pass
        time.sleep(settings.browser_poll_interval_seconds)


def _run_server(*, open_browser: bool) -> None:
    from app.core.config import get_settings
    from app.core.single_instance import (
        ServerInstanceLock,
        port_is_occupied,
        probe_local_service,
        wait_for_existing_service,
    )

    settings = get_settings()
    display_host = f"[{settings.host}]" if ":" in settings.host else settings.host
    url = f"http://{display_host}:{settings.port}"
    health_url = f"{url}/api/v1/health"

    def reuse_existing() -> None:
        print(f"PredixaLearn is already running at {url}")
        if open_browser:
            webbrowser.open(url)

    instance_lock = ServerInstanceLock(settings.log_dir / "predixalearn.lock")
    if not instance_lock.acquire():
        state = wait_for_existing_service(
            health_url,
            timeout_seconds=settings.browser_ready_timeout_seconds,
            poll_seconds=settings.browser_poll_interval_seconds,
            request_timeout_seconds=settings.browser_request_timeout_seconds,
        )
        if state == "predixalearn":
            reuse_existing()
            return
        if state == "unknown_http":
            raise SystemExit(
                f"Port {settings.port} is used by another local application; it was not stopped."
            )
        raise SystemExit(
            "Another PredixaLearn launch holds the server lock but did not become ready "
            "within the startup timeout."
        )

    try:
        state = probe_local_service(
            health_url,
            timeout_seconds=settings.browser_request_timeout_seconds,
        )
        if state == "predixalearn":
            reuse_existing()
            return
        if state == "unknown_http" or port_is_occupied(
            settings.host,
            settings.port,
            timeout_seconds=settings.browser_request_timeout_seconds,
        ):
            raise SystemExit(
                f"Port {settings.port} is used by another local application; it was not stopped."
            )

        # Importing Uvicorn only after lock/port preflight prevents a duplicate launch
        # from importing FastAPI, Paddle, or initializing GPU models.
        import uvicorn

        print(f"PredixaLearn: {url}")
        print(f"API documentation: {url}/docs")
        if open_browser:
            threading.Thread(
                target=_open_browser_when_ready,
                args=(url,),
                daemon=True,
                name="browser-readiness",
            ).start()
        uvicorn.run(
            "app.main:create_app",
            factory=True,
            host=settings.host,
            port=settings.port,
            workers=1,
            log_level="info",
        )
    finally:
        instance_lock.release()


def _workflow_function(name: str):
    from app.workflows import (
        run_layout_parsing,
        run_table_extraction,
        run_text_recognition,
        run_vl_processing,
    )

    return {
        "text": run_text_recognition,
        "layout": run_layout_parsing,
        "table": run_table_extraction,
        "vl": run_vl_processing,
    }[name]


def _run_cli(workflow: str, input_value: str, output_value: str | None) -> None:
    import uuid
    from dataclasses import replace
    from datetime import datetime, timezone

    from app.core.config import get_settings
    from app.documents.artifacts import build_and_publish_artifacts
    from app.storage.history import HistoryStore

    path = Path(input_value).expanduser().resolve()
    if not path.exists():
        raise SystemExit(f"Input does not exist: {path}")
    allowed = {".pdf", ".png", ".jpg", ".jpeg", ".bmp", ".tif", ".tiff", ".webp"}
    inputs = (
        sorted(item for item in path.iterdir() if item.suffix.lower() in allowed)
        if path.is_dir()
        else [path]
    )
    if not inputs:
        raise SystemExit("No supported documents were found")

    function = _workflow_function(workflow)
    output_base = Path(output_value).expanduser().resolve() if output_value else None
    settings = get_settings()
    if output_base:
        settings = replace(settings, output_dir=output_base)
    workflow_name = {
        "text": "text_recognition",
        "layout": "layout_parsing",
        "table": "table_extraction",
        "vl": "vl_processing",
    }[workflow]
    started = datetime.now(timezone.utc)

    if workflow == "text" and len(inputs) > 1 and not any(
        path.suffix.lower() == ".pdf" for path in inputs
    ):
        raw_results = function(inputs)
    else:
        raw_results = []
        for input_path in inputs:
            parameters = inspect.signature(function).parameters
            kwargs: dict[str, Any] = {"image_input": input_path}
            if "save_output" in parameters:
                kwargs["save_output"] = False
            raw_results.append(function(**kwargs))

    history = HistoryStore(settings)
    results: list[dict[str, Any]] = []
    for input_path, raw_result in zip(inputs, raw_results, strict=True):
        job_id = uuid.uuid4().hex
        outcome = build_and_publish_artifacts(
            input_path,
            input_path.name,
            workflow_name,
            raw_result,
            job_id=job_id,
            settings=settings,
        )
        completed = datetime.now(timezone.utc)
        result_path = history.save_result(job_id, outcome.result)
        history.upsert(
            job_id=job_id,
            input_name=input_path.name,
            workflow=workflow_name,
            input_kind="pdf" if input_path.suffix.lower() == ".pdf" else "image",
            status="completed",
            item_count=int(outcome.result.get("page_count", 1)),
            error=None,
            result_path=result_path,
            artifact_manifest_path=outcome.manifest_path,
            created_at=started,
            started_at=started,
            completed_at=completed,
            updated_at=completed,
        )
        results.append(outcome.result)
    result: Any = results[0] if len(results) == 1 else results
    print(json.dumps(result, ensure_ascii=False, indent=2))


def main() -> None:
    parser = argparse.ArgumentParser(description="Local PredixaLearn document service")
    parser.add_argument(
        "--mode", choices=("server", "cli", "institution-worker"), default="server"
    )
    parser.add_argument("--workflow", choices=("text", "layout", "table", "vl"))
    parser.add_argument("--input")
    parser.add_argument("--output-dir")
    parser.add_argument("--no-browser", action="store_true")
    parser.add_argument(
        "--once",
        action="store_true",
        help="Claim at most one institution worker job and then exit",
    )
    parser.add_argument(
        "--poll-interval",
        type=float,
        default=5.0,
        help="Seconds between institution worker job checks (1-300)",
    )
    parser.add_argument(
        "--desktop",
        action="store_true",
        help="Run the installer-configured local HTTPS service with its Windows tray host",
    )
    args = parser.parse_args()
    if args.mode == "server":
        if args.desktop:
            from app.desktop_host import run_desktop_host

            run_desktop_host(open_browser=not args.no_browser)
            return
        _run_server(open_browser=not args.no_browser)
        return
    if args.mode == "institution-worker":
        if args.desktop:
            parser.error("--desktop is only available in server mode")
        from app.institution.worker import run_worker_loop, run_worker_once

        if args.once:
            print(asyncio.run(run_worker_once()))
        else:
            asyncio.run(run_worker_loop(poll_interval_seconds=args.poll_interval))
        return
    if args.desktop:
        parser.error("--desktop is only available in server mode")
    if not args.workflow or not args.input:
        parser.error("--workflow and --input are required in CLI mode")
    _run_cli(args.workflow, args.input, args.output_dir)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit(130)
