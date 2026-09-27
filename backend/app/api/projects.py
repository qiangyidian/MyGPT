"""Projects router (Phase 3).

A project is a *soft* grouping of conversations: ``Conversation.project_id`` carries
no FK (see app/models/project.py), so **nothing cascades** when a project goes.
Deleting the row alone left every assigned conversation pointing at a vanished id
— and the sidebar groups by *known* project, so those conversations disappeared
from the list entirely (neither filed nor unfiled). The delete below therefore
nulls the references explicitly, mirroring how ``knowledge_bases`` deletion was
made to actually reclaim what it claims to.

Rename / delete / impact are owner-guarded with 404-on-foreign-id (this repo never
leaks existence) via :func:`app.services.project_service.get_owned`.
"""
from __future__ import annotations

import re
import uuid

from fastapi import APIRouter, Depends, status
from pydantic import BaseModel
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import get_current_user
from app.core.exceptions import AppException
from app.db import get_db
from app.models import Conversation, Message, User
from app.schemas import ConversationOut, ProjectCreate, ProjectOut, ProjectUpdate
from app.services import project_service

router = APIRouter(prefix="/api/projects", tags=["projects"])

DEFAULT_COLOR = "#6366f1"
# Mirrors ``Project.name``'s String(255). Postgres answers a longer value with a
# driver error (a 500), and SQLite silently stores it — so the column limit is
# enforced here as a 400 instead of being discovered by whichever DB is running.
NAME_MAX = 255
_COLOR_RE = re.compile(r"^#(?:[0-9a-fA-F]{3}|[0-9a-fA-F]{6})$")

# Fields a PATCH may write. Without this list a ``null`` for a NOT NULL column
# (``name``/``color``) would reach ``setattr`` and blow up on commit.
_PATCHABLE = ("name", "description", "color")


def _clean_name(value: str | None) -> str:
    name = (value or "").strip()
    if not name:
        raise AppException(
            status.HTTP_400_BAD_REQUEST, "project_name_required", "项目名称不能为空"
        )
    if len(name) > NAME_MAX:
        raise AppException(
            status.HTTP_400_BAD_REQUEST,
            "project_name_too_long",
            f"项目名称不能超过 {NAME_MAX} 个字符",
        )
    return name


def _clean_color(value: str | None) -> str:
    color = (value or "").strip()
    if not _COLOR_RE.match(color):
        raise AppException(
            status.HTTP_400_BAD_REQUEST,
            "project_color_invalid",
            "项目颜色需为 #RRGGBB 或 #RGB 格式",
        )
    return color


class ProjectImpact(BaseModel):
    """The consequences of deleting a project, computed server-side.

    Lives here (not in app/schemas) because it is a view model for exactly one
    router — the same reason the KB router keeps its own helpers local.
    """

    project_id: uuid.UUID
    name: str
    conversation_count: int
    archived_conversation_count: int
    message_count: int
    knowledge_base_count: int
    deletes_conversations: bool


@router.get("", response_model=list[ProjectOut])
async def list_projects(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> list[ProjectOut]:
    return [ProjectOut.model_validate(p) for p in await project_service.list_for_user(db, user.id)]


@router.post("", response_model=ProjectOut, status_code=status.HTTP_201_CREATED)
async def create_project(
    payload: ProjectCreate,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> ProjectOut:
    # Validated here rather than relying on the service's strip-or-default: a
    # 300-character name used to reach the column and come back as a 500.
    cleaned = ProjectCreate(
        name=_clean_name(payload.name),
        description=payload.description,
        color=payload.color,
    )
    return ProjectOut.model_validate(await project_service.create(db, user, cleaned))


@router.get("/{project_id}/impact", response_model=ProjectImpact)
async def get_project_impact(
    project_id: uuid.UUID,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> ProjectImpact:
    """What deleting this project actually touches — counted from live rows.

    The UI refuses to guess this: a confirmation dialog that says "此操作不可撤销"
    without naming the conversations on the other side is worse than no dialog.
    ``knowledge_base_count`` is structural, not a query: there is no
    project↔knowledge-base link in the model at all, so the honest answer is
    always 0 and the dialog can say so instead of staying silent about it.
    """
    project = await project_service.get_owned(db, project_id, user.id)
    conv_ids = select(Conversation.id).where(
        Conversation.project_id == project.id,
        Conversation.user_id == user.id,
    )
    conversation_count = (
        await db.execute(select(func.count()).select_from(conv_ids.subquery()))
    ).scalar_one()
    archived_count = (
        await db.execute(
            select(func.count())
            .select_from(Conversation)
            .where(
                Conversation.project_id == project.id,
                Conversation.user_id == user.id,
                Conversation.is_archived.is_(True),
            )
        )
    ).scalar_one()
    message_count = (
        await db.execute(
            select(func.count(Message.id)).where(Message.conversation_id.in_(conv_ids))
        )
    ).scalar_one()
    return ProjectImpact(
        project_id=project.id,
        name=project.name,
        conversation_count=conversation_count,
        archived_conversation_count=archived_count,
        message_count=message_count,
        # No FK, no cascade: the conversations survive the project.
        deletes_conversations=False,
        knowledge_base_count=0,
    )


@router.patch("/{project_id}", response_model=ProjectOut)
async def update_project(
    project_id: uuid.UUID,
    payload: ProjectUpdate,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> ProjectOut:
    """Rename / re-describe / re-color a project.

    ``model_fields_set`` keeps this a real PATCH: an omitted key stays as-is.
    An explicit ``null`` on a NOT NULL column is *not* passed through — ``name``
    is rejected, ``color`` resets to the platform default, which is what a
    "restore default" affordance in the UI needs.
    """
    provided = payload.model_fields_set & set(_PATCHABLE)
    if not provided:
        raise AppException(
            status.HTTP_400_BAD_REQUEST, "empty_patch", "没有需要更新的字段"
        )
    updates: dict[str, object] = {}
    if "name" in provided:
        updates["name"] = _clean_name(payload.name)
    if "color" in provided:
        updates["color"] = (
            DEFAULT_COLOR if payload.color is None else _clean_color(payload.color)
        )
    if "description" in provided:
        updates["description"] = (payload.description or "").strip() or None
    return ProjectOut.model_validate(
        await project_service.update(db, project_id, user.id, ProjectUpdate(**updates))
    )


@router.delete("/{project_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_project(
    project_id: uuid.UUID,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> None:
    await project_service.get_owned(db, project_id, user.id)  # 404 on foreign id
    # Un-file the conversations BEFORE the project row goes: ``project_id`` is a
    # soft reference, so no ON DELETE SET NULL exists to do it for us and a
    # dangling id makes the conversation unreachable in the sidebar. Same
    # transaction as the delete below, so it is atomic either way.
    await db.execute(
        update(Conversation)
        .where(Conversation.project_id == project_id, Conversation.user_id == user.id)
        .values(project_id=None)
    )
    await project_service.delete(db, project_id, user.id)


@router.post("/{project_id}/conversations/{conversation_id}", response_model=ConversationOut)
async def assign_conversation(
    project_id: uuid.UUID,
    conversation_id: uuid.UUID,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> ConversationOut:
    conv = await project_service.assign_conversation(db, project_id, conversation_id, user.id)
    return ConversationOut.model_validate(conv)


@router.delete("/{project_id}/conversations/{conversation_id}", response_model=ConversationOut)
async def unassign_conversation(
    project_id: uuid.UUID,
    conversation_id: uuid.UUID,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> ConversationOut:
    conv = await project_service.unassign_conversation(db, conversation_id, user.id)
    return ConversationOut.model_validate(conv)
