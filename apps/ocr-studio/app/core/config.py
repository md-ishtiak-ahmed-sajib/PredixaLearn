"""Validated runtime settings for the local PredixaLearn service."""

from __future__ import annotations

import ipaddress
import json
import os
import re
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from urllib.parse import urlsplit

PROJECT_ROOT = Path(__file__).resolve().parents[2]
BYTES_PER_MEBIBYTE = 1024 * 1024
PRODUCT_NAME = "PredixaLearn"
PRODUCT_TAGLINE = "Predict Smarter. Learn Better."
# Encoded only for one-way, non-destructive migration of the prior default
# local data directory; the former product name is not part of the active UI.
_LEGACY_DATA_DIRECTORY = bytes.fromhex("43697669536372696265204f4352").decode("ascii")
UPDATE_MANIFEST_URL = (
    "https://github.com/md-ishtiak-ahmed-sajib/PredixaLearn/"
    "releases/latest/download/predixalearn-update-manifest.json"
)
DESKTOP_HOSTNAME = "app.predixalearn.com"
DESKTOP_HTTPS_PORT = 443


def workspace_root() -> Path:
    """Return the monorepo root when this app is checked out in ``apps/``."""
    candidate = PROJECT_ROOT.parents[1]
    if (candidate / "apps" / "ocr-studio").resolve() == PROJECT_ROOT.resolve():
        return candidate
    return PROJECT_ROOT


def tools_root() -> Path:
    """Return the local, non-source runtime directory used by the OCR app.

    A standalone OCR studio keeps tools beside its application source. The
    root launchers set ``PREDIXALEARN_TOOLS_ROOT`` during migration so an
    already-validated LibreOffice runtime is reused without copying it.
    """
    configured = os.getenv("PREDIXALEARN_TOOLS_ROOT")
    if configured:
        candidate = Path(configured).expanduser().resolve()
        for allowed_root in (PROJECT_ROOT.resolve(), workspace_root()):
            try:
                candidate.relative_to(allowed_root)
                return candidate
            except ValueError:
                continue
        raise ValueError("PREDIXALEARN_TOOLS_ROOT must resolve inside the OCR workspace")
    return (PROJECT_ROOT / ".tools").resolve()


def default_data_root() -> Path:
    local_app_data = os.getenv("LOCALAPPDATA")
    if local_app_data:
        return (Path(local_app_data) / PRODUCT_NAME).resolve()
    if os.name == "nt":
        return (Path.home() / "AppData" / "Local" / PRODUCT_NAME).resolve()
    return (Path.home() / ".local" / "share" / PRODUCT_NAME).resolve()


def legacy_data_root() -> Path:
    """Return the previous default data location for one-way safe migration."""
    local_app_data = os.getenv("LOCALAPPDATA")
    if local_app_data:
        return (Path(local_app_data) / _LEGACY_DATA_DIRECTORY).resolve()
    if os.name == "nt":
        return (Path.home() / "AppData" / "Local" / _LEGACY_DATA_DIRECTORY).resolve()
    return (Path.home() / ".local" / "share" / _LEGACY_DATA_DIRECTORY).resolve()


def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    value = raw.strip().lower()
    if value in {"1", "true", "yes", "on"}:
        return True
    if value in {"0", "false", "no", "off"}:
        return False
    raise ValueError(f"{name} must be a boolean, got {raw!r}")


def _env_int(name: str, default: int, *, minimum: int, maximum: int) -> int:
    raw = os.getenv(name, str(default))
    try:
        value = int(raw)
    except ValueError as exc:
        raise ValueError(f"{name} must be an integer, got {raw!r}") from exc
    if not minimum <= value <= maximum:
        raise ValueError(f"{name} must be between {minimum} and {maximum}")
    return value


def _env_float(name: str, default: float, *, minimum: float, maximum: float) -> float:
    raw = os.getenv(name, str(default))
    try:
        value = float(raw)
    except ValueError as exc:
        raise ValueError(f"{name} must be numeric, got {raw!r}") from exc
    if not minimum <= value <= maximum:
        raise ValueError(f"{name} must be between {minimum} and {maximum}")
    return value


