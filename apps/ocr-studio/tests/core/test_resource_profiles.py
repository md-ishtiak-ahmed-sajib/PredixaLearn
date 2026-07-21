from __future__ import annotations

import pytest

from app.core.resource_profiles import profile_allows_workflow, resolve_profile


def test_explicit_runtime_profiles_report_capabilities_without_hidden_switches():
    full = resolve_profile("full", "gpu:0")
    balanced = resolve_profile("balanced_cpu", "cpu")
    low = resolve_profile("low_memory", "cpu")

    assert full.features["vision_language"] is True
    assert balanced.recommended_batch_size == 4
    assert low.recommended_batch_size == 1
    assert low.public()["quality_notice"].startswith("The selected profile is always reported")
    assert profile_allows_workflow("low_memory", "text_recognition") is True
    assert profile_allows_workflow("low_memory", "vl_processing") is False


def test_unknown_runtime_profile_is_rejected():
    with pytest.raises(ValueError, match="OCR_RUNTIME_PROFILE"):
        resolve_profile("silent-degradation", "cpu")
