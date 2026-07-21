from __future__ import annotations

import threading
from dataclasses import replace

import numpy as np
import pytest

from app.core.config import Settings, get_settings
from app.core.engine import OCREngineManager
from app.core.errors import InputLimitError
from app.workflows import pdf_utils


class FakeBitmap:
    def __init__(self, array, mode="BGR"):
        self.array = array
        self.mode = mode
        self.closed = False

    def to_numpy(self):
        return self.array

    def close(self):
        self.closed = True


class FakePage:
    def __init__(self, bitmap, size=(10, 10)):
        self.bitmap = bitmap
        self.size = size
        self.closed = False

    def get_size(self):
        return self.size

    def render(self, scale):
        assert scale == 2.0
        return self.bitmap

    def close(self):
        self.closed = True


class FakeDocument:
    def __init__(self, pages):
        self.pages = pages
        self.closed = False

    def __len__(self):
        return len(self.pages)

    def __getitem__(self, index):
        return self.pages[index]

    def close(self):
        self.closed = True


def test_pdf_bitmap_preserves_native_bgr_and_converts_rgb():
    native = FakeBitmap(np.array([[[10, 20, 30]]], dtype=np.uint8), "BGR")
    rgb = FakeBitmap(np.array([[[30, 20, 10]]], dtype=np.uint8), "RGB")
    assert pdf_utils._bitmap_to_bgr(native).tolist() == [[[10, 20, 30]]]
    assert pdf_utils._bitmap_to_bgr(rgb).tolist() == [[[10, 20, 30]]]


def test_pdf_stream_closes_all_pdfium_resources(monkeypatch):
    created = []

    def create_document(path):
        del path
        bitmap = FakeBitmap(np.array([[[1, 2, 3]]], dtype=np.uint8))
        document = FakeDocument([FakePage(bitmap)])
        created.append(document)
        return document

    monkeypatch.setattr(pdf_utils.pdfium, "PdfDocument", create_document)
    events = []
    pages = list(
        pdf_utils.iter_pdf_pages(
            "fake.pdf",
            progress_callback=lambda **event: events.append(event),
        )
    )

    assert pages[0].image.tolist() == [[[1, 2, 3]]]
    assert len(created) == 2
    assert all(document.closed for document in created)
    assert all(page.closed for document in created for page in document.pages)
    assert created[1].pages[0].bitmap.closed
    assert [event["stage"] for event in events] == ["pdf_inspection", "pdf_rendering"]
    assert events[-1]["current_page"] == 1
    assert events[-1]["total_pages"] == 1


def test_pdf_page_and_pixel_limits(monkeypatch):
    settings = replace(get_settings(), max_pdf_pages=1, max_image_pixels=100)
    document = FakeDocument(
        [
            FakePage(FakeBitmap(np.zeros((1, 1, 3), dtype=np.uint8))),
            FakePage(FakeBitmap(np.zeros((1, 1, 3), dtype=np.uint8))),
        ]
    )
    monkeypatch.setattr(pdf_utils.pdfium, "PdfDocument", lambda path: document)
    with pytest.raises(InputLimitError, match="2 pages"):
        pdf_utils.inspect_pdf("fake.pdf", settings)
    assert document.closed


def test_settings_reject_non_loopback_and_invalid_boolean(monkeypatch):
    monkeypatch.setenv("OCR_HOST", "0.0.0.0")
    with pytest.raises(ValueError, match="loopback"):
        Settings.from_env()
    monkeypatch.setenv("OCR_HOST", "127.0.0.1")
    monkeypatch.setenv("OCR_USE_DOC_UNWARPING", "sometimes")
    with pytest.raises(ValueError, match="must be a boolean"):
        Settings.from_env()


def test_desktop_settings_require_loopback_https_and_a_launch_secret(monkeypatch, tmp_path):
    certificate = tmp_path / "server.pem"
    key = tmp_path / "server-key.pem"
    certificate.write_text("certificate", encoding="utf-8")
    key.write_text("key", encoding="utf-8")
    monkeypatch.setenv("OCR_DESKTOP_MODE", "1")
    monkeypatch.setenv("OCR_DESKTOP_CONTROL_TOKEN", "x" * 32)
    monkeypatch.setenv("OCR_DESKTOP_TLS_CERT_PATH", str(certificate))
    monkeypatch.setenv("OCR_DESKTOP_TLS_KEY_PATH", str(key))
    monkeypatch.setenv("OCR_HOST", "127.0.0.1")
    monkeypatch.setenv("OCR_PORT", "443")
    settings = Settings.from_env()
    assert settings.desktop_mode is True
    assert settings.browser_origin == "https://app.predixalearn.com"

    monkeypatch.setenv("OCR_PORT", "8443")
    with pytest.raises(ValueError, match="127.0.0.1:443"):
        Settings.from_env()