def _desktop_secret() -> str | None:
    """Return the launcher-issued control secret for desktop-only actions."""
    value = os.getenv("OCR_DESKTOP_CONTROL_TOKEN", "").strip()
    if not value:
        return None
    if len(value) < 32:
        raise ValueError("OCR_DESKTOP_CONTROL_TOKEN must contain at least 32 characters")
    return value


def _desktop_tls_path(name: str) -> Path | None:
    value = os.getenv(name, "").strip()
    return Path(value).expanduser().resolve() if value else None


def _validate_loopback(host: str) -> str:
    normalized = host.strip().lower()
    if normalized == "localhost":
        return host
    try:
        if ipaddress.ip_address(normalized).is_loopback:
            return host
    except ValueError:
        pass
    raise ValueError(
        "OCR_HOST must be a loopback address (127.0.0.1, ::1, or localhost) "
        "for this local-only deployment"
    )


def _env_choice(name: str, default: str, allowed: set[str]) -> str:
    value = os.getenv(name, default)
    if value not in allowed:
        choices = ", ".join(sorted(allowed))
        raise ValueError(f"{name} must be one of: {choices}")
    return value


def _validate_device(device: str) -> str:
    if device == "cpu" or re.fullmatch(r"gpu(?::\d+)?", device):
        return device
    raise ValueError("OCR_DEVICE must be 'cpu', 'gpu', or 'gpu:<index>'")


def _validate_update_manifest_url(value: str) -> str:
    """Accept only the curated PredixaLearn GitHub Release channel.

    Keeping this endpoint fixed prevents a local environment variable from
    silently turning the maintenance UI into an arbitrary package downloader.
    Maintainers may still use a release tag instead of ``latest`` for a
    controlled rollout.
    """
    parsed = urlsplit(value)
    prefix = "/md-ishtiak-ahmed-sajib/PredixaLearn/releases/"
    if (
        parsed.scheme != "https"
        or parsed.hostname != "github.com"
        or parsed.username
        or parsed.password
        or not parsed.path.startswith(prefix)
        or not parsed.path.endswith("/predixalearn-update-manifest.json")
    ):
        raise ValueError("OCR_UPDATE_MANIFEST_URL must be a PredixaLearn GitHub Release manifest")
    return value


def _validate_data_paths(output_dir: Path, upload_dir: Path, history_dir: Path) -> None:
    if output_dir == upload_dir:
        raise ValueError("OCR_OUTPUT_DIR and OCR_UPLOAD_DIR must be different")
    if history_dir in {output_dir, upload_dir}:
        raise ValueError("OCR_HISTORY_DIR must be separate from output and temporary uploads")


