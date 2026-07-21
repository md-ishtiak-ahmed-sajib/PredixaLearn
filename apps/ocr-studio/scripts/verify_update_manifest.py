"""Verify a detached Ed25519 signature for a runtime maintenance manifest."""

from __future__ import annotations

import argparse
import base64
import json
from pathlib import Path
from typing import Any

from Crypto.PublicKey import ECC
from Crypto.Signature import eddsa


def _load_public_key(path: Path) -> ECC.EccKey:
    try:
        key = ECC.import_key(path.read_bytes())
        if key.curve != "Ed25519" or key.has_private():
            raise ValueError("not an Ed25519 public key")
    except (OSError, ValueError, IndexError) as exc:
        raise SystemExit("--public-key must contain an Ed25519 public PEM") from exc
    return key


def _decode_signature(value: bytes) -> bytes:
    raw = value.strip()
    if len(raw) == 64:
        return raw
    try:
        signature = base64.b64decode(raw, validate=True)
    except ValueError as exc:
        raise SystemExit("The manifest signature is malformed") from exc
    if len(signature) != 64:
        raise SystemExit("The manifest signature is malformed")
    return signature


def _validate_manifest(payload: bytes) -> dict[str, Any]:
    try:
        manifest = json.loads(payload)
    except json.JSONDecodeError as exc:
        raise SystemExit("The signed payload is not valid JSON") from exc
    if not isinstance(manifest, dict) or manifest.get("schema_version") != 1:
        raise SystemExit("The signed payload is not a supported maintenance manifest")
    if not isinstance(manifest.get("components"), list):
        raise SystemExit("The signed payload has no valid components list")
    return manifest


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--signature", type=Path, required=True)
    parser.add_argument("--public-key", type=Path, required=True)
    arguments = parser.parse_args()

    payload = arguments.manifest.read_bytes()
    signature = _decode_signature(arguments.signature.read_bytes())
    try:
        eddsa.new(_load_public_key(arguments.public_key), mode="rfc8032").verify(payload, signature)
    except ValueError as exc:
        raise SystemExit("The manifest signature does not match the committed public key") from exc
    manifest = _validate_manifest(payload)
    print(
        f"Verified {manifest['manifest_version']} with {len(manifest['components'])} approved component(s)."
    )


if __name__ == "__main__":
    main()
