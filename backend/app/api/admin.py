"""Admin router: user management, usage stats, system health.

Every route requires an admin user (``get_current_admin``). User mutations guard the
last admin so the system can't be locked out.
"""
from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import get_current_admin, get_current_user
from app.db import get_db
from app.models import User
from app.schemas import AdminUserUpdate, SystemStatus, UsageStat, UserOut
from app.services import admin_service

router = APIRouter(prefix="/api/admin", tags=["admin"])

NOT_FOUND = status.HTTP_404_NOT_FOUND
BAD = status.HTTP_400_BAD_REQUEST


@router.get("/users", response_model=list[UserOut])
async def list_users(
    admin: User = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db),
) -> list[UserOut]:
    users = await admin_service.list_users(db)
    return [UserOut.model_validate(u) for u in users]


@router.patch("/users/{user_id}", response_model=UserOut)
async def update_user(
    user_id: uuid.UUID,
    payload: AdminUserUpdate,
    admin: User = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db),
) -> UserOut:
    # Refuse to deactivate/demote the last remaining admin.
    if payload.role == "user" or payload.is_active is False:
        count = (
            await db.execute(
                select(User).where(User.role == "admin", User.is_active.is_(True))
            )
        ).scalars().all()
        if len(count) <= 1:
            target = await db.get(User, user_id)
            if target is not None and target.id == (count[0].id if count else None):
                raise HTTPException(BAD, "不能降级或停用最后一个管理员")

    user = await admin_service.update_user(
        db, user_id, role=payload.role, is_active=payload.is_active
    )
    if user is None:
        raise HTTPException(NOT_FOUND, "User not found")
    return UserOut.model_validate(user)


@router.get("/stats")
async def stats(
    admin: User = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db),
):
    """Combined usage (last 14 days) + live component status for the dashboard."""
    usage = await admin_service.usage_stats(db)
    status_info = await admin_service.system_status(db)
    return {"usage": [u.model_dump(mode="json") for u in usage], "status": status_info.model_dump(mode="json")}


@router.get("/status", response_model=SystemStatus)
async def get_status(
    admin: User = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db),
) -> SystemStatus:
    return await admin_service.system_status(db)


@router.get("/audit")
async def audit_log(
    admin: User = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db),
):
    """Audit log placeholder — returns [] until a dedicated audit table is added."""
    return []
