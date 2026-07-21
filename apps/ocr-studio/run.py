"""Compatibility launcher for PredixaLearn."""

from __future__ import annotations

import sys

from app.cli import main

if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit(130)
