"""Signed webhook delivery with replay protection metadata and retry."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import time
from datetime import datetime, timedelta, timezone

import httpx
from predixalearn_protocol import WebhookEvent
from sqlalchemy import select

from .config import Settings, get_settings
from .database import SessionLocal, set_tenant_context
from .models import AuditEvent, Resource, WebhookDelivery

logger = logging.getLogger(__name__)


def signature(secret: str, timestamp: str, body: bytes) -> str:
    return hmac.new(secret.encode(), timestamp.encode() + b"." + body, hashlib.sha256).hexdigest()


async def deliver_once(settings: Settings | None = None) -> dict[str, int]:
    selected = settings or get_settings()
    if not selected.webhook_signing_secret:
        return {"delivered": 0, "pending": 0, "failed": 0}
    now = datetime.now(timezone.utc)
    with SessionLocal() as session:
        deliveries = list(
            session.scalars(
                select(WebhookDelivery)
                .where(
                    WebhookDelivery.status == "pending",
                    WebhookDelivery.next_attempt_at <= now,
                )
                .order_by(WebhookDelivery.created_at)
                .limit(50)
            )
        )
        counts = {"delivered": 0, "pending": 0, "failed": 0}
        async with httpx.AsyncClient(timeout=15) as client:
            for delivery in deliveries:
                set_tenant_context(session, delivery.tenant_id)
                subscription = session.scalar(
                    select(Resource).where(
                        Resource.tenant_id == delivery.tenant_id,
                        Resource.resource_type == "webhook_subscription",
                        Resource.resource_id == delivery.subscription_id,
                    )
                )
                event = session.scalar(
                    select(AuditEvent).where(
                        AuditEvent.tenant_id == delivery.tenant_id,
                        AuditEvent.event_id == delivery.event_id,
                    )
                )
                if subscription is None or event is None:
                    delivery.status = "failed"
                    delivery.last_error = "Subscription or event no longer exists"
                    counts["failed"] += 1
                    continue
                payload = WebhookEvent(
                    tenant_id=delivery.tenant_id,
                    event_id=event.event_id,
                    event_type=event.action,
                    occurred_at=event.created_at,
                    resource_id=event.resource_id,
                    payload={
                        "resource_type": event.resource_type,
                        "request_id": event.request_id,
                    },
                ).model_dump(mode="json")
                body = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
                timestamp = str(int(time.time()))
                delivery.attempt += 1
                try:
                    response = await client.post(
                        str(subscription.payload["target_url"]),
                        content=body,
                        headers={
                            "Content-Type": "application/json",
                            "X-PredixaLearn-Delivery": delivery.delivery_id,
                            "X-PredixaLearn-Timestamp": timestamp,
                            "X-PredixaLearn-Signature": f"v1={signature(str(selected.webhook_signing_secret), timestamp, body)}",
                        },
                    )
                    response.raise_for_status()
                    delivery.status = "delivered"
                    delivery.last_error = None
                    counts["delivered"] += 1
                except httpx.HTTPError:
                    if delivery.attempt >= 10:
                        delivery.status = "failed"
                        delivery.last_error = "Webhook delivery exhausted its retry policy"
                        counts["failed"] += 1
                    else:
                        delay = min(3600, 15 * (2 ** min(delivery.attempt, 8)))
                        delivery.next_attempt_at = now + timedelta(seconds=delay)
                        delivery.last_error = "Webhook target is temporarily unavailable"
                        counts["pending"] += 1
        session.commit()
    return counts


async def delivery_loop(stop: asyncio.Event) -> None:
    settings = get_settings()
    while not stop.is_set():
        try:
            await deliver_once(settings)
        except Exception:
            # Delivery payloads never contain source document content.
            logger.exception("Webhook delivery loop failed")
        try:
            await asyncio.wait_for(stop.wait(), timeout=10)
        except TimeoutError:
            continue
