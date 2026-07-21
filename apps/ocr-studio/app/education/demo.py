"""Original synthetic exam-paper fixture used by the one-click Judge Demo."""

from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

from app.core.config import Settings


def _font(size: int, *, bold: bool = False) -> ImageFont.ImageFont:
    candidates = [
        "C:/Windows/Fonts/arialbd.ttf" if bold else "C:/Windows/Fonts/arial.ttf",
        "C:/Windows/Fonts/calibrib.ttf" if bold else "C:/Windows/Fonts/calibri.ttf",
    ]
    for candidate in candidates:
        try:
            return ImageFont.truetype(candidate, size)
        except OSError:
            continue
    return ImageFont.load_default()


def _paper(page: int) -> Image.Image:
    image = Image.new("RGB", (1654, 2339), (249, 246, 237))
    draw = ImageDraw.Draw(image)
    title = _font(42, bold=True)
    body = _font(29)
    heading = _font(32, bold=True)
    draw.rectangle((90, 90, 1564, 2249), outline=(42, 72, 96), width=4)
    draw.text((155, 150), "PredixaLearn Synthetic Civil Engineering Examination", fill=(18, 47, 71), font=title)
    draw.text((155, 215), "Original demo material — for local OCR evaluation only", fill=(62, 91, 108), font=body)
    if page == 1:
        rows = [
            "1(a) Explain the principle of open-channel flow. (5 marks)",
            "1(b) A rectangular channel has width b = 4 m and depth y = 2 m. Calculate its area. (5 marks)",
            "2(a) Use Q = A × V to determine discharge when A = 8 m² and V = 1.5 m/s. [6]",
            "2(b) State two limitations of a Manning-equation estimate. (4 marks)",
        ]
        y = 360
        for row in rows:
            draw.text((170, y), row, fill=(25, 39, 48), font=heading if row.startswith("1") else body)
            y += 150
        draw.text((170, 1050), "Table 1 — Field observations", fill=(18, 47, 71), font=heading)
        left, top, right, bottom = 170, 1115, 1460, 1515
        for x in (left, 620, 1010, right):
            draw.line((x, top, x, bottom), fill=(44, 88, 112), width=3)
        for y_line in (top, 1215, 1315, 1415, bottom):
            draw.line((left, y_line, right, y_line), fill=(44, 88, 112), width=3)
        for x, text in ((205, "Reach"), (675, "Velocity"), (1060, "Depth")):
            draw.text((x, 1145), text, fill=(18, 47, 71), font=body)
        values = [("A", "1.2 m/s", "1.5 m"), ("B", "1.5 m/s", "2.0 m"), ("C", "1.7 m/s", "2.2 m")]
        for index, row in enumerate(values):
            y = 1240 + index * 100
            for x, value in zip((205, 675, 1060), row, strict=True):
                draw.text((x, y), value, fill=(25, 39, 48), font=body)
        draw.polygon([(1050, 1750), (1370, 1900), (1120, 2040)], outline=(25, 94, 142), fill=(211, 232, 243))
        draw.text((170, 1820), "Figure 1 — Not-to-scale channel section", fill=(18, 47, 71), font=body)
    else:
        draw.text((170, 360), "3(a) Compare subcritical and supercritical flow. (8 marks)", fill=(25, 39, 48), font=heading)
        draw.text((170, 540), "3(b) Describe one field method for checking a hydraulic gradient. (6 marks)", fill=(25, 39, 48), font=body)
        draw.text((170, 760), "4(a) The energy relationship is E = y + V²/(2g). Explain each term. [6]", fill=(25, 39, 48), font=body)
        draw.text((170, 960), "4(b) Draw and label a simple specific-energy curve. (6 marks)", fill=(25, 39, 48), font=body)
        draw.line((250, 1800, 1330, 1800), fill=(37, 84, 119), width=5)
        draw.line((350, 2050, 350, 1300), fill=(37, 84, 119), width=5)
        draw.arc((420, 1400, 1240, 2050), start=190, end=340, fill=(28, 128, 176), width=7)
        draw.text((1280, 1815), "Depth", fill=(18, 47, 71), font=body)
        draw.text((210, 1280), "Energy", fill=(18, 47, 71), font=body)
    return image


def create_judge_demo(settings: Settings) -> Path:
    """Create an original, temporary PDF input for the standard OCR queue."""

    settings.upload_dir.mkdir(parents=True, exist_ok=True)
    destination = settings.upload_dir / "predixalearn-judge-demo.synthetic.pdf"
    pages = [_paper(1), _paper(2).rotate(0.7, resample=Image.Resampling.BICUBIC, fillcolor=(249, 246, 237))]
    pages[0].save(destination, format="PDF", resolution=180.0, save_all=True, append_images=pages[1:])
    return destination
