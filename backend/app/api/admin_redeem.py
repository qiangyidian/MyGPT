"""管理侧兑换码的单码运营面（列表 / 作废 / 删除）。

``app/api/credits.py`` 的 admin_router 已经有批次级 CRUD，但批次是唯一的操作
单位 —— 运营遇到「这批里有一张贴错了群，只废那一张」时只能整批作废，或者去
写 SQL。本模块补上单码粒度：

* ``GET    /api/admin/redeem-codes``          跨批次/按批次/按状态分页（带核销人）
* ``POST   /api/admin/redeem-codes/{id}/void``  只作废这一张（active → void）
* ``DELETE /api/admin/redeem-codes/{id}``     删掉这一张（仅限未兑换）

**明文永远取不到**：``redeem_codes.code_hash`` 存的是 peppered HMAC-SHA256
（威胁模型见 ``app/models/redeem_code.py``），生成响应之后系统里不再存在明文，
所以这里的列表只给 6 位前缀 + 掩码。前缀存在的目的就是把「用户手上那张是不是
AB12CD 开头」问清楚，而不是还原凭证。

作废 / 删除都不动已兑换的码：分已经进过用户账本（``credit_ledger`` 上
``ref_type='redeem_code'`` 的部分唯一索引保证一张码只能兑一次），删行只会让
对账断链。
"""
from __future__ import annotations

import uuid
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel
from sqlalchemy import delete, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import get_current_admin
from app.db import get_db
from app.models import RedeemCode, RedeemCodeBatch, User
from app.services import audit_service

router = APIRouter(prefix="/api/admin", tags=["admin-redeem"])

NOT_FOUND = status.HTTP_404_NOT_FOUND
CONFLICT = status.HTTP_409_CONFLICT

# ``redeem_codes.status``：active | redeemed | void（见模型注释）。
CODE_STATUSES = ("active", "redeemed", "void")


class RedeemCodeRowOut(BaseModel):
    """一行码 —— 只有前缀与掩码，永不包含哈希或明文。"""

    id: uuid.UUID
    batch_id: uuid.UUID
    batch_name: str
    credits_per_code: int
    code_prefix: str
    masked_code: str
    status: str
    expires_at: datetime | None = None
    redeemed_by: uuid.UUID | None = None
    redeemer_email: str | None = None
    redeemer_username: str | None = None
    redeemed_at: datetime | None = None
    created_at: datetime


class RedeemCodePageOut(BaseModel):
    items: list[RedeemCodeRowOut]
    total: int
    limit: int
    offset: int


class RedeemCodeActionOut(BaseModel):
    id: uuid.UUID
    status: str
    changed: bool
    message: str


def _mask(prefix: str) -> str:
    """前缀 + 固定长度掩码：辨认得出是哪张卡，还原不出任何可用凭证。

    掩码长度故意与真实码长无关 —— 连「后缀有几位」都不额外说。
    """
    return f"{prefix}{'•' * 8}"


# --------------------------------------------------------------------------- #
# 列表
# --------------------------------------------------------------------------- #
@router.get("/redeem-codes", response_model=RedeemCodePageOut)
async def list_redeem_codes(
    batch_id: uuid.UUID | None = Query(default=None),
    code_status: str | None = Query(default=None, alias="status"),
    limit: int = Query(default=50, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    admin: User = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db),
) -> RedeemCodePageOut:
    """单码列表：可按批次与状态过滤（未兑换 = ``status=active``）。"""
    conds = []
    if batch_id is not None:
        conds.append(RedeemCode.batch_id == batch_id)
    if code_status:
        if code_status not in CODE_STATUSES:
            raise HTTPException(
                status.HTTP_400_BAD_REQUEST,
                f"未知状态：{code_status}（可选：{'、'.join(CODE_STATUSES)}）",
            )
        conds.append(RedeemCode.status == code_status)

    count_stmt = select(func.count()).select_from(RedeemCode)
    stmt = (
        select(RedeemCode, RedeemCodeBatch, User)
        .join(RedeemCodeBatch, RedeemCode.batch_id == RedeemCodeBatch.id)
        .outerjoin(User, RedeemCode.redeemed_by == User.id)
        .order_by(RedeemCode.created_at.desc())
    )
    for c in conds:
        count_stmt = count_stmt.where(c)
        stmt = stmt.where(c)

    total = (await db.execute(count_stmt)).scalar_one()
    rows = (await db.execute(stmt.limit(limit).offset(offset))).all()

    items = [
        RedeemCodeRowOut(
            id=code.id,
            batch_id=batch.id,
            batch_name=batch.name,
            credits_per_code=int(batch.credits_per_code),
            code_prefix=code.code_prefix,
            masked_code=_mask(code.code_prefix),
            status=code.status,
            expires_at=batch.expires_at,
            redeemed_by=code.redeemed_by,
            redeemer_email=(user.email if user else None),
            redeemer_username=(user.username if user else None),
            redeemed_at=code.redeemed_at,
            created_at=code.created_at,
        )
        for code, batch, user in rows
    ]
    return RedeemCodePageOut(items=items, total=int(total), limit=limit, offset=offset)


