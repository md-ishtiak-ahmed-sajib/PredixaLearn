"""Signed bootstrap operations used by the elevated Windows installer only.

This module is built into a small PyInstaller executable.  It does not start
OCR, accept browser input, or execute commands from a release manifest.
"""

from __future__ import annotations

import argparse
import base64
import ctypes
import hashlib
import json
import os
import re
import shutil
import socket
import stat
import subprocess
import sys
import tempfile
import zipfile
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import Request, urlopen

from Crypto.PublicKey import ECC
from Crypto.Signature import eddsa

DESKTOP_HOSTNAME = "app.predixalearn.com"
# An upgrade removes only the prior application's marked loopback mapping.
# It is retained exclusively for precise cleanup during a branded upgrade.
_LEGACY_DESKTOP_HOSTNAME = bytes.fromhex("6170702e636976697363726962652d6f63722e636f6d").decode("ascii")
_LEGACY_HOSTS_MARKER = bytes.fromhex(
    "232043697669536372696265204f4352206c6f63616c206170706c69636174696f6e"
).decode("ascii")
MANIFEST_URL = (
    "https://github.com/md-ishtiak-ahmed-sajib/PredixaLearn/releases/latest/download/"
    "predixalearn-desktop-manifest.json"
)
RELEASE_PREFIX = "/md-ishtiak-ahmed-sajib/PredixaLearn/releases/"
MAX_MANIFEST_BYTES = 1_000_000
MAX_RUNTIME_BYTES = 10 * 1024 * 1024 * 1024
MAX_EXTRACTED_BYTES = 20 * 1024 * 1024 * 1024
HOSTS_MARKER = "# PredixaLearn local application"


class BootstrapError(RuntimeError):
    """A safe installer-facing error that contains no private document data."""


def _resource_path(name: str) -> Path:
    frozen_root = getattr(sys, "_MEIPASS", None)
    if frozen_root:
        return Path(frozen_root) / name
    return Path(__file__).resolve().parent / "maintenance" / name


def _public_key() -> object:
    try:
        return ECC.import_key(_resource_path("predixalearn-update-public.pem").read_text("utf-8"))
    except (OSError, ValueError, IndexError) as exc:
        raise BootstrapError("The installer trust key is unavailable") from exc


def _trusted_release_url(value: str) -> str:
    parsed = urlsplit(value)
    if (
        parsed.scheme != "https"
        or parsed.hostname != "github.com"
        or parsed.username
        or parsed.password
        or not parsed.path.startswith(RELEASE_PREFIX)
    ):
        raise BootstrapError("The signed desktop release does not use the approved channel")
    return value


def _final_download_url_is_trusted(value: str) -> bool:
    parsed = urlsplit(value)
    return parsed.scheme == "https" and parsed.hostname in {
        "github.com",
        "objects.githubusercontent.com",
        "release-assets.githubusercontent.com",
    }


def _download_bytes(url: str, *, maximum: int) -> bytes:
    request = Request(  # noqa: S310 - caller validates a curated GitHub Release URL.
        url, headers={"User-Agent": "PredixaLearn-desktop-bootstrap/1"}
    )
    try:
        with urlopen(request, timeout=30) as response:  # noqa: S310 - curated GitHub release URL.
            if not _final_download_url_is_trusted(response.geturl()):
                raise BootstrapError("The signed download redirected to an untrusted location")
            length = response.headers.get("Content-Length")
            if length and int(length) > maximum:
                raise BootstrapError("The approved download is too large")
            chunks: list[bytes] = []
            size = 0
            while True:
                chunk = response.read(min(1024 * 1024, maximum - size + 1))
                if not chunk:
                    break
                size += len(chunk)
                if size > maximum:
                    raise BootstrapError("The approved download is too large")
                chunks.append(chunk)
            return b"".join(chunks)
    except (HTTPError, URLError, OSError, ValueError) as exc:
        raise BootstrapError("The signed desktop release is unavailable") from exc


