"""Scale-independent bounding-box helpers shared by OCR and document export."""

from __future__ import annotations

import math
from collections.abc import Sequence
from typing import Any


def bbox_values(raw: Any) -> tuple[float, float, float, float] | None:
    if not isinstance(raw, Sequence) or isinstance(raw, (str, bytes)):
        return None
    values: list[float] = []
    for value in raw:
        if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
            values.extend(float(item) for item in value if isinstance(item, (int, float)))
        elif isinstance(value, (int, float)):
            values.append(float(value))
    if len(values) == 4:
        x1, y1, x2, y2 = values
    elif len(values) >= 8 and len(values) % 2 == 0:
        x1, y1 = min(values[0::2]), min(values[1::2])
        x2, y2 = max(values[0::2]), max(values[1::2])
    else:
        return None
    if not all(math.isfinite(value) for value in (x1, y1, x2, y2)):
        return None
    return (x1, y1, x2, y2) if x1 < x2 and y1 < y2 else None


def normalized_bbox(raw: Any, width: int, height: int) -> list[float]:
    values = bbox_values(raw)
    if values is None or width < 1 or height < 1:
        return []
    x1, y1, x2, y2 = values
    return [
        round(max(0.0, min(1.0, x1 / width)), 7),
        round(max(0.0, min(1.0, y1 / height)), 7),
        round(max(0.0, min(1.0, x2 / width)), 7),
        round(max(0.0, min(1.0, y2 / height)), 7),
    ]


def preferred_bbox(item: Any) -> Any:
    if isinstance(item, dict):
        normalized = item.get("normalized_bbox")
        if bbox_values(normalized):
            return normalized
        return item.get("bbox")
    return item


def overlap_ratio(inner: Any, outer: Any) -> float:
    first = bbox_values(preferred_bbox(inner))
    second = bbox_values(preferred_bbox(outer))
    if first is None or second is None:
        return 0.0
    left, top = max(first[0], second[0]), max(first[1], second[1])
    right, bottom = min(first[2], second[2]), min(first[3], second[3])
    intersection = max(0.0, right - left) * max(0.0, bottom - top)
    area = max(0.0, first[2] - first[0]) * max(0.0, first[3] - first[1])
    return intersection / area if area else 0.0


def bbox_iou(first: Any, second: Any) -> float:
    a = bbox_values(preferred_bbox(first))
    b = bbox_values(preferred_bbox(second))
    if a is None or b is None:
        return 0.0
    left, top = max(a[0], b[0]), max(a[1], b[1])
    right, bottom = min(a[2], b[2]), min(a[3], b[3])
    intersection = max(0.0, right - left) * max(0.0, bottom - top)
    a_area = (a[2] - a[0]) * (a[3] - a[1])
    b_area = (b[2] - b[0]) * (b[3] - b[1])
    union = a_area + b_area - intersection
    return intersection / union if union else 0.0


def bbox_center(raw: Any) -> tuple[float, float] | None:
    value = bbox_values(preferred_bbox(raw))
    if value is None:
        return None
    return (value[0] + value[2]) / 2, (value[1] + value[3]) / 2


def union_bbox(values: Sequence[Any]) -> list[float]:
    boxes = [bbox_values(preferred_bbox(value)) for value in values]
    valid = [box for box in boxes if box is not None]
    if not valid:
        return []
    return [
        min(box[0] for box in valid),
        min(box[1] for box in valid),
        max(box[2] for box in valid),
        max(box[3] for box in valid),
    ]
