"""chat_service 里可复用的纯 helper 的落位。

``app/agents/memory/*`` 需要 ``load_history`` / ``safe_int``，但不能 import
``app.services.chat_service``（会成环：``chat_service`` 要 import ``facade``）。把这两个
函数搬到这里，``chat_service`` 再从本模块 import 回去 —— 函数体一字未改，只是换了个
模块住。
"""
from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Message


def safe_int(value: Any, default: int) -> int:
    try:
        v = int(value)
        return v if v > 0 else default
    except (TypeError, ValueError):
        return default


async def load_history(
    db: AsyncSession, conversation_id: uuid.UUID, *, limit: int = 500
) -> list[Message]:
    """Return the most recent ``limit`` conversation messages, oldest-first.

    Capped so a very long conversation doesn't load every message row (with full
    content/metadata) into memory on every turn — trimming + summarization then
    operate within this recent window. ``limit`` defaults high enough that normal
    conversations are unaffected; it's a backstop against pathological histories.
    """
    result = await db.execute(
        select(Message)
        .where(Message.conversation_id == conversation_id)
        .order_by(Message.created_at.desc(), Message.id.desc())
        .limit(limit)
    )
    rows = list(result.scalars().all())
    rows.reverse()  # restore oldest-first for callers
    return rows
