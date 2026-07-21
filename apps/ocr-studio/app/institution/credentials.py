"""Store device-bound institution credentials outside OCR History."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass

import keyring
from keyring.errors import KeyringError

SERVICE_NAME = "PredixaLearn Institution"


class CredentialStoreError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class DeviceCredential:
    worker_id: str
    credential: str
    private_key: str


def save_device_credential(value: DeviceCredential) -> None:
    try:
        keyring.set_password(SERVICE_NAME, value.worker_id, json.dumps(asdict(value)))
    except (KeyringError, OSError) as exc:
        raise CredentialStoreError(
            "Windows Credential Manager could not store the institution device credential"
        ) from exc


def load_device_credential(worker_id: str) -> DeviceCredential:
    try:
        raw = keyring.get_password(SERVICE_NAME, worker_id)
    except (KeyringError, OSError) as exc:
        raise CredentialStoreError(
            "Windows Credential Manager could not read the institution device credential"
        ) from exc
    if not raw:
        raise CredentialStoreError("Institution device credential is unavailable; enroll again")
    try:
        value = json.loads(raw)
        return DeviceCredential(
            worker_id=str(value["worker_id"]),
            credential=str(value["credential"]),
            private_key=str(value["private_key"]),
        )
    except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise CredentialStoreError("Institution device credential is corrupted") from exc


def delete_device_credential(worker_id: str) -> None:
    try:
        keyring.delete_password(SERVICE_NAME, worker_id)
    except keyring.errors.PasswordDeleteError:
        return
    except (KeyringError, OSError) as exc:
        raise CredentialStoreError(
            "Windows Credential Manager could not remove the institution credential"
        ) from exc