def _download_file(url: str, destination: Path, *, maximum: int) -> tuple[int, str]:
    """Stream a signed runtime artifact without holding gigabytes in memory."""
    request = Request(  # noqa: S310 - caller validates a curated GitHub Release URL.
        url, headers={"User-Agent": "PredixaLearn-desktop-bootstrap/1"}
    )
    try:
        with urlopen(request, timeout=30) as response:  # noqa: S310 - curated GitHub release URL.
            if not _final_download_url_is_trusted(response.geturl()):
                raise BootstrapError("The signed download redirected to an untrusted location")
            length = response.headers.get("Content-Length")
            if length and int(length) > maximum:
                raise BootstrapError("The approved download is too large")
            size = 0
            digest = hashlib.sha256()
            with destination.open("wb") as target:
                while True:
                    chunk = response.read(min(1024 * 1024, maximum - size + 1))
                    if not chunk:
                        break
                    size += len(chunk)
                    if size > maximum:
                        raise BootstrapError("The approved download is too large")
                    digest.update(chunk)
                    target.write(chunk)
            return size, digest.hexdigest()
    except (HTTPError, URLError, OSError, ValueError) as exc:
        destination.unlink(missing_ok=True)
        raise BootstrapError("The signed desktop release is unavailable") from exc


def _parse_time(value: object) -> datetime:
    if not isinstance(value, str):
        raise BootstrapError("The signed desktop manifest has invalid timestamps")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise BootstrapError("The signed desktop manifest has invalid timestamps") from exc
    if parsed.tzinfo is None:
        raise BootstrapError("The signed desktop manifest has invalid timestamps")
    return parsed.astimezone(UTC)


def _verify_manifest(payload: bytes, signature: bytes) -> dict[str, object]:
    try:
        decoded_signature = base64.b64decode(signature.strip(), validate=True)
        if len(decoded_signature) != 64:
            raise ValueError("signature size")
        eddsa.new(_public_key(), mode="rfc8032").verify(payload, decoded_signature)
        manifest = json.loads(payload)
    except (ValueError, TypeError, json.JSONDecodeError) as exc:
        raise BootstrapError("The desktop release signature is invalid") from exc
    if not isinstance(manifest, dict) or manifest.get("schema_version") != 1:
        raise BootstrapError("The signed desktop manifest is unsupported")
    if "windows-x86_64" not in manifest.get("platforms", []):
        raise BootstrapError("The signed desktop release does not support this Windows device")
    issued_at = _parse_time(manifest.get("issued_at"))
    expires_at = _parse_time(manifest.get("expires_at"))
    now = datetime.now(UTC)
    if issued_at > now or expires_at <= now:
        raise BootstrapError("The signed desktop release has expired")
    runtime = manifest.get("runtime")
    if not isinstance(runtime, dict):
        raise BootstrapError("The signed desktop release has no runtime bundle")
    _trusted_release_url(str(runtime.get("url", "")))
    if not re.fullmatch(r"[a-f0-9]{64}", str(runtime.get("sha256", ""))):
        raise BootstrapError("The signed desktop runtime hash is invalid")
    size = runtime.get("size_bytes")
    if not isinstance(size, int) or not 1 <= size <= MAX_RUNTIME_BYTES:
        raise BootstrapError("The signed desktop runtime size is invalid")
    return manifest


def _safe_extract(archive: Path, destination: Path) -> None:
    with zipfile.ZipFile(archive) as bundle:
        entries = bundle.infolist()
        if not 1 <= len(entries) <= 25_000:
            raise BootstrapError("The approved runtime archive has an invalid entry count")
        total = 0
        for entry in entries:
            name = Path(entry.filename)
            if not entry.filename or name.is_absolute() or ".." in name.parts:
                raise BootstrapError("The approved runtime archive contains an unsafe path")
            if stat.S_ISLNK(entry.external_attr >> 16):
                raise BootstrapError("The approved runtime archive contains a link")
            total += entry.file_size
            if total > MAX_EXTRACTED_BYTES:
                raise BootstrapError("The approved runtime archive expands beyond its safe limit")
            target = (destination / name).resolve()
            try:
                target.relative_to(destination.resolve())
            except ValueError as exc:
                raise BootstrapError("The approved runtime archive contains an unsafe path") from exc
        bundle.extractall(destination)


