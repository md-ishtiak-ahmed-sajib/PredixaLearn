from __future__ import annotations

import os
import subprocess
from dataclasses import replace
from pathlib import Path

import pytest
from PIL import Image, ImageDraw

from app.core.config import get_settings
from app.documents import libreoffice


def local_settings(tmp_path: Path):
    project_root = tmp_path / "project"
    executable = project_root / ".tools" / "libreoffice" / "26.2.4" / "program" / "soffice.com"
    executable.parent.mkdir(parents=True)
    executable.write_bytes(b"MZ")
    return replace(
        get_settings(),
        project_root=project_root,
        libreoffice_path=executable,
        libreoffice_version="26.2.4",
        libreoffice_timeout_seconds=30,
    )


def test_resolver_rejects_an_executable_outside_the_project(tmp_path):
    settings = local_settings(tmp_path)
    outside = tmp_path / "outside" / "soffice.com"
    outside.parent.mkdir()
    outside.write_bytes(b"MZ")

    with pytest.raises(libreoffice.LibreOfficeValidationError, match="configuration"):
        libreoffice.resolve_libreoffice_executable(
            replace(settings, libreoffice_path=outside)
        )


def test_headless_validation_uses_isolated_profile_and_cleans_qa_files(
    tmp_path, monkeypatch
):
    settings = local_settings(tmp_path)
    document = tmp_path / "document.docx"
    document.write_bytes(b"docx fixture")
    work_dir = tmp_path / "job staging"
    work_dir.mkdir()
    monkeypatch.setenv("PADDLE_TEST_SENTINEL", "must-not-leak")
    calls: list[tuple[list[str], dict[str, str] | None]] = []
    expected_pages: list[int] = []

    def fake_run(command, *, timeout_seconds, cwd, environment=None):
        del timeout_seconds, cwd
        calls.append((command, environment))
        if "--version" in command:
            return subprocess.CompletedProcess(
                command, 0, "LibreOffice 26.2.4.2 10(Build:2)", ""
            )
        output_dir = Path(command[command.index("--outdir") + 1])
        output_dir.mkdir(parents=True, exist_ok=True)
        (output_dir / "document.pdf").write_bytes(b"%PDF-" + b"x" * 2_000)
        return subprocess.CompletedProcess(command, 0, "convert complete", "")

    def fake_validate(path, expected_source_pages):
        assert path.name == "document.pdf"
        expected_pages.append(expected_source_pages)
        return 3

    libreoffice.clear_libreoffice_cache()
    monkeypatch.setattr(libreoffice, "_run_process", fake_run)
    monkeypatch.setattr(libreoffice, "_validate_rendered_pdf", fake_validate)

    result = libreoffice.validate_docx_with_libreoffice(
        document,
        work_dir=work_dir,
        expected_source_pages=2,
        settings=settings,
    )

    assert result.status == "passed"
    assert result.version == "26.2.4.2"
    assert result.rendered_pages == 3
    assert expected_pages == [2]
    conversion, environment = calls[-1]
    validation_input = Path(conversion[-1])
    assert validation_input.name == document.name
    assert validation_input != document.resolve()
    assert validation_input.parent.parent.name.startswith(".libreoffice-")
    profile_argument = next(item for item in conversion if item.startswith("-env:UserInstallation="))
    assert profile_argument.startswith("-env:UserInstallation=file:///")
    assert "%20" in profile_argument
    assert environment is not None
    assert Path(environment["HOME"]).is_relative_to(work_dir)
    assert Path(environment["TEMP"]).is_relative_to(work_dir)
    if os.name == "nt":
        assert "PADDLE_TEST_SENTINEL" not in environment
        assert environment["PATH"].split(os.pathsep)[0] == str(settings.libreoffice_path.parent)
    assert not list(work_dir.glob(".libreoffice-*"))
    libreoffice.clear_libreoffice_cache()


