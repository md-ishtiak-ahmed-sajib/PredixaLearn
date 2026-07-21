"""Deterministically export the cross-application JSON Schema bundle."""

from __future__ import annotations

import json
from pathlib import Path

from predixalearn_protocol import SyncEnvelope, WebhookEvent, WorkerJobEnvelope


def main() -> None:
    destination = Path(__file__).resolve().parents[1] / "schemas" / "protocol.schema.json"
    destination.parent.mkdir(parents=True, exist_ok=True)
    schemas = {
        "protocol_version": "2026-07-01",
        "schemas": {
            model.__name__: model.model_json_schema()
            for model in (SyncEnvelope, WorkerJobEnvelope, WebhookEvent)
        },
    }
    destination.write_text(
        json.dumps(schemas, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )


if __name__ == "__main__":
    main()
