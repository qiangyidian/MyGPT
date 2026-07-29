"""Background tasks router (Phase 3): list / enqueue / cancel."""
from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import get_current_user
from app.db import get_db
from app.models import User
from app.schemas import BackgroundTaskEnqueue, BackgroundTaskOut
from app.services import background_task_service

router = APIRouter(prefix="/api/background-tasks", tags=["background-tasks"])


@router.get("", response_model=list[BackgroundTaskOut])
async def list_tasks(
    kind: str | None = Query(None),
    limit: int = Query(50),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> list[BackgroundTaskOut]:
    rows = await background_task_service.list_for_user(db, user.id, kind=kind, limit=limit)
    return [BackgroundTaskOut.model_validate(t) for t in rows]


@router.post("", response_model=BackgroundTaskOut, status_code=status.HTTP_201_CREATED)
async def enqueue_task(
    payload: BackgroundTaskEnqueue,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> BackgroundTaskOut:
    t = await background_task_service.enqueue(
        db,
        user_id=user.id,
        kind=payload.kind,
        payload=payload.payload,
        conversation_id=payload.conversation_id,
        scheduled_at=payload.scheduled_at,
    )
    return BackgroundTaskOut.model_validate(t)


@router.delete("/{task_id}", response_model=BackgroundTaskOut)
async def cancel_task(
    task_id: uuid.UUID,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> BackgroundTaskOut:
    t = await background_task_service.cancel(db, task_id, user.id)
    return BackgroundTaskOut.model_validate(t)
