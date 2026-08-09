"""Per-user token / cost usage analytics.

Aggregates the per-message token accounting persisted on assistant messages
(``Message.prompt_tokens`` etc.) into a day-by-day usage summary — the answer to
"how much did I (or a given user) spend, and on how many turns". Backs the
operator cost dashboard; previously there was no token/cost accounting at all.
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Query
from sqlalchemy import desc, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import get_current_user
from app.db import get_db
from app.models import Conversation, Message, User

router = APIRouter(prefix="/api/usage", tags=["usage"])


@router.get("/summary")
async def usage_summary(
    days: int = Query(30, ge=1, le=365),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """Per-day token + cost rollup for the current user over the last ``days`` days."""
    day = func.date(Message.created_at).label("day")
    stmt = (
        select(
            day,
            func.coalesce(func.sum(Message.prompt_tokens), 0).label("prompt_tokens"),
            func.coalesce(func.sum(Message.completion_tokens), 0).label("completion_tokens"),
            func.coalesce(func.sum(Message.total_tokens), 0).label("total_tokens"),
            func.coalesce(func.sum(Message.cost_usd), 0.0).label("cost_usd"),
            func.count(Message.id).label("turns"),
        )
        .join(Conversation, Conversation.id == Message.conversation_id)
        .where(
            Conversation.user_id == user.id,
            Message.role == "assistant",
            Message.total_tokens.is_not(None),
        )
        .group_by(day)
        .order_by(desc(day))
        .limit(days)
    )
    rows = (await db.execute(stmt)).all()
    return {
        "days": [
            {
                "date": str(r.day),
                "prompt_tokens": int(r.prompt_tokens or 0),
                "completion_tokens": int(r.completion_tokens or 0),
                "total_tokens": int(r.total_tokens or 0),
                "cost_usd": round(float(r.cost_usd or 0.0), 6),
                "turns": int(r.turns or 0),
            }
            for r in rows
        ],
        "totals": {
            "prompt_tokens": sum(int(r.prompt_tokens or 0) for r in rows),
            "completion_tokens": sum(int(r.completion_tokens or 0) for r in rows),
            "total_tokens": sum(int(r.total_tokens or 0) for r in rows),
            "cost_usd": round(sum(float(r.cost_usd or 0.0) for r in rows), 6),
            "turns": sum(int(r.turns or 0) for r in rows),
        },
    }
