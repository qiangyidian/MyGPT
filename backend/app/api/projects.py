"""Projects router (Phase 3)."""
from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import get_current_user
from app.db import get_db
from app.models import User
from app.schemas import ConversationOut, ProjectCreate, ProjectOut, ProjectUpdate
from app.services import project_service

router = APIRouter(prefix="/api/projects", tags=["projects"])


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
    return ProjectOut.model_validate(await project_service.create(db, user, payload))


@router.patch("/{project_id}", response_model=ProjectOut)
async def update_project(
    project_id: uuid.UUID,
    payload: ProjectUpdate,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> ProjectOut:
    return ProjectOut.model_validate(
        await project_service.update(db, project_id, user.id, payload)
    )


@router.delete("/{project_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_project(
    project_id: uuid.UUID,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> None:
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
