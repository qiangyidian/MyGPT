"""Admin operations: user management, usage stats, system health.

Health checks are defensive — a component that can't be reached reports ``down``
rather than raising, so the status endpoint itself never 500s.
"""
from __future__ import annotations

import logging
import time
from datetime import datetime, timedelta, timezone

from sqlalchemy import case, func, select
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
    db: AsyncSession, user_id, *, role: str | None = None, is_active: bool | None = None
) -> User | None:
    user = await db.get(User, user_id)
    if user is None:
        return None
    if role in ("user", "admin"):
        user.role = role
    if is_active is not None:
        if user.is_active and not bool(is_active):
            # Deactivation also invalidates every access token already in the
            # user's hands (belt to the is_active check in get_current_user).
            user.token_version = int(user.token_version or 0) + 1
        user.is_active = bool(is_active)
    await db.commit()
    await db.refresh(user)
    return user


async def usage_stats(db: AsyncSession, days: int = 14) -> list[UsageStat]:
    """Last ``days`` of activity, grouped by UTC date.

    Aggregated in SQL (GROUP BY date_trunc / day) instead of pulling every
    Message row — including full content TEXT — into Python. The old
    load-everything version turned the admin dashboard into a table scan of
    hundreds of thousands of rows as data grew.
    """
    since = datetime.now(timezone.utc) - timedelta(days=days)
    day_expr = func.strftime("%Y-%m-%d", Message.created_at) if _is_sqlite(db) else func.to_char(
        func.date_trunc("day", Message.created_at), "YYYY-MM-DD"
    )
    result = await db.execute(
        select(
            day_expr.label("day"),
            func.count().label("messages"),
            func.sum(case((Message.role == "user", 1), else_=0)).label("user"),
            func.sum(case((Message.role == "assistant", 1), else_=0)).label("assistant"),
        )
        .where(Message.created_at >= since, Message.role.in_(["user", "assistant"]))
        .group_by(day_expr)
        .order_by(day_expr)
    )
    stats: list[UsageStat] = []
    for row in result.all():
        day = str(row.day)
        stats.append(
            UsageStat(
                date=day,
                messages=int(row.messages or 0),
                user_messages=int(row.user or 0),
                assistant_messages=int(row.assistant or 0),
            )
        )
    return stats


def _is_sqlite(db: AsyncSession) -> bool:
    return db.bind is not None and db.bind.dialect.name == "sqlite"


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
        from redis.asyncio import Redis  # type: ignore

        from app.core.config import get_settings
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
        await store._client.get_collections()
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

