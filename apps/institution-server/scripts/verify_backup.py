"""Read-only verification for a restored institution database and object manifest."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path

from sqlalchemy import create_engine, select, text
from sqlalchemy.orm import Session

from institution_server.models import AuditEvent
from institution_server.storage import create_storage


def verify_audit(session: Session, tenant_id: str) -> int:
    if session.bind and session.bind.dialect.name == "postgresql":
        session.execute(
            text("SELECT set_config('app.tenant_id', :tenant_id, true)"),
            {"tenant_id": tenant_id},
        )
    events = list(
        session.scalars(
            select(AuditEvent)
            .where(AuditEvent.tenant_id == tenant_id)
            .order_by(AuditEvent.sequence)
        )
    )
    prior = None
    for event in events:
        if event.previous_hash != prior:
            raise RuntimeError(f"Audit chain breaks at event {event.event_id}")
        prior = event.event_hash
    return len(events)


def verify_objects(manifest_path: Path) -> int:
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    items = manifest.get("objects", [])
    storage = create_storage()
    for item in items:
        data = storage.read(str(item["key"]))
        if hashlib.sha256(data).hexdigest() != item["sha256"]:
            raise RuntimeError(f"Object checksum mismatch: {item['key']}")
    return len(items)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tenant-id", required=True)
    parser.add_argument("--manifest", type=Path)
    arguments = parser.parse_args()
    database_url = os.environ["PREDIXALEARN_DATABASE_URL"].replace(
        "postgresql://", "postgresql+psycopg://", 1
    )
    engine = create_engine(database_url, pool_pre_ping=True)
    with Session(engine) as session:
        session.execute(text("SELECT 1"))
        audit_count = verify_audit(session, arguments.tenant_id)
    object_count = verify_objects(arguments.manifest) if arguments.manifest else 0
    print(json.dumps({"database": "verified", "audit_events": audit_count, "objects": object_count}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
