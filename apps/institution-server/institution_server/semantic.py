"""Privacy-preserving tenant-local hashing embeddings."""

from __future__ import annotations

import hashlib
import math
import re

DIMENSIONS = 128
TOKEN = re.compile(r"[\w'-]{2,}", re.UNICODE)


def embedding(text: str) -> list[float]:
    vector = [0.0] * DIMENSIONS
    for token in TOKEN.findall(text.casefold()):
        digest = hashlib.blake2b(token.encode(), digest_size=16).digest()
        index = int.from_bytes(digest[:4], "big") % DIMENSIONS
        sign = 1.0 if digest[4] & 1 else -1.0
        vector[index] += sign
    magnitude = math.sqrt(sum(value * value for value in vector))
    return [value / magnitude for value in vector] if magnitude else vector


def cosine(left: list[float], right: list[float]) -> float:
    return sum(a * b for a, b in zip(left, right, strict=True))


def pgvector_literal(value: list[float]) -> str:
    return "[" + ",".join(f"{item:.8f}" for item in value) + "]"
