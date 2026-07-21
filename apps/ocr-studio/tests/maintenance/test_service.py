from __future__ import annotations

import base64
import json
import zipfile
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from Crypto.PublicKey import ECC
from Crypto.Signature import eddsa

from app.core.config import get_settings
from app.maintenance import service as maintenance


def _manifest(*, expires_at: datetime | None = None) -> dict[str, object]:
    now = datetime.now(UTC).replace(microsecond=0)
    return {
        "schema_version": 1,
        "manifest_version": "runtime-2026.07.20",
        "issued_at": now.isoformat().replace("+00:00", "Z"),
        "expires_at": (expires_at or now + timedelta(days=2)).isoformat().replace("+00:00", "Z"),
        "minimum_predixalearn_version": "1.0.0",
        "platforms": ["windows-x86_64"],
        "components": [
            {
                "id": "paddle-runtime",
                "name": "Approved Paddle runtime",
                "kind": "python_runtime_bundle",
                "target_version": "3.7.1",
                "requires": {"minimum_python": "3.12", "free_disk_bytes": 0},
                "artifact": {
                    "url": "https://github.com/md-ishtiak-ahmed-sajib/PredixaLearn/releases/download/v1/paddle-runtime.zip",
                    "sha256": "a" * 64,
                    "maximum_download_bytes": 1024,
                },
            },
            {
                "id": "unapproved-handler",
                "name": "Unknown runtime tool",
                "kind": "run_shell_script",
                "target_version": "1.0",
            },
        ],
    }


def _service(tmp_path: Path) -> maintenance.MaintenanceService:
    settings = replace(
        get_settings(),
        maintenance_dir=tmp_path / "maintenance",
        update_public_key_path=tmp_path / "public.pem",
    )
    return maintenance.MaintenanceService(settings)


def test_signed_manifest_is_verified_before_it_is_parsed(monkeypatch, tmp_path: Path) -> None:
    private_key = ECC.generate(curve="Ed25519")
    public_key = private_key.public_key()
    service = _service(tmp_path)
    service.settings.update_public_key_path.write_text(public_key.export_key(format="PEM"), encoding="utf-8")
    payload = json.dumps(_manifest(), sort_keys=True, separators=(",", ":")).encode("utf-8")
    signature = base64.b64encode(eddsa.new(private_key, mode="rfc8032").sign(payload))

    def fake_download(url: str, *, maximum: int) -> bytes:
        del maximum
        return signature if url.endswith(".sig") else payload

    monkeypatch.setattr(maintenance, "_download_bytes", fake_download)
    manifest, digest = service._verify_manifest()
    assert manifest["manifest_version"] == "runtime-2026.07.20"
    assert len(digest) == 64


def test_invalid_public_key_returns_safe_unavailable_channel(tmp_path: Path) -> None:
    service = _service(tmp_path)
    service.settings.update_public_key_path.write_text("not a key", encoding="utf-8")
    result = service.check()
    assert result["channel"]["status"] == "unavailable"
    assert result["ready_to_update"] == []
    assert result["can_apply"] is False


def test_ready_and_blocked_candidates_are_kept_separate(monkeypatch, tmp_path: Path) -> None:
    service = _service(tmp_path)
    manifest = _manifest()
    monkeypatch.setattr(service, "_verify_manifest", lambda: (manifest, "b" * 64))
    monkeypatch.setattr(maintenance, "_active_ocr_jobs", lambda: False)
    result = service.check()
    assert [item["id"] for item in result["ready_to_update"]] == ["paddle-runtime"]
    assert result["blocked"][0]["id"] == "unapproved-handler"
    assert "approved updater handler" in result["blocked"][0]["reason"]


def test_expired_manifest_is_never_offered_for_update(monkeypatch, tmp_path: Path) -> None:
    service = _service(tmp_path)
    expired = datetime.now(UTC) - timedelta(minutes=1)
    monkeypatch.setattr(service, "_verify_manifest", lambda: (_manifest(expires_at=expired), "c" * 64))
    result = service.check()
    assert result["channel"]["status"] == "unavailable"
    assert result["ready_to_update"] == []


def test_update_archives_reject_path_traversal(tmp_path: Path) -> None:
    archive = tmp_path / "unsafe.zip"
    with zipfile.ZipFile(archive, "w") as bundle:
        bundle.writestr("../outside.txt", "unsafe")
    destination = tmp_path / "destination"
    destination.mkdir()
    with pytest.raises(maintenance.MaintenanceError, match="unsafe path"):
        maintenance._safe_extract_zip(archive, destination)
    assert not (tmp_path / "outside.txt").exists()
