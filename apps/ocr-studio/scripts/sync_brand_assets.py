"""Generate browser-facing PredixaLearn logo assets from the canonical masters."""

from __future__ import annotations

import shutil
from pathlib import Path

from PIL import Image

ROOT = Path(__file__).resolve().parents[3]
MASTERS = ROOT / "packages" / "brand-assets" / "source" / "ocr-logo-masters"
MARK = MASTERS / "predixalearn-mark-1024.png"
OCR_STATIC = ROOT / "apps" / "ocr-studio" / "web" / "static"


def write_mark(size: int) -> None:
    destination = OCR_STATIC / f"predixalearn-mark-{size}.png"
    with Image.open(MARK) as image:
        image.convert("RGBA").resize((size, size), Image.Resampling.LANCZOS).save(destination)


def main() -> None:
    if not MARK.is_file():
        raise SystemExit("The canonical PredixaLearn mark is missing.")
    OCR_STATIC.mkdir(parents=True, exist_ok=True)
    for size in (32, 48, 180):
        write_mark(size)
    shutil.copy2(MARK, OCR_STATIC / "predixalearn-mark.png")
    print("Synchronized PredixaLearn brand assets.")


if __name__ == "__main__":
    main()