def _sha256_file(path: Path, *, maximum: int) -> tuple[int, str]:
    """Hash a locally bundled archive with the same size guard as downloads."""
    try:
        size = path.stat().st_size
    except OSError as exc:
        raise BootstrapError("The bundled desktop runtime is unavailable") from exc
    if not 1 <= size <= maximum:
        raise BootstrapError("The bundled desktop runtime has an invalid size")
    digest = hashlib.sha256()
    try:
        with path.open("rb") as source:
            while chunk := source.read(1024 * 1024):
                digest.update(chunk)
    except OSError as exc:
        raise BootstrapError("The bundled desktop runtime is unavailable") from exc
    return size, digest.hexdigest()


def _promote_runtime(extracted: Path, destination: Path) -> None:
    if not (extracted / "run.py").is_file():
        raise BootstrapError("The approved runtime archive is missing its PredixaLearn launcher")
    backup = destination.with_name(f"{destination.name}.backup")
    if backup.exists():
        shutil.rmtree(backup)
    if destination.exists():
        destination.replace(backup)
    try:
        extracted.replace(destination)
    except OSError:
        if backup.exists() and not destination.exists():
            backup.replace(destination)
        raise
    if backup.exists():
        shutil.rmtree(backup)


def download_runtime(manifest_url: str, destination: Path) -> None:
    """Download and atomically promote only the signed desktop runtime archive."""
    _trusted_release_url(manifest_url)
    payload = _download_bytes(manifest_url, maximum=MAX_MANIFEST_BYTES)
    signature = _download_bytes(f"{manifest_url}.sig", maximum=8_192)
    manifest = _verify_manifest(payload, signature)
    runtime = manifest["runtime"]
    if not isinstance(runtime, dict):
        raise BootstrapError("The signed desktop release has no runtime bundle")
    destination = destination.resolve()
    destination.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix="predixalearn-runtime-", dir=destination.parent))
    archive = staging / "runtime.zip"
    extracted = staging / "runtime"
    extracted.mkdir()
    try:
        size, digest = _download_file(
            str(runtime["url"]), archive, maximum=int(runtime["size_bytes"])
        )
        if size != int(runtime["size_bytes"]):
            raise BootstrapError("The desktop runtime size does not match its signed manifest")
        if digest != runtime["sha256"]:
            raise BootstrapError("The desktop runtime checksum does not match its signed manifest")
        _safe_extract(archive, extracted)
        _promote_runtime(extracted, destination)
    finally:
        shutil.rmtree(staging, ignore_errors=True)


def install_bundled_runtime(archive: Path, destination: Path, sha256: str) -> None:
    """Safely install the checksum-pinned ZIP embedded in an Inno installer.

    This is deliberately separate from online updates: the archive is already
    inside the signed/verified setup executable and is never fetched from a
    network location.  It still gets a strict checksum, ZIP safety, staging,
    and atomic-promotion check before becoming the active runtime.
    """
    if not re.fullmatch(r"[a-f0-9]{64}", sha256):
        raise BootstrapError("The bundled desktop runtime checksum is invalid")
    archive = archive.resolve()
    destination = destination.resolve()
    destination.parent.mkdir(parents=True, exist_ok=True)
    _, digest = _sha256_file(archive, maximum=MAX_RUNTIME_BYTES)
    if digest != sha256:
        raise BootstrapError("The bundled desktop runtime checksum does not match")
    staging = Path(tempfile.mkdtemp(prefix="predixalearn-runtime-", dir=destination.parent))
    extracted = staging / "runtime"
    extracted.mkdir()
    try:
        _safe_extract(archive, extracted)
        _promote_runtime(extracted, destination)
    finally:
        shutil.rmtree(staging, ignore_errors=True)