def test_startup_probe_waits_for_engine_warmup(monkeypatch):
    states = iter(("not_started", "initializing", "ready"))
    sleeps: list[float] = []

    monkeypatch.setattr(
        libreoffice,
        "get_warmup_status",
        lambda: {"status": next(states)},
    )
    monkeypatch.setattr(libreoffice.time, "sleep", sleeps.append)

    libreoffice._wait_for_engine_warmup()

    assert sleeps == [
        libreoffice._WARMUP_POLL_SECONDS,
        libreoffice._WARMUP_POLL_SECONDS,
    ]


def test_unexpected_renderer_failure_returns_safe_warning_and_cleans(tmp_path, monkeypatch):
    settings = local_settings(tmp_path)
    document = tmp_path / "secret document.docx"
    document.write_bytes(b"docx fixture")
    work_dir = tmp_path / "work"
    work_dir.mkdir()

    def fail(*args, **kwargs):
        del args, kwargs
        raise RuntimeError(r"D:\private\unexpected renderer output")

    libreoffice.clear_libreoffice_cache()
    monkeypatch.setattr(libreoffice, "_run_process", fail)
    result = libreoffice.validate_docx_with_libreoffice(
        document,
        work_dir=work_dir,
        expected_source_pages=1,
        settings=settings,
    )

    assert result.status == "failed"
    assert result.warning == "LibreOffice document validation failed unexpectedly"
    assert "private" not in result.warning.lower()
    assert not list(work_dir.glob(".libreoffice-*"))
    libreoffice.clear_libreoffice_cache()


def test_pdf_observability_accepts_one_safe_late_named_candidate(tmp_path):
    output_dir = tmp_path / "out"
    output_dir.mkdir()
    candidate = output_dir / "renderer-selected-name.pdf"
    candidate.write_bytes(b"%PDF-" + b"x" * 2_000)

    observed = libreoffice._wait_for_converted_pdf(
        output_dir,
        output_dir / "expected-name.pdf",
    )

    assert observed == candidate


def test_conversion_retries_once_with_a_fresh_profile(tmp_path, monkeypatch):
    settings = local_settings(tmp_path)
    document = tmp_path / "retry.docx"
    document.write_bytes(b"docx fixture")
    work_dir = tmp_path / "work"
    work_dir.mkdir()
    conversions = 0

    def fake_run(command, *, timeout_seconds, cwd, environment=None):
        nonlocal conversions
        del timeout_seconds, cwd, environment
        if "--version" in command:
            return subprocess.CompletedProcess(command, 0, "LibreOffice 26.2.4.2", "")
        conversions += 1
        if conversions == 2:
            output_dir = Path(command[command.index("--outdir") + 1])
            (output_dir / "retry.pdf").write_bytes(b"%PDF-" + b"x" * 2_000)
        return subprocess.CompletedProcess(command, 0, "", "")

    libreoffice.clear_libreoffice_cache()
    monkeypatch.setattr(libreoffice, "_run_process", fake_run)
    monkeypatch.setattr(libreoffice, "_PDF_OBSERVABILITY_SECONDS", 0.0)
    monkeypatch.setattr(libreoffice, "_validate_rendered_pdf", lambda path, pages: pages)

    result = libreoffice.validate_docx_with_libreoffice(
        document,
        work_dir=work_dir,
        expected_source_pages=1,
        settings=settings,
    )

    assert result.status == "passed"
    assert conversions == 2
    assert not list(work_dir.glob(".libreoffice-*"))
    libreoffice.clear_libreoffice_cache()


def test_rendered_pdf_requires_ink_and_all_source_pages(tmp_path):
    readable = tmp_path / "readable.pdf"
    image = Image.new("RGB", (600, 800), "white")
    drawing = ImageDraw.Draw(image)
    drawing.rectangle((30, 30, 570, 90), fill="black")
    image.save(readable, "PDF", resolution=72)

    assert libreoffice._validate_rendered_pdf(readable, 1) == 1
    with pytest.raises(libreoffice.LibreOfficeValidationError, match="fewer pages"):
        libreoffice._validate_rendered_pdf(readable, 2)

    blank = tmp_path / "blank.pdf"
    Image.new("RGB", (600, 800), "white").save(blank, "PDF", resolution=72)
    with pytest.raises(libreoffice.LibreOfficeValidationError, match="appears blank"):
        libreoffice._validate_rendered_pdf(blank, 1)
