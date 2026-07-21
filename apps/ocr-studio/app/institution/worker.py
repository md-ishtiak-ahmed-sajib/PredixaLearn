"""Explicit institution-controlled OCR worker with signed-envelope verification."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import tempfile
import time
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)

from app.core.capabilities import validate_document_profile, validate_language
from app.core.config import get_settings
from app.core.queue import JobQueue, JobStatus
from app.core.resource_profiles import profile_allows_workflow
from app.documents.text_filters import normalize_remove_terms, normalize_remove_text
from app.institution.credentials import (
    CredentialStoreError,
    DeviceCredential,
    load_device_credential,
)
from app.institution.service import read_connection
from app.storage.history import HistoryResultError, HistoryStore
from app.workflows import (
    run_layout_parsing,
    run_table_extraction,
    run_text_recognition,
    run_vl_processing,
)

MAX_ENVELOPE_BYTES = 256 * 1024
MEDIA_SUFFIXES = {
    "application/pdf": ".pdf",
    "image/png": ".png",
    "image/jpeg": ".jpg",
    "image/bmp": ".bmp",
    "image/tiff": ".tiff",
    "image/webp": ".webp",
}
OUTPUT_MEDIA_TYPES = {
    "result_json": "application/json",
    "markdown": "text/markdown",
    "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "table": "application/octet-stream",
}
WORKFLOWS = {
    "text_recognition": run_text_recognition,
    "layout_parsing": run_layout_parsing,
    "table_extraction": run_table_extraction,
    "vl_processing": run_vl_processing,
}


class InstitutionWorkerError(RuntimeError):
    pass


def _device_headers(
    device: DeviceCredential, *, method: str, path: str, query: str = ""
) -> dict[str, str]:
    timestamp = str(int(time.time()))
    target = f"{path}?{query}" if query else path
    message = f"{timestamp}\n{method.upper()}\n{target}".encode()
    try:
        private_raw = base64.urlsafe_b64decode(
            device.private_key + "=" * (-len(device.private_key) % 4)
        )
        signature = Ed25519PrivateKey.from_private_bytes(private_raw).sign(message)
    except (TypeError, ValueError) as exc:
        raise InstitutionWorkerError("Institution device signing key is invalid") from exc
    return {
        "Authorization": f"Bearer {device.credential}",
        "X-PredixaLearn-Device-Timestamp": timestamp,
        "X-PredixaLearn-Device-Signature": base64.urlsafe_b64encode(signature)
        .decode()
        .rstrip("="),
    }


def _decode_public_key(value: str) -> Ed25519PublicKey:
    try:
        raw = base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))
        return Ed25519PublicKey.from_public_bytes(raw)
    except (TypeError, ValueError) as exc:
        raise InstitutionWorkerError("Institution server signing identity is invalid") from exc


def verify_worker_envelope(
    signed: dict[str, Any], *, pinned_public_key: str, tenant_id: str
) -> dict[str, Any]:
    """Verify a job against the key pinned during one-time device enrollment."""

    if signed.get("algorithm") != "Ed25519" or signed.get("public_key") != pinned_public_key:
        raise InstitutionWorkerError("Worker job was not signed by the enrolled institution")
    payload = signed.get("payload")
    signature_value = signed.get("signature")
    if not isinstance(payload, dict) or not isinstance(signature_value, str):
        raise InstitutionWorkerError("Worker job envelope is malformed")
    canonical = json.dumps(
        payload, sort_keys=True, separators=(",", ":"), default=str
    ).encode()
    if len(canonical) > MAX_ENVELOPE_BYTES:
        raise InstitutionWorkerError("Worker job envelope exceeds the safety limit")
    try:
        signature = base64.urlsafe_b64decode(
            signature_value + "=" * (-len(signature_value) % 4)
        )
        _decode_public_key(pinned_public_key).verify(signature, canonical)
    except (InvalidSignature, TypeError, ValueError) as exc:
        raise InstitutionWorkerError("Worker job signature verification failed") from exc
    required = {"job_id", "tenant_id", "workflow", "source", "expires_at"}
    if not required.issubset(payload) or payload["tenant_id"] != tenant_id:
        raise InstitutionWorkerError("Worker job tenant or fields are invalid")
    try:
        expires_at = datetime.fromisoformat(str(payload["expires_at"]).replace("Z", "+00:00"))
    except ValueError as exc:
        raise InstitutionWorkerError("Worker job expiry is invalid") from exc
    if expires_at.tzinfo is None or expires_at <= datetime.now(timezone.utc):
        raise InstitutionWorkerError("Worker job envelope has expired")
    if payload["workflow"] not in WORKFLOWS:
        raise InstitutionWorkerError("Worker job requested an unsupported workflow")
    source = payload["source"]
    if not isinstance(source, dict) or source.get("kind") != "source_document":
        raise InstitutionWorkerError("Worker job source descriptor is invalid")
    if source.get("media_type") not in MEDIA_SUFFIXES:
        raise InstitutionWorkerError("Worker job source media type is unsupported")
    if not isinstance(source.get("size_bytes"), int) or source["size_bytes"] <= 0:
        raise InstitutionWorkerError("Worker job source size is invalid")
    runtime = get_settings()
    if not profile_allows_workflow(runtime.runtime_profile, str(payload["workflow"])):
        raise InstitutionWorkerError("Worker job is disabled by this runtime profile")
    if source["size_bytes"] > runtime.max_upload_bytes:
        raise InstitutionWorkerError("Worker job source exceeds this runtime profile's limit")
    digest = source.get("sha256")
    if not isinstance(digest, str) or len(digest) != 64:
        raise InstitutionWorkerError("Worker job source checksum is invalid")
    return payload


async def _download_source(
    client: httpx.AsyncClient,
    base_url: str,
    device: DeviceCredential,
    payload: dict[str, Any],
    destination: Path,
) -> None:
    source = payload["source"]
    expected_size = int(source["size_bytes"])
    digest = hashlib.sha256()
    received = 0
    path = f"/api/v2/workers/jobs/{payload['job_id']}/source"
    async with client.stream(
        "GET",
        f"{base_url}{path}",
        headers=_device_headers(device, method="GET", path=path),
    ) as response:
        response.raise_for_status()
        with destination.open("xb") as output:
            async for chunk in response.aiter_bytes(1024 * 1024):
                received += len(chunk)
                if received > expected_size:
                    raise InstitutionWorkerError("Worker source exceeded its signed size")
                digest.update(chunk)
                output.write(chunk)
    if received != expected_size or digest.hexdigest() != source["sha256"]:
        await asyncio.to_thread(destination.unlink, missing_ok=True)
        raise InstitutionWorkerError("Worker source checksum verification failed")


def _job_settings(payload: dict[str, Any]) -> tuple[str | None, str, str | None, list[str]]:
    selected = payload.get("settings", {})
    if not isinstance(selected, dict):
        raise InstitutionWorkerError("Worker job settings are invalid")
    unsupported = set(selected) - {
        "language",
        "document_profile",
        "remove_text",
        "remove_terms",
    }
    if unsupported:
        raise InstitutionWorkerError("Worker job requested unsupported settings")
    runtime = get_settings()
    try:
        language = validate_language(
            selected.get("language"),
            ocr_version=runtime.ocr_version,
            structure_version=runtime.structure_ocr_version,
            default=runtime.ocr_lang,
        )
        profile = validate_document_profile(selected.get("document_profile"))
        remove_text = normalize_remove_text(selected.get("remove_text"))
        terms_value = selected.get("remove_terms", [])
        if not isinstance(terms_value, list) or not all(
            isinstance(item, str) for item in terms_value
        ):
            raise ValueError("remove_terms must be a list of text values")
        remove_terms = normalize_remove_terms(terms_value)
    except ValueError as exc:
        raise InstitutionWorkerError(str(exc)) from exc
    return language, profile, remove_text, remove_terms


def _artifact_files(
    store: HistoryStore, job_id: str, allowed_outputs: set[str]
) -> list[tuple[str, Path, str]]:
    record = store.get(job_id)
    items: list[tuple[str, Path, str]] = []
    for kind, format_name in (
        ("result_json", "json"),
        ("markdown", "markdown"),
        ("docx", "docx"),
    ):
        if kind not in allowed_outputs:
            continue
        try:
            path = store.artifact_path(record, format_name)
        except HistoryResultError:
            continue
        items.append((kind, path, OUTPUT_MEDIA_TYPES[kind]))
    if "table" in allowed_outputs:
        manifest = store.read_manifest(record) or {}
        files = manifest.get("files", {})
        table_files = files.get("tables", []) if isinstance(files, dict) else []
        archive_root = manifest.get("archive_root")
        if isinstance(archive_root, str) and isinstance(table_files, list):
            root = store._resolve_history_path(archive_root).resolve()  # noqa: SLF001
            for relative in table_files[:200]:
                if not isinstance(relative, str):
                    continue
                candidate = (root / relative).resolve()
                try:
                    candidate.relative_to(root)
                except ValueError:
                    continue
                if candidate.is_file():
                    media_type = (
                        "text/csv"
                        if candidate.suffix.casefold() == ".csv"
                        else "application/vnd.openxmlformats-officedocument."
                        "spreadsheetml.sheet"
                    )
                    items.append(("table", candidate, media_type))
    return items


async def _upload_outputs(
    client: httpx.AsyncClient,
    *,
    base_url: str,
    device: DeviceCredential,
    job_id: str,
    files: list[tuple[str, Path, str]],
) -> list[dict[str, Any]]:
    uploaded: list[dict[str, Any]] = []
    for kind, path, media_type in files:
        endpoint = f"/api/v2/workers/jobs/{job_id}/artifacts"
        query = f"kind={kind}"
        with path.open("rb") as stream:
            response = await client.post(
                f"{base_url}{endpoint}",
                params={"kind": kind},
                headers=_device_headers(
                    device, method="POST", path=endpoint, query=query
                ),
                files={"file": (path.name, stream, media_type)},
            )
        response.raise_for_status()
        uploaded.append(response.json())
    return uploaded


async def run_worker_once() -> str:
    """Claim at most one job, process it locally, and return its terminal state."""

    connection = read_connection()
    if connection is None or not connection.server_signing_public_key:
        raise InstitutionWorkerError(
            "This device must be enrolled again before it can accept signed OCR jobs"
        )
    try:
        device = load_device_credential(connection.worker_id)
    except CredentialStoreError as exc:
        raise InstitutionWorkerError(str(exc)) from exc
    async with httpx.AsyncClient(timeout=httpx.Timeout(120, connect=20)) as client:
        next_path = "/api/v2/workers/jobs/next"
        response = await client.get(
            f"{connection.base_url}{next_path}",
            headers=_device_headers(device, method="GET", path=next_path),
        )
        if response.status_code == 204:
            return "idle"
        response.raise_for_status()
        payload = verify_worker_envelope(
            response.json(),
            pinned_public_key=connection.server_signing_public_key,
            tenant_id=connection.tenant_id,
        )
        job_id = str(payload["job_id"])
        with tempfile.TemporaryDirectory(prefix="predixalearn-institution-worker-") as raw_root:
            root = Path(raw_root)
            source = payload["source"]
            input_path = root / f"source{MEDIA_SUFFIXES[source['media_type']]}"
            try:
                await _download_source(
                    client,
                    connection.base_url,
                    device,
                    payload,
                    input_path,
                )
                runtime = get_settings()
                isolated = replace(
                    runtime,
                    output_dir=root / "output",
                    upload_dir=root / "uploads",
                    history_dir=root / "history",
                    history_db_path=root / "history" / "history.sqlite3",
                    log_dir=root / "logs",
                )
                store = HistoryStore(isolated)
                queue = JobQueue(isolated, store)
                language, profile, remove_text, remove_terms = _job_settings(payload)
                try:
                    local_job_id = await queue.submit(
                        WORKFLOWS[str(payload["workflow"])],
                        input_path,
                        workflow_name=str(payload["workflow"]),
                        input_name=f"institution-job-{job_id}{input_path.suffix}",
                        delete_input=True,
                        language=language,
                        document_profile=profile,
                        remove_text=remove_text,
                        remove_terms=remove_terms,
                    )
                    result = await queue.wait_for_result(local_job_id)
                finally:
                    queue.shutdown()
                if result.status != JobStatus.COMPLETED:
                    raise InstitutionWorkerError(result.error or "Local OCR worker failed")
                allowed = {
                    str(item)
                    for item in payload.get("allowed_outputs", [])
                    if isinstance(item, str)
                }
                files = _artifact_files(store, local_job_id, allowed)
                artifacts = await _upload_outputs(
                    client,
                    base_url=connection.base_url,
                    device=device,
                    job_id=job_id,
                    files=files,
                )
                result_value = result.result if isinstance(result.result, dict) else {}
                summary = {
                    "page_count": result_value.get("page_count"),
                    "warning_count": len(result_value.get("warnings", []))
                    if isinstance(result_value.get("warnings"), list)
                    else 0,
                    "runtime_profile": runtime.runtime_profile,
                }
                result_path = f"/api/v2/workers/jobs/{job_id}/result"
                terminal = await client.post(
                    f"{connection.base_url}{result_path}",
                    headers=_device_headers(device, method="POST", path=result_path),
                    json={
                        "status": "completed",
                        "artifacts": artifacts,
                        "result_summary": summary,
                    },
                )
                terminal.raise_for_status()
                return "completed"
            except Exception as exc:
                try:
                    result_path = f"/api/v2/workers/jobs/{job_id}/result"
                    await client.post(
                        f"{connection.base_url}{result_path}",
                        headers=_device_headers(
                            device, method="POST", path=result_path
                        ),
                        json={
                            "status": "failed",
                            "artifacts": [],
                            "result_summary": {"error_code": type(exc).__name__},
                        },
                    )
                except httpx.HTTPError:
                    pass
                if isinstance(exc, InstitutionWorkerError):
                    raise
                raise InstitutionWorkerError("Institution OCR job failed safely") from exc


async def run_worker_loop(*, poll_interval_seconds: float = 5.0) -> None:
    if not 1.0 <= poll_interval_seconds <= 300.0:
        raise InstitutionWorkerError("Worker poll interval must be between 1 and 300 seconds")
    while True:
        status = await run_worker_once()
        if status == "idle":
            await asyncio.sleep(poll_interval_seconds)
