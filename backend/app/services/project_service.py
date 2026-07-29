"""Projects service (Phase 3): CRUD + conversation assignment. User-scoped."""
from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import AppException
from app.models import Conversation, Project, User
from app.schemas import ProjectCreate, ProjectUpdate


async def list_for_user(db: AsyncSession, user_id: uuid.UUID) -> list[Project]:
    res = await db.execute(
        select(Project).where(Project.user_id == user_id).order_by(Project.updated_at.desc())
    )
    return list(res.scalars().all())


async def get_owned(db: AsyncSession, project_id: uuid.UUID, user_id: uuid.UUID) -> Project:
    p = await db.get(Project, project_id)
    if p is None or p.user_id != user_id:
        raise AppException(404, "project_not_found", "项目不存在")
    return p


async def create(db: AsyncSession, user: User, data: ProjectCreate) -> Project:
    p = Project(
        user_id=user.id,
        name=(data.name or "").strip() or "未命名项目",
        description=data.description,
        color=data.color or "#6366f1",
    )
    db.add(p)
    await db.commit()
    await db.refresh(p)
    return p


async def update(
    db: AsyncSession, project_id: uuid.UUID, user_id: uuid.UUID, data: ProjectUpdate
) -> Project:
    p = await get_owned(db, project_id, user_id)
    for field, value in data.model_dump(exclude_unset=True).items():
        setattr(p, field, value)
    await db.commit()
    await db.refresh(p)
    return p


async def delete(db: AsyncSession, project_id: uuid.UUID, user_id: uuid.UUID) -> None:
    p = await get_owned(db, project_id, user_id)
    await db.delete(p)
    await db.commit()


async def assign_conversation(
    db: AsyncSession, project_id: uuid.UUID, conversation_id: uuid.UUID, user_id: uuid.UUID
) -> Conversation:
    """Attach a conversation to a project (soft ref via Conversation.project_id)."""
    await get_owned(db, project_id, user_id)
    conv = await db.get(Conversation, conversation_id)
    if conv is None or conv.user_id != user_id:
        raise AppException(404, "conversation_not_found", "对话不存在")
    conv.project_id = project_id
    await db.commit()
    await db.refresh(conv)
    return conv


async def unassign_conversation(
    db: AsyncSession, conversation_id: uuid.UUID, user_id: uuid.UUID
) -> Conversation:
    conv = await db.get(Conversation, conversation_id)
    if conv is None or conv.user_id != user_id:
        raise AppException(404, "conversation_not_found", "对话不存在")
    conv.project_id = None
    await db.commit()
    await db.refresh(conv)
    return conv
