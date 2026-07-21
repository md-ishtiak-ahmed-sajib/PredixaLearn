"""Lazy PaddleOCR engine lifecycle with serialized single-GPU inference."""

from __future__ import annotations

import gc
import logging
import threading
from contextlib import contextmanager
from typing import Iterator, Literal

from app.core.config import Settings, get_settings

logger = logging.getLogger("predixalearn.engine")
EngineName = Literal["ocr", "structure", "vl"]


class OCREngineManager:
    """Own one instance of each pipeline and one shared GPU inference lock."""

    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()
        self._engines: dict[EngineName, object | None] = {
            "ocr": None,
            "structure": None,
            "vl": None,
        }
        self._variants: dict[EngineName, tuple[object, ...] | None] = {
            name: None for name in self._engines
        }
        self._init_locks = {name: threading.Lock() for name in self._engines}
        self._inference_lock = threading.RLock()

    def _release_other_engines(self, keep: EngineName) -> None:
        released = False
        for name, engine in self._engines.items():
            if name != keep and engine is not None:
                self._engines[name] = None
                self._variants[name] = None
                released = True
        if not released:
            return
        logger.info("Released inactive OCR engines before loading %s", keep)
        gc.collect()
        try:
            import paddle

            if paddle.device.is_compiled_with_cuda():
                paddle.device.cuda.empty_cache()
        except (ImportError, RuntimeError):
            logger.debug("CUDA cache cleanup unavailable", exc_info=True)

    def _variant(
        self,
        name: EngineName,
        *,
        language: str | None,
        document_profile: str,
    ) -> tuple[object, ...]:
        if name == "vl":
            return (self.settings.vl_pipeline_version,)
        selected_language = language or self.settings.ocr_lang
        if name == "structure":
            formula_recognition = self.settings.structure_use_formula_recognition or (
                document_profile in {"auto", "exam"}
            )
            return selected_language, formula_recognition
        return (selected_language,)

    def _create_engine(
        self,
        name: EngineName,
        *,
        language: str | None = None,
        document_profile: str = "auto",
    ) -> object:
        settings = self.settings
        selected_language = language or settings.ocr_lang
        if name == "ocr":
            from paddleocr import PaddleOCR

            return PaddleOCR(
                lang=selected_language,
                ocr_version=settings.ocr_version,
                device=settings.device,
                use_doc_orientation_classify=settings.ocr_use_doc_orientation,
                use_doc_unwarping=settings.ocr_use_doc_unwarping,
                use_textline_orientation=settings.ocr_use_textline_orientation,
                text_recognition_batch_size=settings.text_recognition_batch_size,
            )
        if name == "structure":
            from paddleocr import PPStructureV3

            return PPStructureV3(
                lang=selected_language,
                ocr_version=settings.structure_ocr_version,
                device=settings.device,
                use_doc_orientation_classify=settings.structure_use_doc_orientation,
                use_doc_unwarping=settings.structure_use_unwarping,
                use_textline_orientation=settings.structure_use_textline_orientation,
                use_chart_recognition=settings.structure_use_chart_recognition,
                use_formula_recognition=(
                    settings.structure_use_formula_recognition
                    or document_profile in {"auto", "exam"}
                ),
            )
        if name == "vl":
            from paddleocr import PaddleOCRVL

            return PaddleOCRVL(
                pipeline_version=settings.vl_pipeline_version,
                device=settings.device,
            )
        raise ValueError(f"Unknown engine: {name}")

    def get_engine(
        self,
        name: EngineName,
        *,
        language: str | None = None,
        document_profile: str = "auto",
    ) -> object:
        requested_variant = self._variant(
            name,
            language=language,
            document_profile=document_profile,
        )
        with self._inference_lock:
            engine = self._engines[name]
            if engine is not None and self._variants[name] == requested_variant:
                return engine
            with self._init_locks[name]:
                engine = self._engines[name]
                if engine is not None and self._variants[name] != requested_variant:
                    logger.info("Releasing %s engine before changing its model variant", name)
                    self._engines[name] = None
                    self._variants[name] = None
                    engine = None
                    gc.collect()
                if engine is None:
                    self._release_other_engines(name)
                    logger.info("Initializing %s engine", name)
                    engine = self._create_engine(
                        name,
                        language=language,
                        document_profile=document_profile,
                    )
                    self._engines[name] = engine
                    self._variants[name] = requested_variant
                    logger.info("%s engine ready", name)
            return engine

    @contextmanager
    def session(
        self,
        name: EngineName,
        *,
        language: str | None = None,
        document_profile: str = "auto",
    ) -> Iterator[object]:
        """Serialize initialization and inference across the single GPU."""
        with self._inference_lock:
            yield self.get_engine(
                name,
                language=language,
                document_profile=document_profile,
            )

    def get_ocr_engine(self) -> object:
        return self.get_engine("ocr")

    def get_structure_engine(self) -> object:
        return self.get_engine("structure")

    def get_vl_engine(self) -> object:
        return self.get_engine("vl")

    @property
    def status(self) -> dict[str, bool]:
        return {f"{name}_engine": value is not None for name, value in self._engines.items()}

    def shutdown(self) -> None:
        with self._inference_lock:
            for name in self._engines:
                self._engines[name] = None
                self._variants[name] = None
            gc.collect()
            try:
                import paddle

                if paddle.device.is_compiled_with_cuda():
                    paddle.device.cuda.empty_cache()
            except (ImportError, RuntimeError):
                logger.debug("CUDA cache cleanup unavailable", exc_info=True)


_manager: OCREngineManager | None = None
_manager_lock = threading.Lock()


def get_manager() -> OCREngineManager:
    global _manager
    if _manager is None:
        with _manager_lock:
            if _manager is None:
                _manager = OCREngineManager()
    return _manager


def reset_manager() -> None:
    """Release the process manager; primarily useful for app/test teardown."""
    global _manager
    with _manager_lock:
        if _manager is not None:
            _manager.shutdown()
        _manager = None
