"""Institution-owned object-storage adapters with traversal-safe keys."""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Protocol

from .config import Settings, get_settings


def validate_key(key: str) -> str:
    normalized = key.replace("\\", "/").strip("/")
    if not normalized or normalized.startswith(".") or "../" in f"/{normalized}/":
        raise ValueError("Object storage key is invalid")
    return normalized


class ObjectStorage(Protocol):
    def healthcheck(self) -> None: ...

    def put(self, key: str, data: bytes, *, content_type: str) -> dict[str, object]: ...

    def read(self, key: str) -> bytes: ...

    def delete(self, key: str) -> None: ...

    def download_url(self, key: str, *, expires_seconds: int = 300) -> str | None: ...


class LocalObjectStorage:
    """Development-only storage that preserves production key semantics."""

    def __init__(self, root: Path) -> None:
        self.root = root.resolve()
        self.root.mkdir(parents=True, exist_ok=True)

    def _path(self, key: str) -> Path:
        candidate = (self.root / validate_key(key)).resolve()
        candidate.relative_to(self.root)
        return candidate

    def healthcheck(self) -> None:
        if not self.root.is_dir():
            raise RuntimeError("Local storage directory is unavailable")

    def put(self, key: str, data: bytes, *, content_type: str) -> dict[str, object]:
        path = self._path(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(path.suffix + ".part")
        temporary.write_bytes(data)
        temporary.replace(path)
        return {
            "key": validate_key(key),
            "size_bytes": len(data),
            "sha256": hashlib.sha256(data).hexdigest(),
            "content_type": content_type,
        }

    def read(self, key: str) -> bytes:
        return self._path(key).read_bytes()

    def delete(self, key: str) -> None:
        self._path(key).unlink(missing_ok=True)

    def download_url(self, key: str, *, expires_seconds: int = 300) -> str | None:
        del key, expires_seconds
        return None


class S3ObjectStorage:
    def __init__(self, settings: Settings) -> None:
        import boto3

        self.bucket = str(settings.s3_bucket)
        self.client = boto3.client("s3", endpoint_url=settings.s3_endpoint_url)

    def healthcheck(self) -> None:
        self.client.head_bucket(Bucket=self.bucket)

    def put(self, key: str, data: bytes, *, content_type: str) -> dict[str, object]:
        normalized = validate_key(key)
        digest = hashlib.sha256(data).hexdigest()
        self.client.put_object(
            Bucket=self.bucket,
            Key=normalized,
            Body=data,
            ContentType=content_type,
            Metadata={"sha256": digest},
            ServerSideEncryption="AES256",
        )
        return {"key": normalized, "size_bytes": len(data), "sha256": digest, "content_type": content_type}

    def read(self, key: str) -> bytes:
        value = self.client.get_object(Bucket=self.bucket, Key=validate_key(key))
        return value["Body"].read()

    def delete(self, key: str) -> None:
        self.client.delete_object(Bucket=self.bucket, Key=validate_key(key))

    def download_url(self, key: str, *, expires_seconds: int = 300) -> str:
        return self.client.generate_presigned_url(
            "get_object",
            Params={"Bucket": self.bucket, "Key": validate_key(key)},
            ExpiresIn=expires_seconds,
        )


class AzureObjectStorage:
    def __init__(self, settings: Settings) -> None:
        from azure.identity import DefaultAzureCredential
        from azure.storage.blob import BlobServiceClient

        account_url = __import__("os").environ.get("PREDIXALEARN_AZURE_ACCOUNT_URL", "")
        if not account_url.startswith("https://"):
            raise ValueError("Azure storage requires PREDIXALEARN_AZURE_ACCOUNT_URL")
        service = BlobServiceClient(account_url, credential=DefaultAzureCredential())
        self.container = service.get_container_client(str(settings.azure_container))

    def healthcheck(self) -> None:
        self.container.get_container_properties()

    def put(self, key: str, data: bytes, *, content_type: str) -> dict[str, object]:
        from azure.storage.blob import ContentSettings

        normalized = validate_key(key)
        digest = hashlib.sha256(data).hexdigest()
        self.container.upload_blob(
            normalized,
            data,
            overwrite=True,
            metadata={"sha256": digest},
            content_settings=ContentSettings(content_type=content_type),
        )
        return {"key": normalized, "size_bytes": len(data), "sha256": digest, "content_type": content_type}

    def read(self, key: str) -> bytes:
        return self.container.download_blob(validate_key(key)).readall()

    def delete(self, key: str) -> None:
        self.container.delete_blob(validate_key(key), delete_snapshots="include")

    def download_url(self, key: str, *, expires_seconds: int = 300) -> None:
        # User-delegation SAS generation is deployment-specific. The API streams
        # the object when an institution does not configure a delegation key.
        del key, expires_seconds
        return None


def create_storage(settings: Settings | None = None) -> ObjectStorage:
    selected = settings or get_settings()
    if selected.storage_provider == "s3":
        return S3ObjectStorage(selected)
    if selected.storage_provider == "azure":
        return AzureObjectStorage(selected)
    return LocalObjectStorage(selected.storage_root)
