"""Live institutional audit notifications with database polling fallback."""

from __future__ import annotations

import asyncio
import json
from typing import Annotated

from fastapi import APIRouter, Depends, Request
from fastapi.responses import StreamingResponse
from sqlalchemy import select

from .config import get_settings
from .database import SessionLocal, set_tenant_context
from .models import AuditEvent
from .security import Principal, require

router = APIRouter(prefix="/api/v2", tags=["events"])


@router.get("/events")
async def event_stream(
    request: Request,
    principal: Annotated[Principal, Depends(require("review:read"))],
    after: int = 0,
) -> StreamingResponse:
    async def generate():
        sequence = max(0, after)
        redis_client = None
        pubsub = None
        if get_settings().redis_url:
            try:
                import redis.asyncio as redis_async

                redis_client = redis_async.Redis.from_url(get_settings().redis_url)
                pubsub = redis_client.pubsub()
                await pubsub.subscribe(f"predixalearn:{principal.tenant_id}:events")
            except Exception:
                redis_client = None
                pubsub = None
        try:
            while not await request.is_disconnected():
                with SessionLocal() as session:
                    set_tenant_context(session, principal.tenant_id)
                    events = list(
                        session.scalars(
                            select(AuditEvent)
                            .where(
                                AuditEvent.tenant_id == principal.tenant_id,
                                AuditEvent.sequence > sequence,
                            )
                            .order_by(AuditEvent.sequence)
                            .limit(100)
                        )
                    )
                if events:
                    for event in events:
                        sequence = event.sequence
                        payload = {
                            "sequence": event.sequence,
                            "event_id": event.event_id,
                            "action": event.action,
                            "resource_type": event.resource_type,
                            "resource_id": event.resource_id,
                            "created_at": event.created_at.isoformat(),
                        }
                        yield f"id: {sequence}\nevent: audit\ndata: {json.dumps(payload)}\n\n"
                elif pubsub:
                    await pubsub.get_message(ignore_subscribe_messages=True, timeout=2)
                else:
                    await asyncio.sleep(2)
                if not events:
                    yield ": keep-alive\n\n"
        finally:
            if pubsub:
                await pubsub.aclose()
            if redis_client:
                await redis_client.aclose()

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"},
    )