# --------------------------------------------------------------------------- #
# 单码操作
# --------------------------------------------------------------------------- #
async def _load_code(db: AsyncSession, code_id: uuid.UUID) -> RedeemCode:
    code = await db.get(RedeemCode, code_id)
    if code is None:
        raise HTTPException(NOT_FOUND, "兑换码不存在")
    return code


@router.post("/redeem-codes/{code_id}/void", response_model=RedeemCodeActionOut)
async def void_redeem_code(
    code_id: uuid.UUID,
    admin: User = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db),
) -> RedeemCodeActionOut:
    """作废单张码。条件更新（只有 ``active`` 改得动）—— 已兑换的改不动。

    用 ``UPDATE … WHERE status='active'`` 而不是「读—判—写」：两个管理员同时
    点作废时，rowcount 是唯一可信的答案（谁真的把状态改了）。
    """
    code = await _load_code(db, code_id)
    if code.status == "void":
        return RedeemCodeActionOut(
            id=code.id, status="void", changed=False, message="该码已经是作废状态"
        )
    if code.status == "redeemed":
        raise HTTPException(CONFLICT, "该码已被兑换，积分已入账，不能作废")

    result = await db.execute(
        update(RedeemCode)
        .where(RedeemCode.id == code_id, RedeemCode.status == "active")
        .values(status="void")
    )
    await db.commit()
    if not (result.rowcount or 0):
        # 读到 active、写回时已被别人处理：如实报冲突，让前端刷新列表。
        raise HTTPException(CONFLICT, "该码刚被其他人处理，请刷新后重试")

    await audit_service.log(
        actor_id=admin.id,
        action="credits:void_code",
        target=str(code_id),
        detail={"code_prefix": code.code_prefix, "batch_id": str(code.batch_id)},
    )
    return RedeemCodeActionOut(
        id=code_id, status="void", changed=True, message="已作废这张未使用的兑换码"
    )


@router.delete("/redeem-codes/{code_id}", response_model=RedeemCodeActionOut)
async def delete_redeem_code(
    code_id: uuid.UUID,
    admin: User = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db),
) -> RedeemCodeActionOut:
    """删除单张**未兑换**的码（误生成 / 重复生成的清理）。已兑换的一律拒绝。

    与作废的区别：作废保留行并永久拒绝再兑换；删除连行一起消失，批次
    ``total`` 随之减少 —— 所以只在「确认这张从未发放出去」时用删除。
    """
    code = await _load_code(db, code_id)
    if code.status == "redeemed":
        raise HTTPException(CONFLICT, "已兑换的码必须留档对账，不能删除")

    result = await db.execute(
        delete(RedeemCode).where(
            RedeemCode.id == code_id, RedeemCode.status != "redeemed"
        )
    )
    await db.commit()
    if not (result.rowcount or 0):
        raise HTTPException(CONFLICT, "该码刚被兑换，请刷新后重试")

    await audit_service.log(
        actor_id=admin.id,
        action="credits:delete_code",
        target=str(code_id),
        detail={
            "code_prefix": code.code_prefix,
            "batch_id": str(code.batch_id),
            "was_status": code.status,
        },
    )
    return RedeemCodeActionOut(
        id=code_id, status="deleted", changed=True, message="已删除这张未使用的兑换码"
    )