def _libreoffice_lock() -> dict[str, object]:
    lock_path = PROJECT_ROOT / "scripts" / "libreoffice.lock.json"
    try:
        value = json.loads(lock_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ValueError("The project LibreOffice lock manifest is invalid") from exc
    if not isinstance(value, dict):
        raise ValueError("The project LibreOffice lock manifest must be an object")
    return value


def _project_local_path(name: str, raw: str) -> Path:
    candidate = Path(raw).expanduser()
    path = (candidate if candidate.is_absolute() else PROJECT_ROOT / candidate).resolve()
    for allowed_root in (PROJECT_ROOT.resolve(), tools_root()):
        try:
            path.relative_to(allowed_root)
            return path
        except ValueError:
            continue
    raise ValueError(
        f"{name} must resolve inside the project (the OCR studio or its local tools directory)"
    )


@dataclass(frozen=True, slots=True)
class Settings:
    project_root: Path
    output_dir: Path
    upload_dir: Path
    history_dir: Path
    history_db_path: Path
    log_dir: Path
    maintenance_dir: Path
    institution_dir: Path
    update_manifest_url: str
    update_public_key_path: Path
    host: str
    port: int
    desktop_mode: bool
    desktop_hostname: str
    desktop_control_token: str | None
    desktop_tls_cert_path: Path | None
    desktop_tls_key_path: Path | None
    device: str
    runtime_profile: str
    ocr_lang: str
    ocr_version: str
    ocr_use_doc_orientation: bool
    ocr_use_doc_unwarping: bool
    ocr_use_textline_orientation: bool
    text_recognition_batch_size: int
    structure_ocr_version: str
    structure_use_doc_orientation: bool
    structure_use_unwarping: bool
    structure_use_textline_orientation: bool
    structure_use_chart_recognition: bool
    structure_use_formula_recognition: bool
    vl_pipeline_version: str
    pdf_render_scale: float
    max_upload_bytes: int
    max_history_import_bytes: int
    max_pdf_pages: int
    max_image_pixels: int
    max_queued_jobs: int
    max_job_history: int
    job_ttl_seconds: int
    max_table_cells: int
    clear_gpu_cache_after_document: bool
    upload_chunk_bytes: int
    browser_ready_timeout_seconds: int
    browser_poll_interval_seconds: float
    browser_request_timeout_seconds: int
    docx_timeout_seconds: int
    libreoffice_path: Path
    libreoffice_version: str
    libreoffice_timeout_seconds: int
    max_embedded_images: int
    max_embedded_image_bytes: int
    crop_padding_pixels: int
    correction_enabled: bool
    adaptive_retry_enabled: bool
    adaptive_max_scale: float
    low_confidence_threshold: float
    max_retry_regions_per_page: int
    table_quality_threshold: float

    @property
    def browser_origin(self) -> str:
        """Canonical browser origin for the current launch profile."""
        if self.desktop_mode:
            return f"https://{self.desktop_hostname}"
        return f"http://{self.host}:{self.port}"

    @classmethod
    def from_env(cls) -> "Settings":
        data_root = default_data_root()
        desktop_mode = _env_bool("OCR_DESKTOP_MODE", False)
        host = _validate_loopback(os.getenv("OCR_HOST", "127.0.0.1"))
        port = _env_int(
            "OCR_PORT",
            DESKTOP_HTTPS_PORT if desktop_mode else 8000,
            minimum=1,
            maximum=65535,
        )
        control_token = _desktop_secret()
        certificate_path = _desktop_tls_path("OCR_DESKTOP_TLS_CERT_PATH")
        key_path = _desktop_tls_path("OCR_DESKTOP_TLS_KEY_PATH")
        if desktop_mode:
            if host != "127.0.0.1" or port != DESKTOP_HTTPS_PORT:
                raise ValueError(
                    "Desktop mode must bind only to 127.0.0.1:443 for local HTTPS"
                )
            if not control_token or not certificate_path or not key_path:
                raise ValueError(
                    "Desktop mode requires a launch control token and local TLS certificate paths"
                )
        output_dir = (
            Path(os.getenv("OCR_OUTPUT_DIR", str(data_root / "output"))).expanduser().resolve()
        )
        upload_dir = (
            Path(os.getenv("OCR_UPLOAD_DIR", str(data_root / "uploads")))
            .expanduser()
            .resolve()
        )
        history_dir = (
            Path(os.getenv("OCR_HISTORY_DIR", str(data_root / "history")))
            .expanduser()
            .resolve()
        )
        history_db_path = (
            Path(os.getenv("OCR_HISTORY_DB", str(history_dir / "history.sqlite3")))
            .expanduser()
            .resolve()
        )
        log_dir = (
            Path(os.getenv("OCR_LOG_DIR", str(data_root / "logs"))).expanduser().resolve()
        )
        maintenance_dir = (
            Path(os.getenv("OCR_MAINTENANCE_DIR", str(data_root / "maintenance")))
            .expanduser()
            .resolve()
        )
        institution_dir = (
            Path(os.getenv("OCR_INSTITUTION_DIR", str(data_root / "institution")))
            .expanduser()
            .resolve()
        )
        legacy_device = os.getenv("OCR_CUDA_DEVICE")
        device = _validate_device(os.getenv("OCR_DEVICE", legacy_device or "gpu:0"))
        from app.core.resource_profiles import resolve_profile

        runtime_profile = resolve_profile(
            os.getenv("OCR_RUNTIME_PROFILE", "auto").strip().casefold(), device
        )
        libreoffice_lock = _libreoffice_lock()
        libreoffice_version = str(libreoffice_lock.get("version", "")).strip()
        install_relative = str(libreoffice_lock.get("install_relative_path", "")).strip()
        executable_relative = str(
            libreoffice_lock.get("executable_relative_path", "")
        ).strip()
        if not libreoffice_version or not install_relative or not executable_relative:
            raise ValueError("The project LibreOffice lock manifest is incomplete")
        install_parts = Path(install_relative).parts
        if not install_parts or install_parts[0] != ".tools":
            raise ValueError("The LibreOffice lock manifest must use the local .tools directory")
        current_libreoffice = tools_root() / "libreoffice" / "current" / executable_relative
        default_libreoffice = (
            current_libreoffice
            if current_libreoffice.is_file()
            else tools_root() / Path(*install_parts[1:]) / executable_relative
        )
        libreoffice_path = _project_local_path(
            "OCR_LIBREOFFICE_PATH",
            os.getenv("OCR_LIBREOFFICE_PATH", str(default_libreoffice)),
        )
        _validate_data_paths(output_dir, upload_dir, history_dir)
        max_upload_mb = _env_int("OCR_MAX_UPLOAD_MB", 50, minimum=1, maximum=2048)
        ocr_version = _env_choice(
            "OCR_VERSION",
            "PP-OCRv6",
            {"PP-OCRv3", "PP-OCRv4", "PP-OCRv5", "PP-OCRv6"},
        )
        structure_ocr_version = _env_choice(
            "STRUCTURE_OCR_VERSION",
            "PP-OCRv5",
            {"PP-OCRv3", "PP-OCRv4", "PP-OCRv5"},
        )
        from app.core.capabilities import validate_language

        ocr_lang = validate_language(
            os.getenv("OCR_LANG"),
            ocr_version=ocr_version,
            structure_version=structure_ocr_version,
            default="en",
        )
        return cls(
            project_root=PROJECT_ROOT,
            output_dir=output_dir,
            upload_dir=upload_dir,
            history_dir=history_dir,
            history_db_path=history_db_path,
            log_dir=log_dir,
            maintenance_dir=maintenance_dir,
            institution_dir=institution_dir,
            update_manifest_url=_validate_update_manifest_url(
                os.getenv("OCR_UPDATE_MANIFEST_URL", UPDATE_MANIFEST_URL)
            ),
            update_public_key_path=_project_local_path(
                "OCR_UPDATE_PUBLIC_KEY",
                os.getenv(
                    "OCR_UPDATE_PUBLIC_KEY",
                    str(PROJECT_ROOT / "app" / "maintenance" / "predixalearn-update-public.pem"),
                ),
            ),
            host=host,
            port=port,
            desktop_mode=desktop_mode,
            desktop_hostname=DESKTOP_HOSTNAME,
            desktop_control_token=control_token,
            desktop_tls_cert_path=certificate_path,
            desktop_tls_key_path=key_path,
            device=device,
            runtime_profile=runtime_profile.name,
            ocr_lang=ocr_lang,
            ocr_version=ocr_version,
            ocr_use_doc_orientation=_env_bool("OCR_USE_DOC_ORIENTATION", False),
            ocr_use_doc_unwarping=_env_bool("OCR_USE_DOC_UNWARPING", False),
            ocr_use_textline_orientation=_env_bool("OCR_USE_TEXTLINE_ORIENTATION", False),
            text_recognition_batch_size=_env_int(
                "OCR_TEXT_RECOGNITION_BATCH_SIZE",
                _env_int(
                    "OCR_BATCH_SIZE",
                    runtime_profile.recommended_batch_size,
                    minimum=1,
                    maximum=64,
                ),
                minimum=1,
                maximum=64,
            ),
            structure_ocr_version=structure_ocr_version,
            structure_use_doc_orientation=_env_bool("STRUCTURE_USE_DOC_ORIENTATION", False),
            structure_use_unwarping=_env_bool("STRUCTURE_USE_UNWARPING", False),
            structure_use_textline_orientation=_env_bool(
                "STRUCTURE_USE_TEXTLINE_ORIENTATION", False
            ),
            structure_use_chart_recognition=_env_bool("STRUCTURE_USE_CHART", False),
            structure_use_formula_recognition=_env_bool("STRUCTURE_USE_FORMULA", False),
            vl_pipeline_version=_env_choice("OCR_VL_VERSION", "v1.6", {"v1.5", "v1.6"}),
            pdf_render_scale=_env_float(
                "OCR_PDF_RENDER_SCALE",
                runtime_profile.recommended_pdf_render_scale,
                minimum=0.5,
                maximum=4.0,
            ),
            max_upload_bytes=max_upload_mb * BYTES_PER_MEBIBYTE,
            max_history_import_bytes=_env_int(
                "OCR_MAX_HISTORY_IMPORT_MB", 512, minimum=1, maximum=4096
            )
            * BYTES_PER_MEBIBYTE,
            max_pdf_pages=_env_int("OCR_MAX_PDF_PAGES", 100, minimum=1, maximum=2000),
            max_image_pixels=_env_int(
                "OCR_MAX_IMAGE_PIXELS", 25_000_000, minimum=1_000_000, maximum=200_000_000
            ),
            max_queued_jobs=_env_int(
                "OCR_MAX_QUEUED_JOBS",
                runtime_profile.recommended_queue_limit,
                minimum=0,
                maximum=1000,
            ),
            max_job_history=_env_int("OCR_MAX_JOB_HISTORY", 100, minimum=1, maximum=10_000),
            job_ttl_seconds=_env_int("OCR_JOB_TTL_SECONDS", 3600, minimum=60, maximum=604_800),
            max_table_cells=_env_int(
                "OCR_MAX_TABLE_CELLS", 100_000, minimum=100, maximum=1_000_000
            ),
            clear_gpu_cache_after_document=_env_bool("OCR_CLEAR_GPU_CACHE_AFTER_DOCUMENT", True),
            upload_chunk_bytes=_env_int("OCR_UPLOAD_CHUNK_KB", 1024, minimum=64, maximum=8192)
            * 1024,
            browser_ready_timeout_seconds=_env_int(
                "OCR_BROWSER_READY_TIMEOUT", 60, minimum=1, maximum=300
            ),
            browser_poll_interval_seconds=_env_float(
                "OCR_BROWSER_POLL_INTERVAL", 0.25, minimum=0.05, maximum=5.0
            ),
            browser_request_timeout_seconds=_env_int(
                "OCR_BROWSER_REQUEST_TIMEOUT", 2, minimum=1, maximum=30
            ),
            docx_timeout_seconds=_env_int(
                "OCR_DOCX_TIMEOUT_SECONDS", 120, minimum=10, maximum=900
            ),
            libreoffice_path=libreoffice_path,
            libreoffice_version=libreoffice_version,
            libreoffice_timeout_seconds=_env_int(
                "OCR_LIBREOFFICE_TIMEOUT_SECONDS", 120, minimum=10, maximum=300
            ),
            max_embedded_images=_env_int(
                "OCR_MAX_EMBEDDED_IMAGES", 100, minimum=0, maximum=1000
            ),
            max_embedded_image_bytes=(
                _env_int("OCR_MAX_EMBEDDED_IMAGE_MB", 100, minimum=1, maximum=2048)
                * BYTES_PER_MEBIBYTE
            ),
            crop_padding_pixels=_env_int(
                "OCR_CROP_PADDING_PIXELS", 4, minimum=0, maximum=128
            ),
            correction_enabled=_env_bool("OCR_ENABLE_CORRECTION", True),
            adaptive_retry_enabled=_env_bool("OCR_ADAPTIVE_RETRY_ENABLED", True),
            adaptive_max_scale=_env_float(
                "OCR_ADAPTIVE_MAX_SCALE", 2.75, minimum=1.0, maximum=4.0
            ),
            low_confidence_threshold=_env_float(
                "OCR_LOW_CONFIDENCE_THRESHOLD", 0.90, minimum=0.0, maximum=1.0
            ),
            max_retry_regions_per_page=_env_int(
                "OCR_MAX_RETRY_REGIONS_PER_PAGE", 8, minimum=0, maximum=100
            ),
            table_quality_threshold=_env_float(
                "OCR_TABLE_QUALITY_THRESHOLD", 0.85, minimum=0.0, maximum=1.0
            ),
        )


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings.from_env()


def configure_runtime_environment() -> None:
    """Set supported Paddle runtime flags before the first Paddle import."""
    os.environ.setdefault("FLAGS_allocator_strategy", "auto_growth")
    os.environ.setdefault("FLAGS_eager_delete_tensor_gb", "0.0")
    os.environ.setdefault("FLAGS_eager_delete_scope", "True")
    os.environ.setdefault("PADDLE_PDX_DISABLE_MODEL_SOURCE_CHECK", "True")


configure_runtime_environment()
