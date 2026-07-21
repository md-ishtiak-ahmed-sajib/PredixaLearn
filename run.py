"""Root compatibility launcher for the relocated OCR studio."""

from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
OCR_STUDIO = ROOT / "apps" / "ocr-studio"
os.environ.setdefault("PREDIXALEARN_TOOLS_ROOT", str(ROOT / ".tools"))
sys.path.insert(0, str(OCR_STUDIO))

from app.cli import main  # noqa: E402


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit(130)
