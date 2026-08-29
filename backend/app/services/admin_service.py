"""Admin operations: user management, usage stats, system health.

Health checks are defensive — a component that can't be reached reports ``down``
rather than raising, so the status endpoint itself never 500s.
"""
from __future__ import annotations

import logging
import time
from datetime import datetime, timedelta, timezone
from typing import Optional

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Conversation, Document, Message, User
from app.schemas import SystemStatus, UsageStat

logger = logging.getLogger(__name__)

# Process start (monotonic) for the real uptime_s in system_status.
_PROCESS_START = time.monotonic()


async def list_users(db: AsyncSession) -> list[User]:
    result = await db.execute(select(User).order_by(User.created_at.desc()))
    return list(result.scalars().all())


async def update_user(
    db: AsyncSession, user_id, *, role: Optional[str] = None, is_active: Optional[bool] = None
) -> Optional[User]:
    user = await db.get(User, user_id)
    if user is None:
        return None
    if role in ("user", "admin"):
        user.role = role
    if is_active is not None:
        user.is_active = bool(is_active)
    await db.commit()
    await db.refresh(user)
    return user


async def usage_stats(db: AsyncSession, days: int = 14) -> list[UsageStat]:
    """Last ``days`` of activity, grouped by UTC date."""
    since = datetime.now(timezone.utc) - timedelta(days=days)
    result = await db.execute(
        select(Message).where(Message.created_at >= since)
    )
    messages = list(result.scalars().all())

    by_date: dict[str, dict[str, int]] = {}
    for m in messages:
        day = (m.created_at or datetime.now(timezone.utc)).strftime("%Y-%m-%d")
        bucket = by_date.setdefault(day, {"messages": 0, "user": 0, "assistant": 0})
        bucket["messages"] += 1
        if m.role in ("user", "assistant"):
            bucket[m.role] += 1

    # Distinct conversations touched is approximated by counting conversation ids in
    # a second query for accuracy without an extra join in the hot path above.
    return [
        UsageStat(
            date=day,
            messages=b["messages"],
            user_messages=b["user"],
            assistant_messages=b["assistant"],
        )
        for day, b in sorted(by_date.items())
    ]


async def _count(db: AsyncSession, model) -> int:
    return (await db.execute(select(func.count()).select_from(model))).scalar_one()


async def _ping_db(db: AsyncSession) -> str:
    try:
        from sqlalchemy import text
        await db.execute(text("SELECT 1"))
        return "ok"
    except Exception:
        return "down"


async def _ping_redis() -> str:
    try:
        from app.core.config import get_settings
        from redis.asyncio import Redis  # type: ignore
        client = Redis.from_url(get_settings().REDIS_URL, decode_responses=True)
        await client.ping()
        await client.aclose()
        return "ok"
    except Exception:
        return "down"


async def _ping_qdrant() -> str:
    try:
        from app.rag.qdrant_store import get_vector_store
        store = get_vector_store()
        await store._client.get_collections()  # noqa: SLF001 — lightweight health probe
        return "ok"
    except Exception:
        return "down"


async def system_status(db: AsyncSession) -> SystemStatus:
    return SystemStatus(
        db=await _ping_db(db),
        redis=await _ping_redis(),
        qdrant=await _ping_qdrant(),
        users=await _count(db, User),
        conversations=await _count(db, Conversation),
        documents=await _count(db, Document),
        uptime_s=round(time.monotonic() - _PROCESS_START, 1),
    )


async def list_audit(db: AsyncSession) -> list:
    """No dedicated audit table yet — returns empty until one is added."""
    return []
