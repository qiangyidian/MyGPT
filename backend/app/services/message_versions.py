"""消息版本的读写原语（条目 31）。

这里只放「存档 / 取回 / 切回」三件事，路由层负责鉴权。三条不变量：

1. 快照发生在覆盖/删除**之前**，所以调用点必须先进这里再改 ``Message``。
2. 切回版本是「当前版存档 + 目标版覆盖当前版」，不是新建一条消息：消息 id 是
   前端流式渲染、引用、反馈投票的锚点，换 id 会把这一切都甩掉。
3. 裁剪按 ``created_at`` 从旧到新删，且永不裁到当前版——版本历史可以长，但不能
   长成无界存储。
"""
from __future__ import annotations

import uuid
from collections.abc import Sequence
from typing import Any

from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Message, MessageVersion
from app.models.message_version import (
    MAX_VERSIONS_PER_CONVERSATION,
    MAX_VERSIONS_PER_MESSAGE,
)

__all__ = [
    "MAX_VERSIONS_PER_CONVERSATION",
    "MAX_VERSIONS_PER_MESSAGE",
    "activate_version",
    "get_version",
    "latest_versions",
    "list_versions",
    "prune_versions",
    "snapshot_message",
]


async def snapshot_message(
    db: AsyncSession,
    message: Message,
    *,
    origin: str,
) -> MessageVersion:
    """把 ``message`` 当前的内容+元数据原样存成的一版。

    不 commit：调用点通常还要接着改同一行或删它，必须和这次快照同事务落库，
    否则崩溃后会留下「新版没存、旧版丢了」的空档。
    """
    version = MessageVersion(
        message_id=message.id,
        conversation_id=message.conversation_id,
        role=message.role,
        content=message.content or "",
        metadata_=dict(message.metadata_ or {}),
        model_name=message.model_name,
        total_tokens=message.total_tokens,
        cost_usd=message.cost_usd,
        origin=origin,
    )
    db.add(version)
    await db.flush()
    await prune_versions(db, version.conversation_id)
    return version


async def latest_versions(
    db: AsyncSession, conversation_id: uuid.UUID, *, limit: int = 200
) -> Sequence[MessageVersion]:
    """整段会话的历史版本，新的在前。

    regenerate 会换掉消息行的 id，所以「这一轮的第 2 版」只能按会话取出来、再由
    界面按时间挂回对应的那一轮——按 message_id 查会漏掉所有已被删掉的老消息。
    """
    rows = (
        await db.execute(
            select(MessageVersion)
            .where(MessageVersion.conversation_id == conversation_id)
            .order_by(MessageVersion.created_at.desc(), MessageVersion.id.desc())
            .limit(limit)
        )
    ).scalars().all()
    return rows


async def list_versions(
    db: AsyncSession, message_id: uuid.UUID, *, limit: int = 50
) -> Sequence[MessageVersion]:
    """某条消息的历史版本，新的在前。"""
    rows = (
        await db.execute(
            select(MessageVersion)
            .where(MessageVersion.message_id == message_id)
            .order_by(MessageVersion.created_at.desc(), MessageVersion.id.desc())
            .limit(limit)
        )
    ).scalars().all()
    return rows


async def get_version(
    db: AsyncSession, *, message_id: uuid.UUID, version_id: uuid.UUID
) -> MessageVersion | None:
    return (
        await db.execute(
            select(MessageVersion).where(
                MessageVersion.id == version_id,
                MessageVersion.message_id == message_id,
            )
        )
    ).scalars().first()


async def prune_versions(db: AsyncSession, conversation_id: uuid.UUID) -> int:
    """两级裁剪：单条消息留最近 ``MAX_VERSIONS_PER_MESSAGE`` 版，整段会话再封一个总量顶。

    只按 ``message_id`` 裁是不够的——regenerate 每次都会留下一条「所属消息行已经不
    在了」的孤儿版本，消息维度的上限永远管不到它，长对话能把它堆到无界。
    """
    scoped = MessageVersion.conversation_id == conversation_id
    ranked = (
        await db.execute(
            select(
                MessageVersion.id,
                func.row_number()
                .over(
                    partition_by=MessageVersion.message_id,
                    order_by=(MessageVersion.created_at.desc(), MessageVersion.id.desc()),
                )
                .label("rn"),
            ).where(scoped)
        )
    ).all()
    doomed = {row.id for row in ranked if row.rn > MAX_VERSIONS_PER_MESSAGE}

    overflow = (
        await db.execute(
            select(MessageVersion.id)
            .where(scoped)
            .order_by(MessageVersion.created_at.desc(), MessageVersion.id.desc())
            .offset(MAX_VERSIONS_PER_CONVERSATION)
        )
    ).scalars().all()
    doomed.update(overflow)

    if not doomed:
        return 0
    await db.execute(delete(MessageVersion).where(MessageVersion.id.in_(list(doomed))))
    return len(doomed)


async def activate_version(
    db: AsyncSession, message: Message, version: MessageVersion
) -> dict[str, Any]:
    """把 ``version`` 变回当前内容；当前版先存档成 ``restore`` 版本。

    返回一个给 UI 用的小结（是否真的换了、当前是第几版）。同一条消息上反复切回
    同一版是幂等的，所以内容一致时什么都不写——不然点八次就攒八条一样的历史。
    """
    current_content = message.content or ""
    current_meta = dict(message.metadata_ or {})
    same = (
        current_content == (version.content or "")
        and current_meta == dict(version.metadata_ or {})
        and (message.model_name or "") == (version.model_name or "")
    )
    if same:
        return {"changed": False, "restored_from": str(version.id)}

    await snapshot_message(db, message, origin="restore")
    message.content = version.content or ""
    message.metadata_ = dict(version.metadata_ or {})
    message.model_name = version.model_name
    message.total_tokens = version.total_tokens
    message.cost_usd = version.cost_usd
    await db.flush()
    await prune_versions(db, message.id)
    return {"changed": True, "restored_from": str(version.id)}
