from __future__ import annotations

import os
import subprocess
import threading
import time

import psutil
import pytest
from PIL import Image

from app.workflows.ocr_text import run_text_recognition


@pytest.mark.gpu_stress
@pytest.mark.skipif(os.getenv("RUN_GPU_STRESS") != "1", reason="set RUN_GPU_STRESS=1")
def test_100_page_pdf_is_page_bounded_and_within_gpu_budget(tmp_path):
    pages = [Image.new("RGB", (200, 200), "white") for _ in range(100)]
    pdf_path = tmp_path / "blank-100-pages.pdf"
    pages[0].save(pdf_path, save_all=True, append_images=pages[1:], resolution=72)
    for page in pages:
        page.close()

    process = psutil.Process()
    initial_rss = process.memory_info().rss
    peak_rss = initial_rss
    peak_gpu_mb = 0
    stop = threading.Event()

    def monitor():
        nonlocal peak_rss, peak_gpu_mb
        while not stop.is_set():
            peak_rss = max(peak_rss, process.memory_info().rss)
            check = subprocess.run(
                [
                    "nvidia-smi",
                    "--query-gpu=memory.used",
                    "--format=csv,noheader,nounits",
                ],
                capture_output=True,
                text=True,
                timeout=5,
                check=False,
            )
            if check.returncode == 0:
                values = [
                    int(value.strip()) for value in check.stdout.splitlines() if value.strip()
                ]
                if values:
                    peak_gpu_mb = max(peak_gpu_mb, max(values))
            time.sleep(0.1)

    watcher = threading.Thread(target=monitor, daemon=True)
    watcher.start()
    try:
        result = run_text_recognition(pdf_path)
    finally:
        stop.set()
        watcher.join(timeout=10)

    assert result["page_count"] == 100
    assert peak_gpu_mb < 5_500
    assert peak_rss - initial_rss < 1_500 * 1024 * 1024