def test_device_alias_prefers_new_setting(monkeypatch):
    monkeypatch.setenv("OCR_CUDA_DEVICE", "gpu:7")
    monkeypatch.delenv("OCR_DEVICE", raising=False)
    assert Settings.from_env().device == "gpu:7"
    monkeypatch.setenv("OCR_DEVICE", "cpu")
    assert Settings.from_env().device == "cpu"


def test_settings_reject_invalid_device_and_structure_version(monkeypatch):
    monkeypatch.setenv("OCR_DEVICE", "cuda:all")
    with pytest.raises(ValueError, match="OCR_DEVICE"):
        Settings.from_env()
    monkeypatch.setenv("OCR_DEVICE", "cpu")
    monkeypatch.setenv("STRUCTURE_OCR_VERSION", "PP-OCRv6")
    with pytest.raises(ValueError, match="STRUCTURE_OCR_VERSION"):
        Settings.from_env()


def test_settings_default_to_english_high_resolution_and_validate_language(monkeypatch):
    for name in (
        "OCR_LANG",
        "OCR_PDF_RENDER_SCALE",
        "OCR_ADAPTIVE_MAX_SCALE",
        "OCR_TABLE_QUALITY_THRESHOLD",
    ):
        monkeypatch.delenv(name, raising=False)
    settings = Settings.from_env()
    assert settings.ocr_lang == "en"
    assert settings.pdf_render_scale == 2.0
    assert settings.adaptive_max_scale == 2.75
    assert settings.table_quality_threshold == 0.85

    # French exists in the vendor model catalog, but is intentionally disabled
    # by the current English-only product policy.
    monkeypatch.setenv("OCR_LANG", "fr")
    with pytest.raises(ValueError, match="not supported"):
        Settings.from_env()


def test_settings_restrict_libreoffice_to_the_project(monkeypatch, tmp_path):
    monkeypatch.setenv("OCR_LIBREOFFICE_PATH", str(tmp_path / "soffice.com"))
    with pytest.raises(ValueError, match="inside the project"):
        Settings.from_env()


@pytest.mark.parametrize("value", ["9", "301"])
def test_settings_reject_invalid_libreoffice_timeout(monkeypatch, value):
    monkeypatch.setenv("OCR_LIBREOFFICE_TIMEOUT_SECONDS", value)
    with pytest.raises(ValueError, match="OCR_LIBREOFFICE_TIMEOUT_SECONDS"):
        Settings.from_env()


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("OCR_ADAPTIVE_MAX_SCALE", "0.5"),
        ("OCR_LOW_CONFIDENCE_THRESHOLD", "1.2"),
        ("OCR_MAX_RETRY_REGIONS_PER_PAGE", "101"),
        ("OCR_TABLE_QUALITY_THRESHOLD", "-0.1"),
    ],
)
def test_settings_reject_invalid_adaptive_quality_limits(monkeypatch, name, value):
    monkeypatch.setenv(name, value)
    with pytest.raises(ValueError, match=name):
        Settings.from_env()


def test_vl_first_session_does_not_self_deadlock():
    class TestManager(OCREngineManager):
        def _create_engine(self, name, **kwargs):
            del kwargs
            return object()

    manager = TestManager()
    finished = threading.Event()

    def initialize():
        with manager.session("vl"):
            finished.set()

    thread = threading.Thread(target=initialize, daemon=True)
    thread.start()
    thread.join(timeout=1)
    assert finished.is_set()


def test_manager_keeps_only_the_active_pipeline():
    class TestManager(OCREngineManager):
        def _create_engine(self, name, **kwargs):
            del kwargs
            return {"name": name}

    manager = TestManager()
    with manager.session("ocr"):
        pass
    assert manager.status["ocr_engine"] is True
    with manager.session("structure"):
        pass
    assert manager.status == {
        "ocr_engine": False,
        "structure_engine": True,
        "vl_engine": False,
    }
