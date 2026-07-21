"""Explicit OCR runtime profiles for GPU, CPU, and low-memory devices."""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any

PROFILE_NAMES = {"auto", "full", "balanced_cpu", "low_memory"}


@dataclass(frozen=True, slots=True)
class ResourceProfile:
    name: str
    label: str
    reason: str
    recommended_memory_gb: int
    recommended_batch_size: int
    recommended_pdf_render_scale: float
    recommended_queue_limit: int
    features: dict[str, bool]

    def public(self) -> dict[str, Any]:
        return {
            "id": self.name,
            "label": self.label,
            "selection_reason": self.reason,
            "recommended_memory_gb": self.recommended_memory_gb,
            "recommended_settings": {
                "batch_size": self.recommended_batch_size,
                "pdf_render_scale": self.recommended_pdf_render_scale,
                "max_queued_jobs": self.recommended_queue_limit,
            },
            "features": self.features,
            "quality_notice": (
                "The selected profile is always reported. PredixaLearn never silently switches "
                "models or disables an advanced stage."
            ),
        }


def _memory_gb() -> float | None:
    if os.name == "nt":
        try:
            import ctypes

            class MemoryStatus(ctypes.Structure):
                _fields_ = [
                    ("length", ctypes.c_ulong),
                    ("memory_load", ctypes.c_ulong),
                    ("total_physical", ctypes.c_ulonglong),
                    ("available_physical", ctypes.c_ulonglong),
                    ("total_page_file", ctypes.c_ulonglong),
                    ("available_page_file", ctypes.c_ulonglong),
                    ("total_virtual", ctypes.c_ulonglong),
                    ("available_virtual", ctypes.c_ulonglong),
                    ("available_extended_virtual", ctypes.c_ulonglong),
                ]

            status = MemoryStatus()
            status.length = ctypes.sizeof(status)
            if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status)):
                return round(status.total_physical / (1024**3), 2)
        except (AttributeError, OSError):
            return None
    try:
        pages = os.sysconf("SC_PHYS_PAGES")
        page_size = os.sysconf("SC_PAGE_SIZE")
        return round((pages * page_size) / (1024**3), 2)
    except (AttributeError, OSError, ValueError):
        return None


def resolve_profile(configured: str, device: str) -> ResourceProfile:
    if configured not in PROFILE_NAMES:
        raise ValueError("OCR_RUNTIME_PROFILE must be auto, full, balanced_cpu, or low_memory")
    memory = _memory_gb()
    selected = configured
    reason = "Selected explicitly by OCR_RUNTIME_PROFILE."
    if configured == "auto":
        if device.startswith("gpu"):
            selected = "full"
            reason = "GPU OCR was requested, so the full profile is recommended."
        elif memory is not None and memory <= 8:
            selected = "low_memory"
            reason = f"Detected {memory:g} GiB physical memory without a requested GPU."
        else:
            selected = "balanced_cpu"
            reason = "No GPU was requested; the balanced CPU profile is recommended."
    profiles = {
        "full": ResourceProfile(
            "full",
            "Full GPU",
            reason,
            16,
            8,
            2.0,
            8,
            {
                "document_ocr": True,
                "vision_language": True,
                "formula_recognition": True,
                "chart_recognition": True,
                "local_accessibility_model": True,
            },
        ),
        "balanced_cpu": ResourceProfile(
            "balanced_cpu",
            "Balanced CPU",
            reason,
            12,
            4,
            1.5,
            4,
            {
                "document_ocr": True,
                "vision_language": True,
                "formula_recognition": False,
                "chart_recognition": False,
                "local_accessibility_model": False,
            },
        ),
        "low_memory": ResourceProfile(
            "low_memory",
            "Low memory",
            reason,
            8,
            1,
            1.25,
            1,
            {
                "document_ocr": True,
                "vision_language": False,
                "formula_recognition": False,
                "chart_recognition": False,
                "local_accessibility_model": False,
            },
        ),
    }
    return profiles[selected]


def profile_allows_workflow(profile_name: str, workflow_name: str) -> bool:
    return not (profile_name == "low_memory" and workflow_name == "vl_processing")
