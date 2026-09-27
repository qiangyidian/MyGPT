"""消息版本历史接口（条目 31）。

两个动作：把一段会话的历史版本列出来，以及把某一条消息切回其中一版。

为什么要单独有这两个接口：编辑提问会原地覆盖正文，重新生成会直接删掉上一条回答，
两者都把用户已经付过 token 的内容弄丢了。快照在覆盖前就写好（见
``app/services/message_versions.py``），所以这里只读不改历史。

鉴权与全项目一致：越权一律 404，不用 403——存在性本身不外泄。
"""
from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import get_current_user
from app.db import get_db
from app.models import Conversation, Message, User
from app.schemas.message_version import MessageVersionOut, VersionActivateResult
from app.services.message_versions import (
    activate_version,
    get_version,
    latest_versions,
)

router = APIRouter(prefix="/api/conversations", tags=["message-versions"])

NOT_FOUND = status.HTTP_404_NOT_FOUND


async def _load_owned_conversation(
    db: AsyncSession, conv_id: uuid.UUID, user: User
) -> Conversation:
    conv = (
        await db.execute(select(Conversation).where(Conversation.id == conv_id))
    ).scalars().first()
    if conv is None or (conv.user_id != user.id and user.role != "admin"):
        raise HTTPException(NOT_FOUND, "会话不存在")
    return conv


@router.get("/{conv_id}/versions", response_model=list[MessageVersionOut])
async def list_conversation_versions(
    conv_id: uuid.UUID,
    message_id: uuid.UUID | None = None,
    limit: int = Query(100, ge=1, le=200),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> list[MessageVersionOut]:
    """一段会话的历史版本，新的在前。

    按会话而不是按消息取，是因为重新生成会换掉消息行的 id：只按 ``message_id``
    查，恰好会漏掉所有「回答被换掉」的那批版本——也就是这个功能最主要的用途。
    """
    conv = await _load_owned_conversation(db, conv_id, user)
    rows = await latest_versions(db, conv.id, limit=limit)
    if message_id is not None:
        rows = [r for r in rows if r.message_id == message_id]
    return [MessageVersionOut.model_validate(r) for r in rows]


@router.post(
    "/{conv_id}/messages/{message_id}/versions/{version_id}/activate",
    response_model=VersionActivateResult,
)
async def activate_message_version(
    conv_id: uuid.UUID,
    message_id: uuid.UUID,
    version_id: uuid.UUID,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> VersionActivateResult:
    """把 ``message_id`` 的内容换成某一历史版本；换掉的当前版先存成一版。

    不新建消息行：消息 id 是流式渲染、引用面板和点赞点踩的锚点，换 id 等于把这一
    轮的其他状态全部甩掉。
    """
    conv = await _load_owned_conversation(db, conv_id, user)
    msg = await db.get(Message, message_id)
    if (
        msg is None
        or msg.conversation_id != conv.id
        or (conv.user_id != user.id and user.role != "admin")
    ):
        raise HTTPException(NOT_FOUND, "消息不存在")
    version = await get_version(db, message_id=message_id, version_id=version_id)
    if version is None or version.conversation_id != conv.id:
        raise HTTPException(NOT_FOUND, "该版本不存在")

    outcome = await activate_version(db, msg, version)
    await db.commit()
    return VersionActivateResult(
        message_id=msg.id,
        activated_version_id=version.id,
        changed=bool(outcome["changed"]),
    )
