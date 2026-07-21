from __future__ import annotations

import json
import zipfile
from dataclasses import replace
from pathlib import Path

import pytest

from app.core.config import get_settings
from app.documents.artifact_manifest import named_output_is_complete, validate_bundle
from app.documents.artifact_publishing import copy_history_archive, publish_named_output
from app.documents.artifact_quality import safe_docx_issue
from app.documents.naming import safe_output_stem


def artifact_settings(tmp_path: Path):
    return replace(
        get_settings(),
        output_dir=tmp_path / "output",
        upload_dir=tmp_path / "uploads",
        history_dir=tmp_path / "history",
        history_db_path=tmp_path / "history" / "history.sqlite3",
        log_dir=tmp_path / "logs",
    )


def write_docx(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("word/document.xml", "<w:document />")


def test_bundle_validation_and_named_output_policy(tmp_path: Path):
    settings = artifact_settings(tmp_path)
    bundle = tmp_path / "bundle"
    bundle.mkdir()
    with pytest.raises(RuntimeError, match="Markdown"):
        validate_bundle(bundle, "scan", require_docx=False)
    (bundle / "scan.md").write_text("# Scan", encoding="utf-8")
    with pytest.raises(RuntimeError, match="JSON"):
        validate_bundle(bundle, "scan", require_docx=False)
    (bundle / "scan.json").write_text("{}", encoding="utf-8")
    with pytest.raises(RuntimeError, match="DOCX"):
        validate_bundle(bundle, "scan", require_docx=True)
    write_docx(bundle / "scan.docx")
    validate_bundle(bundle, "scan", require_docx=True)

    target = settings.output_dir / "scan"
    assert not named_output_is_complete(settings=settings, output_stem="scan")
    write_docx(target / "scan.docx")
    assert named_output_is_complete(settings=settings, output_stem="scan")
    (target / "scan.json").write_text("not json", encoding="utf-8")
    assert named_output_is_complete(settings=settings, output_stem="scan")
    (target / "scan.json").write_text(json.dumps({"artifact_status": "partial"}), encoding="utf-8")
    assert not named_output_is_complete(settings=settings, output_stem="scan")


def test_private_archive_and_atomic_publish_replace_existing_output(tmp_path: Path):
    settings = artifact_settings(tmp_path)
    source = tmp_path / "source"
    source.mkdir()
    (source / "result.txt").write_text("first", encoding="utf-8")
    relative = copy_history_archive(source, settings=settings, job_id="a" * 32, output_stem="scan")
    archived = settings.history_dir / relative
    assert (archived / "result.txt").read_text(encoding="utf-8") == "first"
    (source / "result.txt").write_text("second", encoding="utf-8")
    copy_history_archive(source, settings=settings, job_id="a" * 32, output_stem="scan")
    assert (archived / "result.txt").read_text(encoding="utf-8") == "second"

    prior = settings.output_dir / "scan"
    prior.mkdir(parents=True)
    (prior / "result.txt").write_text("prior", encoding="utf-8")
    staged = tmp_path / "staged"
    staged.mkdir()
    (staged / "result.txt").write_text("published", encoding="utf-8")
    stale_rollback = settings.output_dir / ".staging" / ("b" * 32) / ".previous"
    stale_rollback.mkdir(parents=True)
    (stale_rollback / "old.txt").write_text("old", encoding="utf-8")
    published = publish_named_output(
        staged, settings=settings, output_stem="scan", job_id="b" * 32
    )
    assert published == settings.output_dir / "scan"
    assert (published / "result.txt").read_text(encoding="utf-8") == "published"
    assert not stale_rollback.exists()

    rollback_source = settings.output_dir / "rollback"
    rollback_source.mkdir()
    (rollback_source / "result.txt").write_text("keep", encoding="utf-8")
    (settings.output_dir / ".staging" / ("c" * 32)).mkdir(parents=True)
    with pytest.raises(FileNotFoundError):
        publish_named_output(
            tmp_path / "missing-stage",
            settings=settings,
            output_stem="rollback",
            job_id="c" * 32,
        )
    assert (rollback_source / "result.txt").read_text(encoding="utf-8") == "keep"


@pytest.mark.parametrize(
    ("message", "code"),
    [
        ("conversion timed out", "docx_generation_timeout"),
        ("unsafe external hyperlink", "unsafe_markdown_conversion"),
        ("unknown failure", "docx_generation_failed"),
    ],
)
def test_docx_issue_classification_and_safe_names(message: str, code: str):
    assert safe_docx_issue(RuntimeError(message))[0] == code
    assert safe_output_stem("", fallback="fallback", max_length=4) == "fall"
    assert safe_output_stem("very long filename.pdf", max_length=4) == "very"
