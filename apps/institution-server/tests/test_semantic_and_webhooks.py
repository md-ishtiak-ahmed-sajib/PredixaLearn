from __future__ import annotations

from institution_server.semantic import cosine, embedding
from institution_server.storage import LocalObjectStorage, validate_key
from institution_server.webhooks import signature


def test_local_hashing_embeddings_are_deterministic_and_related_text_scores_higher():
    query = embedding("force acceleration motion")
    related = embedding("motion acceleration force")
    unrelated = embedding("poetry rhyme metaphor")
    assert embedding("force acceleration motion") == query
    assert cosine(query, related) > cosine(query, unrelated)


def test_webhook_signature_binds_timestamp_and_body():
    first = signature("secret-at-least-32-characters-long", "100", b'{"event":1}')
    assert first == signature("secret-at-least-32-characters-long", "100", b'{"event":1}')
    assert first != signature("secret-at-least-32-characters-long", "101", b'{"event":1}')
    assert first != signature("secret-at-least-32-characters-long", "100", b'{"event":2}')


def test_local_storage_is_atomic_checksum_aware_and_traversal_safe(tmp_path):
    storage = LocalObjectStorage(tmp_path / "objects")
    stored = storage.put("tenant-a/evidence/item.json", b"{}", content_type="application/json")
    assert stored["sha256"] == "44136fa355b3678a1146ad16f7e8649e94fb4fc21fe77e8310c060f61caaff8a"
    assert storage.read("tenant-a/evidence/item.json") == b"{}"
    storage.healthcheck()
    for key in ("../secret", "/../secret", ".hidden"):
        try:
            validate_key(key)
        except ValueError:
            pass
        else:
            raise AssertionError(f"unsafe storage key accepted: {key}")
