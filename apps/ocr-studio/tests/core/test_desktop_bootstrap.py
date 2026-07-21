from __future__ import annotations

import base64
import hashlib
import json
import zipfile
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from Crypto.PublicKey import ECC
from Crypto.Signature import eddsa

from app import desktop_bootstrap


def _signed_manifest(key, *, expires_at: datetime | None = None) -> tuple[bytes, bytes]:
    now = datetime.now(UTC).replace(microsecond=0)
    manifest = {
        "schema_version": 1,
        "manifest_version": "desktop-test",
        "issued_at": (now - timedelta(minutes=1)).isoformat().replace("+00:00", "Z"),
        "expires_at": (expires_at or now + timedelta(days=1)).isoformat().replace(
            "+00:00", "Z"
        ),
        "platforms": ["windows-x86_64"],
        "runtime": {
            "url": "https://github.com/md-ishtiak-ahmed-sajib/PredixaLearn/releases/download/test/runtime.zip",
            "sha256": "a" * 64,
            "size_bytes": 1024,
        },
    }
    payload = json.dumps(manifest, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()
    signature = base64.b64encode(eddsa.new(key, mode="rfc8032").sign(payload))
    return payload, signature


def test_desktop_bootstrap_accepts_only_a_current_signed_release(monkeypatch):
    key = ECC.generate(curve="Ed25519")
    monkeypatch.setattr(desktop_bootstrap, "_public_key", lambda: key.public_key())
    payload, signature = _signed_manifest(key)
    assert desktop_bootstrap._verify_manifest(payload, signature)["manifest_version"] == "desktop-test"

    expired_payload, expired_signature = _signed_manifest(
        key, expires_at=datetime.now(UTC) - timedelta(minutes=1)
    )
    with pytest.raises(desktop_bootstrap.BootstrapError, match="expired"):
        desktop_bootstrap._verify_manifest(expired_payload, expired_signature)


def test_desktop_bootstrap_rejects_traversal_archives(tmp_path):
    archive = tmp_path / "unsafe.zip"
    with zipfile.ZipFile(archive, "w") as bundle:
        bundle.writestr("../outside.txt", "unsafe")
    with pytest.raises(desktop_bootstrap.BootstrapError, match="unsafe path"):
        desktop_bootstrap._safe_extract(archive, tmp_path / "destination")


def test_bundled_runtime_is_checksum_verified_and_promoted_atomically(tmp_path):
    archive = tmp_path / "runtime.zip"
    with zipfile.ZipFile(archive, "w") as bundle:
        bundle.writestr("run.py", "print('PredixaLearn')\n")
        bundle.writestr("app/__init__.py", "")
    destination = tmp_path / "installed" / "runtime"
    checksum = hashlib.sha256(archive.read_bytes()).hexdigest()

    desktop_bootstrap.install_bundled_runtime(archive, destination, checksum)

    assert (destination / "run.py").is_file()
    with pytest.raises(desktop_bootstrap.BootstrapError, match="checksum"):
        desktop_bootstrap.install_bundled_runtime(archive, destination, "a" * 64)


def test_bundled_runtime_directory_is_verified_and_promoted_atomically(tmp_path):
    source = tmp_path / "source"
    (source / "app").mkdir(parents=True)
    (source / "run.py").write_text("print('PredixaLearn')\n", encoding="utf-8")
    (source / "app" / "__init__.py").write_text("", encoding="utf-8")
    checksum = desktop_bootstrap._directory_sha256(
        source, maximum=desktop_bootstrap.MAX_EXTRACTED_BYTES
    )
    destination = tmp_path / "installed" / "runtime"

    desktop_bootstrap.install_bundled_runtime_directory(source, destination, checksum)

    assert (destination / "run.py").is_file()
    (source / "run.py").write_text("modified", encoding="utf-8")
    with pytest.raises(desktop_bootstrap.BootstrapError, match="checksum"):
        desktop_bootstrap.install_bundled_runtime_directory(source, destination, checksum)


def test_inno_verified_runtime_directory_skips_expensive_content_rehash(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    (source / "run.py").write_text("print('PredixaLearn')\n", encoding="utf-8")
    destination = tmp_path / "installed" / "runtime"

    desktop_bootstrap.install_bundled_runtime_directory(source, destination)

    assert (destination / "run.py").is_file()


def test_hosts_cleanup_removes_only_the_exact_predixalearn_mapping(monkeypatch, tmp_path):
    hosts = tmp_path / "hosts"
    hosts.write_text(
        "127.0.0.1 unrelated.local # PredixaLearn local application\n"
        f"{desktop_bootstrap._host_line()}\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(desktop_bootstrap, "_hosts_path", lambda: hosts)
    monkeypatch.setattr(desktop_bootstrap.subprocess, "run", lambda *_, **__: None)
    desktop_bootstrap._update_hosts(add=False)
    assert hosts.read_text(encoding="utf-8") == (
        "127.0.0.1 unrelated.local # PredixaLearn local application\n"
    )


def test_hosts_upgrade_cleanup_removes_only_the_prior_owned_mapping(monkeypatch, tmp_path):
    hosts = tmp_path / "hosts"
    hosts.write_text(
        "127.0.0.1 unrelated.local # local application\n"
        f"{desktop_bootstrap._legacy_host_line()}\n"
        f"{desktop_bootstrap._host_line()}\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(desktop_bootstrap, "_hosts_path", lambda: hosts)
    monkeypatch.setattr(desktop_bootstrap.subprocess, "run", lambda *_, **__: None)

    desktop_bootstrap._update_hosts(add=True)

    lines = hosts.read_text(encoding="utf-8").splitlines()
    assert desktop_bootstrap._legacy_host_line() not in lines
    assert lines.count(desktop_bootstrap._host_line()) == 1
    assert "127.0.0.1 unrelated.local # local application" in lines


def _patch_local_https_environment(monkeypatch, tmp_path, *, acl_returncode=0):
    hosts = tmp_path / "hosts"
    hosts.write_text("", encoding="utf-8")
    monkeypatch.setenv("USERNAME", "tester")
    monkeypatch.setattr(desktop_bootstrap, "_require_administrator", lambda: None)
    monkeypatch.setattr(desktop_bootstrap, "_check_loopback_port_available", lambda: None)
    monkeypatch.setattr(desktop_bootstrap, "_hosts_path", lambda: hosts)
    monkeypatch.setattr(desktop_bootstrap, "_certutil", lambda *args: None)

    def fake_run(command, *args, **kwargs):
        del args, kwargs
        return SimpleNamespace(returncode=acl_returncode if "icacls.exe" in command[0] else 0)

    monkeypatch.setattr(desktop_bootstrap.subprocess, "run", fake_run)
    return hosts


def test_local_https_setup_is_verified_idempotent_and_preserves_bootstrapper(monkeypatch, tmp_path):
    hosts = _patch_local_https_environment(monkeypatch, tmp_path)
    install_root = tmp_path / "installed"
    desktop = install_root / "desktop"
    desktop.mkdir(parents=True)
    bootstrapper = desktop / "PredixaLearnDesktopBootstrapper.exe"
    bootstrapper.write_bytes(b"bootstrapper")

    desktop_bootstrap.install_local_https(install_root)
    first_certificate = (desktop / "tls" / "server.pem").read_bytes()
    desktop_bootstrap._verify_local_https_setup(install_root)
    assert bootstrapper.is_file()
    assert hosts.read_text(encoding="utf-8").splitlines().count(desktop_bootstrap._host_line()) == 1

    desktop_bootstrap.install_local_https(install_root)
    assert bootstrapper.is_file()
    assert (desktop / "tls" / "server.pem").read_bytes() != first_certificate
    assert hosts.read_text(encoding="utf-8").splitlines().count(desktop_bootstrap._host_line()) == 1

    desktop_bootstrap.remove_local_https(install_root)
    assert bootstrapper.is_file()
    assert not (desktop / "tls").exists()
    assert not (desktop / "desktop-state.json").exists()


def test_local_https_setup_rolls_back_hosts_and_certificates_when_acl_fails(monkeypatch, tmp_path):
    hosts = _patch_local_https_environment(monkeypatch, tmp_path, acl_returncode=1)
    install_root = tmp_path / "installed"

    with pytest.raises(desktop_bootstrap.BootstrapError, match="restrict"):
        desktop_bootstrap.install_local_https(install_root)

    assert not (install_root / "desktop" / "tls").exists()
    assert not (install_root / "desktop" / "desktop-state.json").exists()
    assert desktop_bootstrap._host_line() not in hosts.read_text(encoding="utf-8")


def test_hosts_verification_rejects_a_conflicting_hostname_mapping(monkeypatch, tmp_path):
    hosts = tmp_path / "hosts"
    hosts.write_text(
        f"{desktop_bootstrap._host_line()}\n127.0.0.1 {desktop_bootstrap.DESKTOP_HOSTNAME}\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(desktop_bootstrap, "_hosts_path", lambda: hosts)
    with pytest.raises(desktop_bootstrap.BootstrapError, match="conflicting"):
        desktop_bootstrap._verify_hosts_mapping()


def test_loopback_port_preflight_reports_occupied_https_port(monkeypatch):
    class BusySocket:
        def bind(self, *_):
            raise OSError("busy")

        def close(self):
            return None

    monkeypatch.setattr(desktop_bootstrap.socket, "socket", lambda *_: BusySocket())
    with pytest.raises(desktop_bootstrap.BootstrapError, match="port 443"):
        desktop_bootstrap._check_loopback_port_available()
