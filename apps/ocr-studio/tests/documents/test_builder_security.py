from __future__ import annotations

import zipfile
from pathlib import Path

from PIL import Image

from app.documents._builder import (
    _docx_structure,
    _element_markdown,
    _escape_plain,
    _sanitize_external_hyperlinks,
)
from app.documents.docx import convert_markdown_to_docx


def test_unmatched_ocr_bracket_cannot_turn_figure_into_external_link(tmp_path: Path) -> None:
    assets = tmp_path / "assets"
    assets.mkdir()
    Image.new("RGB", (40, 30), "white").save(assets / "page-006-image-002.png")
    figure_markdown, _, _ = _element_markdown(
        {
            "type": "figure",
            "page_index": 5,
            "index": 0,
            "caption_context": "[Where cross sectional area is measured",
        },
        corrected_content="",
        figure_assets={"5:0": "assets/page-006-image-002.png"},
        figure_number=0,
    )
    assert "&#91;Where" in figure_markdown
    assert "![" in figure_markdown

    markdown = tmp_path / "scan.md"
    markdown.write_text(
        "# Scan\n\n## Source page 6\n\n" + figure_markdown + "\n\n"
        + _escape_plain("[Where cross sectional area is measured remains visible.") + "\n",
        encoding="utf-8",
    )
    docx = tmp_path / "scan.docx"
    convert_markdown_to_docx(markdown, docx, source_name="scan.pdf")

    with zipfile.ZipFile(docx) as archive:
        relationships = b"\n".join(
            archive.read(name) for name in archive.namelist() if name.endswith(".rels")
        )
        assert b'TargetMode="External"' not in relationships
        assert b"TargetMode='External'" not in relationships
        assert any(name.startswith("word/media/") for name in archive.namelist())


def _external_relationship_fixture(
    path: Path,
    *,
    source_name: str,
    rels_name: str,
    relationship_type: str = "hyperlink",
) -> None:
    namespace = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
    rel_namespace = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
    xml = (
        f'<w:document xmlns:w="{namespace}" xmlns:r="{rel_namespace}">'
        '<w:body><w:p><w:hyperlink r:id="rId7"><w:r><w:t>Visible OCR URL</w:t>'
        "</w:r></w:hyperlink></w:p></w:body></w:document>"
    ).encode()
    relationships = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        f'<Relationship Id="rId7" Type="http://schemas.openxmlformats.org/'
        f'officeDocument/2006/relationships/{relationship_type}" '
        'Target="https://example.invalid/resource" TargetMode="External"/>'
        "</Relationships>"
    ).encode()
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr(source_name, xml)
        archive.writestr(rels_name, relationships)


def test_external_hyperlinks_are_unwrapped_without_losing_visible_text(
    tmp_path: Path,
) -> None:
    docx = tmp_path / "external-link.docx"
    _external_relationship_fixture(
        docx,
        source_name="word/document.xml",
        rels_name="word/_rels/document.xml.rels",
    )

    assert _sanitize_external_hyperlinks(docx) == 1
    with zipfile.ZipFile(docx) as archive:
        document = archive.read("word/document.xml")
        relationships = archive.read("word/_rels/document.xml.rels")
    assert b"Visible OCR URL" in document
    assert b"hyperlink" not in document
    assert b'TargetMode="External"' not in relationships


def test_external_links_in_secondary_word_parts_are_unwrapped(tmp_path: Path) -> None:
    for part in ("header1", "footer1", "footnotes", "comments"):
        docx = tmp_path / f"{part}.docx"
        _external_relationship_fixture(
            docx,
            source_name=f"word/{part}.xml",
            rels_name=f"word/_rels/{part}.xml.rels",
        )
        assert _sanitize_external_hyperlinks(docx) == 1
        with zipfile.ZipFile(docx) as archive:
            assert b"Visible OCR URL" in archive.read(f"word/{part}.xml")


def test_external_images_templates_and_ole_are_rejected(tmp_path: Path) -> None:
    for relationship_type in ("image", "attachedTemplate", "oleObject"):
        docx = tmp_path / f"{relationship_type}.docx"
        _external_relationship_fixture(
            docx,
            source_name="word/document.xml",
            rels_name="word/_rels/document.xml.rels",
            relationship_type=relationship_type,
        )
        try:
            _sanitize_external_hyperlinks(docx)
        except RuntimeError as exc:
            assert "forbidden external resource" in str(exc)
        else:
            raise AssertionError(f"external {relationship_type} relationship was accepted")


def test_image_relationship_must_resolve_inside_word_media(tmp_path: Path) -> None:
    docx = tmp_path / "escaping-image.docx"
    with zipfile.ZipFile(docx, "w") as archive:
        archive.writestr(
            "word/document.xml",
            '<w:document xmlns:w="http://schemas.openxmlformats.org/'
            'wordprocessingml/2006/main"><w:body/></w:document>',
        )
        archive.writestr(
            "word/_rels/document.xml.rels",
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/'
            'relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/'
            'officeDocument/2006/relationships/image" Target="../../outside.png"/>'
            "</Relationships>",
        )

    _, unsafe = _docx_structure(docx)
    assert unsafe is True
