"""记忆写入的数据库操作（纯直连，不需要 embedder/Qdrant）。

``propose_candidate`` 取代了原先 chat_service 里的 ``propose_user_memory``（该函数已随
写入路径收拢删除）：多了返回值与 ``source``/``author`` —— 候选与"用户明确要求记住"的行
在治理上是两种东西，写入时必须区分，否则前端无法解释"这条是谁写的"。
"""
from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import UserMemory


async def propose_candidate(
    db: AsyncSession,
    *,
    user_id: uuid.UUID,
    content: str,
    memory_type: str = "fact",
    confidence: float = 0.5,
    importance: float = 0.5,
    source: str = "inferred",
    author: str = "extractor",
    source_message_id: uuid.UUID | None = None,
    source_conversation_id: uuid.UUID | None = None,
) -> UserMemory | None:
    """插入一条候选记忆；已存在同内容的行则返回 None（不重复、不改写）。

    ``active`` 由 ``source`` 决定：``explicit``（用户说了"记住"）立即生效，其余进候选
    待用户确认（spec §5.5）。S1 只有 inferred —— 显式识别在 S2。
    """
    content = (content or "").strip()
    if not content:
        return None
    existing = (
        await db.execute(
            select(UserMemory).where(
                UserMemory.user_id == user_id, UserMemory.content == content
            )
        )
    ).scalar_one_or_none()
    if existing is not None:
        return None
    row = UserMemory(
        user_id=user_id,
        memory_type=memory_type,
        content=content,
        confidence=confidence,
        importance=importance,
        source=source,
        author=author,
        source_message_id=source_message_id,
        source_conversation_id=source_conversation_id,
        active=(source == "explicit"),
    )
    db.add(row)
    await db.flush()
    return row