def _directory_sha256(source: Path, *, maximum: int) -> str:
    """Return a stable content hash while rejecting links and oversized trees."""
    if not source.is_dir():
        raise BootstrapError("The bundled desktop runtime directory is unavailable")
    digest = hashlib.sha256(b"PredixaLearn runtime directory\0")
    total = 0
    files: list[Path] = []
    try:
        for entry in source.rglob("*"):
            if entry.is_symlink():
                raise BootstrapError("The bundled desktop runtime directory contains a link")
            if entry.is_file():
                files.append(entry)
    except OSError as exc:
        raise BootstrapError("The bundled desktop runtime directory is unavailable") from exc
    if not files:
        raise BootstrapError("The bundled desktop runtime directory is empty")
    for file_path in sorted(files, key=lambda path: path.relative_to(source).as_posix()):
        try:
            relative = file_path.relative_to(source).as_posix()
            size = file_path.stat().st_size
        except (OSError, ValueError) as exc:
            raise BootstrapError("The bundled desktop runtime directory is unavailable") from exc
        total += size
        if total > maximum:
            raise BootstrapError("The bundled desktop runtime expands beyond its safe limit")
        digest.update(b"F\0")
        digest.update(relative.encode("utf-8"))
        digest.update(b"\0")
        try:
            with file_path.open("rb") as source_file:
                while chunk := source_file.read(1024 * 1024):
                    digest.update(chunk)
        except OSError as exc:
            raise BootstrapError("The bundled desktop runtime directory is unavailable") from exc
    return digest.hexdigest()


def _validate_bundled_runtime_directory(source: Path, *, maximum: int) -> None:
    """Validate an Inno-extracted payload without rereading each file's bytes.

    Inno Setup has already checked its embedded payload before extraction.
    This validation prevents link/traversal and size attacks before copying
    temporary files into the installed application directory.
    """
    if not source.is_dir():
        raise BootstrapError("The bundled desktop runtime directory is unavailable")
    total = 0
    files = 0
    try:
        for entry in source.rglob("*"):
            if entry.is_symlink():
                raise BootstrapError("The bundled desktop runtime directory contains a link")
            if entry.is_file():
                files += 1
                total += entry.stat().st_size
                if total > maximum:
                    raise BootstrapError("The bundled desktop runtime expands beyond its safe limit")
    except OSError as exc:
        raise BootstrapError("The bundled desktop runtime directory is unavailable") from exc
    if not files:
        raise BootstrapError("The bundled desktop runtime directory is empty")


def install_bundled_runtime_directory(
    source: Path, destination: Path, sha256: str | None = None
) -> None:
    """Atomically promote an Inno-extracted runtime directory after verification."""
    source = source.resolve()
    destination = destination.resolve()
    if sha256 is not None:
        if not re.fullmatch(r"[a-f0-9]{64}", sha256):
            raise BootstrapError("The bundled desktop runtime checksum is invalid")
        if _directory_sha256(source, maximum=MAX_EXTRACTED_BYTES) != sha256:
            raise BootstrapError("The bundled desktop runtime checksum does not match")
    else:
        _validate_bundled_runtime_directory(source, maximum=MAX_EXTRACTED_BYTES)
    destination.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix="predixalearn-runtime-", dir=destination.parent))
    extracted = staging / "runtime"
    try:
        shutil.copytree(source, extracted, copy_function=shutil.copy2)
        _promote_runtime(extracted, destination)
    except OSError as exc:
        raise BootstrapError("The bundled desktop runtime could not be staged") from exc
    finally:
        shutil.rmtree(staging, ignore_errors=True)


def _require_administrator() -> None:
    if os.name != "nt" or not bool(ctypes.windll.shell32.IsUserAnAdmin()):
        raise BootstrapError("PredixaLearn desktop HTTPS setup requires administrator approval")


