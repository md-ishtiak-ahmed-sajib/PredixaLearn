"""Build the signed runtime-bootstrap manifest consumed before Python exists."""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.parse import urlsplit

from Crypto.PublicKey import ECC
from Crypto.Signature import eddsa

_RELEASE_PREFIX = "/md-ishtiak-ahmed-sajib/PredixaLearn/releases/"


def _release_url(value: str) -> str:
    parsed = urlsplit(value)
    if (
        parsed.scheme != "https"
        or parsed.hostname != "github.com"
        or parsed.username
        or parsed.password
        or not parsed.path.startswith(_RELEASE_PREFIX)
    ):
        raise ValueError("runtime URL must be an immutable PredixaLearn GitHub Release asset URL")
    return value


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        while chunk := source.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--release-tag", required=True)
    parser.add_argument("--runtime-url", required=True)
    parser.add_argument("--runtime", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--signature-output", type=Path, required=True)
    parser.add_argument("--expires-days", type=int, default=30)
    arguments = parser.parse_args()
    if not 1 <= arguments.expires_days <= 90:
        raise SystemExit("--expires-days must be between 1 and 90")
    private_key = os.environ.get("PREDIXALEARN_UPDATE_SIGNING_KEY")
    if not private_key:
        raise SystemExit("PREDIXALEARN_UPDATE_SIGNING_KEY is required")
    try:
        key = ECC.import_key(private_key)
        if key.curve != "Ed25519" or not key.has_private():
            raise ValueError("not an Ed25519 private key")
    except (ValueError, IndexError) as exc:
        raise SystemExit("PREDIXALEARN_UPDATE_SIGNING_KEY must be an Ed25519 private PEM") from exc
    runtime = arguments.runtime.resolve()
    if not runtime.is_file():
        raise SystemExit("runtime bundle does not exist")
    now = datetime.now(UTC).replace(microsecond=0)
    manifest = {
        "schema_version": 1,
        "manifest_version": arguments.release_tag,
        "issued_at": now.isoformat().replace("+00:00", "Z"),
        "expires_at": (now + timedelta(days=arguments.expires_days)).isoformat().replace(
            "+00:00", "Z"
        ),
        "platforms": ["windows-x86_64"],
        "runtime": {
            "url": _release_url(arguments.runtime_url),
            "sha256": _sha256(runtime),
            "size_bytes": runtime.stat().st_size,
        },
    }
    payload = json.dumps(manifest, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()
    signature = eddsa.new(key, mode="rfc8032").sign(payload)
    arguments.output.parent.mkdir(parents=True, exist_ok=True)
    arguments.output.write_bytes(payload)
    arguments.signature_output.parent.mkdir(parents=True, exist_ok=True)
    arguments.signature_output.write_text(base64.b64encode(signature).decode("ascii"), encoding="ascii")


if __name__ == "__main__":
    main()
