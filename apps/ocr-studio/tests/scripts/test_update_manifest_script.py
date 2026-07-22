from __future__ import annotations

import base64
import json
import os
import subprocess
import sys
from pathlib import Path

from Crypto.PublicKey import ECC
from Crypto.Signature import eddsa

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "build_update_manifest.py"
VERIFY_SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "verify_update_manifest.py"


def _components_file(tmp_path: Path) -> Path:
    components = tmp_path / "components.json"
    components.write_text(json.dumps({"components": []}), encoding="utf-8")
    return components


def _build_manifest(
    tmp_path: Path, *, private_key: ECC.EccKey, expected_public_key: ECC.EccKey
) -> subprocess.CompletedProcess[str]:
    expected = tmp_path / "expected-public.pem"
    expected.write_text(expected_public_key.export_key(format="PEM"), encoding="utf-8")
    manifest = tmp_path / "manifest.json"
    signature = tmp_path / "manifest.json.sig"
    environment = os.environ | {
        "PREDIXALEARN_UPDATE_SIGNING_KEY": private_key.export_key(format="PEM"),
    }
    return subprocess.run(  # noqa: S603 - test invokes the repository's fixed Python script.
        [
            sys.executable,
            str(SCRIPT),
            "--release-tag",
            "v1.1.0",
            "--minimum-app-version",
            "1.1.0",
            "--components",
            str(_components_file(tmp_path)),
            "--output",
            str(manifest),
            "--signature-output",
            str(signature),
            "--expected-public-key",
            str(expected),
        ],
        check=False,
        capture_output=True,
        text=True,
        env=environment,
    )


def test_manifest_builder_accepts_a_matching_generated_ed25519_keypair(tmp_path: Path) -> None:
    private_key = ECC.generate(curve="Ed25519")
    result = _build_manifest(
        tmp_path, private_key=private_key, expected_public_key=private_key.public_key()
    )

    assert result.returncode == 0, result.stderr
    manifest = (tmp_path / "manifest.json").read_bytes()
    signature = base64.b64decode((tmp_path / "manifest.json.sig").read_bytes())
    eddsa.new(private_key.public_key(), mode="rfc8032").verify(manifest, signature)
    assert json.loads(manifest)["components"] == []
    verification = subprocess.run(  # noqa: S603 - test invokes the repository's fixed Python script.
        [
            sys.executable,
            str(VERIFY_SCRIPT),
            "--manifest",
            str(tmp_path / "manifest.json"),
            "--signature",
            str(tmp_path / "manifest.json.sig"),
            "--public-key",
            str(tmp_path / "expected-public.pem"),
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    assert verification.returncode == 0, verification.stderr
    assert "Verified v1.1.0 with 0 approved component(s)." in verification.stdout


def test_manifest_builder_rejects_a_mismatched_private_key(tmp_path: Path) -> None:
    result = _build_manifest(
        tmp_path,
        private_key=ECC.generate(curve="Ed25519"),
        expected_public_key=ECC.generate(curve="Ed25519").public_key(),
    )

    assert result.returncode != 0
    assert "does not match the committed maintenance public key" in result.stderr
    assert not (tmp_path / "manifest.json").exists()