def _hosts_path() -> Path:
    return Path(os.environ["SystemRoot"]) / "System32" / "drivers" / "etc" / "hosts"


def _host_line() -> str:
    return f"127.0.0.1 {DESKTOP_HOSTNAME} {HOSTS_MARKER}"


def _legacy_host_line() -> str:
    return f"127.0.0.1 {_LEGACY_DESKTOP_HOSTNAME} {_LEGACY_HOSTS_MARKER}"


def _update_hosts(*, add: bool) -> None:
    path = _hosts_path()
    try:
        existing = path.read_text(encoding="utf-8", errors="replace") if path.exists() else ""
        owned_lines = {_host_line(), _legacy_host_line()}
        lines = [line for line in existing.splitlines() if line.strip() not in owned_lines]
        if add:
            lines.append(_host_line())
        path.write_text("\r\n".join(lines).rstrip() + "\r\n", encoding="utf-8", newline="")
        subprocess.run(  # noqa: S603 - fixed Windows cache refresh utility.
            [str(Path(os.environ["SystemRoot"]) / "System32" / "ipconfig.exe"), "/flushdns"],
            check=False,
            capture_output=True,
        )
    except OSError as exc:
        raise BootstrapError("Windows could not update the PredixaLearn hosts entry") from exc


def _certutil(*arguments: str) -> None:
    completed = subprocess.run(  # noqa: S603 - fixed Windows utility and argument set.
        [str(Path(os.environ["SystemRoot"]) / "System32" / "certutil.exe"), *arguments],
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if completed.returncode:
        raise BootstrapError("Windows could not update the PredixaLearn local certificate trust")


def _write_json_atomically(path: Path, value: dict[str, object]) -> None:
    """Write installer state without leaving a partially written marker."""
    temporary = path.with_name(f"{path.name}.tmp")
    try:
        temporary.write_text(json.dumps(value, sort_keys=True), encoding="utf-8")
        temporary.replace(path)
    except OSError as exc:
        temporary.unlink(missing_ok=True)
        raise BootstrapError("PredixaLearn could not record its local HTTPS state") from exc


def _certificate_time(value: object) -> datetime:
    """Normalize cryptography's version-dependent certificate time properties."""
    if isinstance(value, datetime):
        if value.tzinfo is None:
            return value.replace(tzinfo=UTC)
        return value.astimezone(UTC)
    raise BootstrapError("The PredixaLearn local certificate has invalid validity dates")


def _validate_local_https_files(tls_dir: Path, *, expected_thumbprint: str | None = None) -> str:
    """Validate the generated CA, leaf certificate, and matching private key."""
    try:
        from cryptography import x509
        from cryptography.exceptions import InvalidSignature
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import padding
        from cryptography.x509 import ExtensionNotFound

        root_path = tls_dir / "root.cer"
        certificate_path = tls_dir / "server.pem"
        key_path = tls_dir / "server-key.pem"
        root = x509.load_der_x509_certificate(root_path.read_bytes())
        certificate = x509.load_pem_x509_certificate(certificate_path.read_bytes())
        private_key = serialization.load_pem_private_key(key_path.read_bytes(), password=None)
        now = datetime.now(UTC)
        root_not_before = (
            root.not_valid_before_utc
            if hasattr(root, "not_valid_before_utc")
            else root.not_valid_before
        )
        root_not_after = (
            root.not_valid_after_utc if hasattr(root, "not_valid_after_utc") else root.not_valid_after
        )
        leaf_not_before = (
            certificate.not_valid_before_utc
            if hasattr(certificate, "not_valid_before_utc")
            else certificate.not_valid_before
        )
        leaf_not_after = (
            certificate.not_valid_after_utc
            if hasattr(certificate, "not_valid_after_utc")
            else certificate.not_valid_after
        )
        if not (_certificate_time(root_not_before) <= now <= _certificate_time(root_not_after)):
            raise BootstrapError("The PredixaLearn local root certificate is not currently valid")
        if not (
            _certificate_time(leaf_not_before) <= now <= _certificate_time(leaf_not_after)
        ):
            raise BootstrapError("The PredixaLearn local HTTPS certificate is not currently valid")
        root_constraints = root.extensions.get_extension_for_class(x509.BasicConstraints).value
        leaf_constraints = certificate.extensions.get_extension_for_class(x509.BasicConstraints).value
        if not root_constraints.ca or leaf_constraints.ca:
            raise BootstrapError("The PredixaLearn local certificate chain is invalid")
        names = certificate.extensions.get_extension_for_class(
            x509.SubjectAlternativeName
        ).value
        if DESKTOP_HOSTNAME not in names.get_values_for_type(x509.DNSName):
            raise BootstrapError("The PredixaLearn local HTTPS certificate has the wrong hostname")
        if certificate.issuer != root.subject:
            raise BootstrapError("The PredixaLearn local HTTPS certificate has the wrong issuer")
        root.public_key().verify(
            certificate.signature,
            certificate.tbs_certificate_bytes,
            padding.PKCS1v15(),
            certificate.signature_hash_algorithm,
        )
        if certificate.public_key().public_bytes(
            serialization.Encoding.DER,
            serialization.PublicFormat.SubjectPublicKeyInfo,
        ) != private_key.public_key().public_bytes(
            serialization.Encoding.DER,
            serialization.PublicFormat.SubjectPublicKeyInfo,
        ):
            raise BootstrapError("The PredixaLearn local HTTPS private key does not match its certificate")
        thumbprint = root.fingerprint(hashes.SHA1()).hex()  # noqa: S303 - Windows store identifier.
        if expected_thumbprint and thumbprint.lower() != expected_thumbprint.lower():
            raise BootstrapError("The PredixaLearn local root certificate does not match its state")
        return thumbprint
    except BootstrapError:
        raise
    except (OSError, ValueError, TypeError, KeyError, ExtensionNotFound, InvalidSignature) as exc:
        raise BootstrapError("The PredixaLearn local HTTPS certificate is missing or invalid") from exc


def _verify_hosts_mapping() -> None:
    """Require exactly one PredixaLearn mapping and reject conflicting mappings."""
    try:
        lines = _hosts_path().read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError as exc:
        raise BootstrapError("The Windows hosts file is unavailable") from exc
    expected = _host_line()
    matches = [line.strip() for line in lines if line.strip() == expected]
    if len(matches) != 1:
        raise BootstrapError("The PredixaLearn local hostname mapping is incomplete")
    for line in lines:
        tokens = line.split("#", 1)[0].split()
        if DESKTOP_HOSTNAME in tokens and line.strip() != expected:
            raise BootstrapError("The Windows hosts file contains a conflicting PredixaLearn mapping")


def _check_loopback_port_available() -> None:
    """Fail installation early when HTTPS cannot bind to loopback:443."""
    probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        probe.bind(("127.0.0.1", 443))
    except OSError as exc:
        raise BootstrapError(
            "HTTPS port 443 is already in use; stop the other service before installing or repairing PredixaLearn"
        ) from exc
    finally:
        probe.close()


def _verify_local_https_setup(install_root: Path) -> None:
    """Verify files, state, hosts mapping, and Windows trust after installation."""
    state_path = _tls_state_path(install_root)
    try:
        state = json.loads(state_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise BootstrapError("PredixaLearn local HTTPS setup state is missing or invalid") from exc
    if not isinstance(state, dict) or state.get("hostname") != DESKTOP_HOSTNAME:
        raise BootstrapError("PredixaLearn local HTTPS setup has the wrong hostname")
    thumbprint = state.get("root_thumbprint")
    if not isinstance(thumbprint, str) or not re.fullmatch(r"[a-fA-F0-9]{40}", thumbprint):
        raise BootstrapError("PredixaLearn local HTTPS setup has an invalid certificate identity")
    actual_thumbprint = _validate_local_https_files(
        state_path.parent / "tls", expected_thumbprint=thumbprint
    )
    _verify_hosts_mapping()
    _certutil("-verifystore", "Root", actual_thumbprint)


def _tls_state_path(install_root: Path) -> Path:
    return install_root / "desktop" / "desktop-state.json"


def _create_local_https_certificates(tls_dir: Path) -> tuple[Path, Path, Path, str]:
    """Generate the CA/leaf pair and remove partial files if creation fails."""
    from cryptography import x509
    from cryptography.exceptions import InternalError
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import NameOID

    try:
        now = datetime.now(UTC)
        root_key = rsa.generate_private_key(public_exponent=65537, key_size=4096)
        root_name = x509.Name(
            [x509.NameAttribute(NameOID.COMMON_NAME, "PredixaLearn Local Root")]
        )
        root_certificate = (
            x509.CertificateBuilder()
            .subject_name(root_name)
            .issuer_name(root_name)
            .public_key(root_key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(now - timedelta(minutes=5))
            .not_valid_after(now + timedelta(days=3650))
            .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
            .add_extension(
                x509.KeyUsage(True, False, False, False, False, True, True, False, False),
                critical=True,
            )
            .sign(root_key, hashes.SHA256())
        )
        leaf_key = rsa.generate_private_key(public_exponent=65537, key_size=3072)
        leaf_name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, DESKTOP_HOSTNAME)])
        leaf_certificate = (
            x509.CertificateBuilder()
            .subject_name(leaf_name)
            .issuer_name(root_certificate.subject)
            .public_key(leaf_key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(now - timedelta(minutes=5))
            .not_valid_after(now + timedelta(days=730))
            .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
            .add_extension(
                x509.SubjectAlternativeName([x509.DNSName(DESKTOP_HOSTNAME)]),
                critical=False,
            )
            .add_extension(
                x509.KeyUsage(True, False, True, False, False, False, False, False, False),
                critical=True,
            )
            .sign(root_key, hashes.SHA256())
        )
        root_path = tls_dir / "root.cer"
        certificate_path = tls_dir / "server.pem"
        key_path = tls_dir / "server-key.pem"
        root_path.write_bytes(root_certificate.public_bytes(serialization.Encoding.DER))
        certificate_path.write_bytes(leaf_certificate.public_bytes(serialization.Encoding.PEM))
        key_path.write_bytes(
            leaf_key.private_bytes(
                serialization.Encoding.PEM,
                serialization.PrivateFormat.PKCS8,
                serialization.NoEncryption(),
            )
        )
        return (
            root_path,
            certificate_path,
            key_path,
            root_certificate.fingerprint(hashes.SHA1()).hex(),  # noqa: S303 - Windows store identifier.
        )
    except (OSError, ValueError, TypeError, InternalError) as exc:
        shutil.rmtree(tls_dir, ignore_errors=True)
        raise BootstrapError("PredixaLearn could not create its local HTTPS certificate") from exc


def install_local_https(install_root: Path) -> None:
    """Create a unique local CA/leaf pair and map the app hostname to loopback."""
    _require_administrator()

    install_root = install_root.resolve()
    _check_loopback_port_available()
    state_path = _tls_state_path(install_root)
    if state_path.exists() or (state_path.parent / "tls").exists():
        remove_local_https(install_root)
    tls_dir = state_path.parent / "tls"
    tls_dir.mkdir(parents=True, exist_ok=True)
    root_path, certificate_path, key_path, root_thumbprint = _create_local_https_certificates(tls_dir)
    try:
        # Write the marker before touching global Windows state so an interrupted
        # repair still leaves enough information for the next repair/uninstall.
        _write_json_atomically(
            state_path,
            {
                "hostname": DESKTOP_HOSTNAME,
                "root_thumbprint": root_thumbprint,
                "certificate": certificate_path.name,
                "key": key_path.name,
            },
        )
        _certutil("-addstore", "-f", "Root", str(root_path))
        _update_hosts(add=True)
        user = os.environ.get("USERNAME", "Users")
        try:
            acl_result = subprocess.run(  # noqa: S603 - fixed ACL tool with generated local path.
                [
                    str(Path(os.environ["SystemRoot"]) / "System32" / "icacls.exe"),
                    str(tls_dir),
                    "/inheritance:r",
                    "/grant:r",
                    f"{user}:(OI)(CI)F",
                    "SYSTEM:(OI)(CI)F",
                    "Administrators:(OI)(CI)F",
                ],
                check=False,
                capture_output=True,
            )
        except OSError as exc:
            raise BootstrapError("Windows could not restrict the local HTTPS private key") from exc
        if acl_result.returncode:
            raise BootstrapError("Windows could not restrict the local HTTPS private key")
        _verify_local_https_setup(install_root)
    except Exception:
        try:
            _update_hosts(add=False)
        except Exception:
            # Preserve the original setup error; the next repair can retry cleanup.
            _ = None
        try:
            _certutil(
                "-delstore",
                "Root",
                root_thumbprint,
            )
        except Exception:
            # Preserve the original setup error; the next repair can retry cleanup.
            _ = None
        state_path.unlink(missing_ok=True)
        shutil.rmtree(tls_dir, ignore_errors=True)
        raise


def remove_local_https(install_root: Path) -> None:
    """Remove only the certificate and hosts entry created by PredixaLearn."""
    _require_administrator()
    state_path = _tls_state_path(install_root)
    thumbprint = ""
    if state_path.is_file():
        try:
            thumbprint = str(json.loads(state_path.read_text("utf-8")).get("root_thumbprint", ""))
        except (OSError, ValueError):
            thumbprint = ""
    _update_hosts(add=False)
    if re.fullmatch(r"[a-fA-F0-9]{40}", thumbprint):
        try:
            _certutil("-delstore", "Root", thumbprint)
        except BootstrapError:
            pass
    # Keep the installed bootstrapper and desktop directory. Only generated
    # TLS material and its state marker belong to this lifecycle.
    shutil.rmtree(state_path.parent / "tls", ignore_errors=True)
    state_path.unlink(missing_ok=True)


def main() -> None:
    parser = argparse.ArgumentParser(description="PredixaLearn signed desktop bootstrapper")
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("install-https", "verify-https", "remove-https"):
        item = commands.add_parser(name)
        item.add_argument("--install-root", type=Path, required=True)
    download = commands.add_parser("download-runtime")
    download.add_argument("--manifest-url", default=MANIFEST_URL)
    download.add_argument("--destination", type=Path, required=True)
    bundled = commands.add_parser("install-bundled-runtime")
    bundled.add_argument("--archive", type=Path, required=True)
    bundled.add_argument("--destination", type=Path, required=True)
    bundled.add_argument("--sha256", required=True)
    bundled_directory = commands.add_parser("install-bundled-runtime-directory")
    bundled_directory.add_argument("--source", type=Path, required=True)
    bundled_directory.add_argument("--destination", type=Path, required=True)
    bundled_directory.add_argument("--sha256")
    arguments = parser.parse_args()
    try:
        if arguments.command == "install-https":
            install_local_https(arguments.install_root.resolve())
        elif arguments.command == "verify-https":
            _verify_local_https_setup(arguments.install_root.resolve())
        elif arguments.command == "remove-https":
            remove_local_https(arguments.install_root.resolve())
        elif arguments.command == "install-bundled-runtime":
            install_bundled_runtime(
                arguments.archive,
                arguments.destination.resolve(),
                arguments.sha256,
            )
        elif arguments.command == "install-bundled-runtime-directory":
            install_bundled_runtime_directory(
                arguments.source,
                arguments.destination.resolve(),
                arguments.sha256,
            )
        else:
            download_runtime(arguments.manifest_url, arguments.destination.resolve())
    except BootstrapError as exc:
        raise SystemExit(f"PredixaLearn setup failed: {exc}") from exc


if __name__ == "__main__":
    main()
