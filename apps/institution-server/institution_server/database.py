"""Database engine and transaction helpers."""

from __future__ import annotations

import json
import logging
from collections.abc import Generator
from pathlib import Path

from sqlalchemy import create_engine, event, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker
from sqlalchemy.pool import StaticPool

from .config import get_settings

logger = logging.getLogger(__name__)


class Base(DeclarativeBase):
    pass


def _database_url() -> str:
    value = get_settings().database_url
    if value.startswith("postgresql://"):
        return value.replace("postgresql://", "postgresql+psycopg://", 1)
    if value.startswith("sqlite:///") and value != "sqlite:///:memory:":
        database_path = Path(value.removeprefix("sqlite:///"))
        database_path.parent.mkdir(parents=True, exist_ok=True)
    return value


_ENGINE_URL = _database_url()
engine = create_engine(
    _ENGINE_URL,
    pool_pre_ping=True,
    **(
        {"connect_args": {"check_same_thread": False}, "poolclass": StaticPool}
        if _ENGINE_URL == "sqlite:///:memory:"
        else {}
    ),
)
SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


@event.listens_for(Session, "after_commit")
def _publish_committed_events(session: Session) -> None:
    """Wake SSE consumers through Redis; PostgreSQL remains authoritative."""

    notifications = session.info.pop("predixalearn_audit_notifications", [])
    redis_url = get_settings().redis_url
    if not notifications or not redis_url:
        return
    try:
        import redis

        client = redis.Redis.from_url(redis_url, socket_timeout=1, socket_connect_timeout=1)
        for notification in notifications:
            client.publish(
                f"predixalearn:{notification['tenant_id']}:events",
                json.dumps(notification, separators=(",", ":")),
            )
        client.close()
    except Exception:
        # Redis is an acceleration layer. Database polling guarantees recovery.
        logger.warning("Redis event notification failed; SSE will use database polling")


@event.listens_for(Engine, "connect")
def _sqlite_foreign_keys(connection, _record) -> None:
    if connection.__class__.__module__.startswith("sqlite3"):
        cursor = connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()


def create_schema() -> None:
    from . import models  # noqa: F401

    Base.metadata.create_all(engine)


def session_scope() -> Generator[Session, None, None]:
    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()


def set_tenant_context(session: Session, tenant_id: str) -> None:
    """Activate PostgreSQL RLS for the current transaction."""

    if session.bind and session.bind.dialect.name == "postgresql":
        session.execute(
            text("SELECT set_config('app.tenant_id', :tenant_id, true)"),
            {"tenant_id": tenant_id},
        )
