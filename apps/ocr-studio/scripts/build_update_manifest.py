"""Build and Ed25519-sign a runtime-only GitHub Release manifest.

Run this only in the Windows release workflow. The input component file is
reviewed source metadata, not browser input, and contains the SHA-256 hashes
and GitHub Release URLs for already-built artifacts.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from Crypto.PublicKey import ECC
from Crypto.Signature import eddsa

ROOT = Path(__file__).resolve().parents[1]


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _load_components(path: Path) -> list[dict[str, Any]]:
    value = json.loads(path.read_text(encoding="utf-8"))
    components = value.get("components", value) if isinstance(value, dict) else value
    if not isinstance(components, list) or not all(isinstance(item, dict) for item in components):
        raise ValueError("Component metadata must be a list or an object containing components")
    return components


def _load_signing_key(private_pem: str) -> ECC.EccKey:
    try:
        key = ECC.import_key(private_pem)
        if key.curve != "Ed25519" or not key.has_private():
            raise ValueError("not an Ed25519 private key")
    except (ValueError, IndexError) as exc:
        raise SystemExit("PREDIXALEARN_UPDATE_SIGNING_KEY must be an Ed25519 private PEM") from exc
    return key


def _require_matching_public_key(private_key: ECC.EccKey, expected_path: Path) -> None:
    try:
        expected_key = ECC.import_key(expected_path.read_bytes())
        if expected_key.curve != "Ed25519" or expected_key.has_private():
            raise ValueError("not an Ed25519 public key")
    except (OSError, ValueError, IndexError) as exc:
        raise SystemExit("--expected-public-key must contain an Ed25519 public PEM") from exc

    if private_key.public_key().export_key(format="DER") != expected_key.export_key(format="DER"):
        raise SystemExit(
            "PREDIXALEARN_UPDATE_SIGNING_KEY does not match the committed maintenance public key"
        )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--release-tag", required=True)
    parser.add_argument("--components", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--signature-output", type=Path, required=True)
    parser.add_argument("--minimum-app-version", required=True)
    parser.add_argument("--expires-days", type=int, default=30)
    parser.add_argument("--expected-public-key", type=Path, required=True)
    arguments = parser.parse_args()
    if not 1 <= arguments.expires_days <= 90:
        raise SystemExit("--expires-days must be between 1 and 90")
    private_key = os.environ.get("PREDIXALEARN_UPDATE_SIGNING_KEY")
    if not private_key:
        raise SystemExit("PREDIXALEARN_UPDATE_SIGNING_KEY is required")
    key = _load_signing_key(private_key)
    _require_matching_public_key(key, arguments.expected_public_key)

    now = datetime.now(UTC).replace(microsecond=0)
    manifest = {
        "schema_version": 1,
        "manifest_version": arguments.release_tag,
        "issued_at": now.isoformat().replace("+00:00", "Z"),
        "expires_at": (now + timedelta(days=arguments.expires_days)).isoformat().replace(
            "+00:00", "Z"
        ),
        "minimum_predixalearn_version": arguments.minimum_app_version,
        "platforms": ["windows-x86_64"],
        "runtime_locks": {
            "constraints_sha256": _sha256(ROOT / "constraints.txt"),
            "libreoffice_lock_sha256": _sha256(ROOT / "scripts" / "libreoffice.lock.json"),
        },
        "components": _load_components(arguments.components),
    }
    payload = json.dumps(manifest, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("utf-8")
    signature = eddsa.new(key, mode="rfc8032").sign(payload)
    arguments.output.parent.mkdir(parents=True, exist_ok=True)
    arguments.output.write_bytes(payload)
    arguments.signature_output.parent.mkdir(parents=True, exist_ok=True)
    arguments.signature_output.write_text(base64.b64encode(signature).decode("ascii"), encoding="ascii")


if __name__ == "__main__":
    main()
