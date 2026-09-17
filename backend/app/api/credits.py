"""积分路由：用户侧兑换与余额，管理侧发码与调分。

两个 router 共用一个模块（与 ``app/api/memories.py`` 的
``router`` + ``user_router`` 同款约定），因为它们是同一个功能的两面。

所有业务失败都抛 :class:`AppException` —— 只有它会发出前端能解析的 ``code``
字段（裸 ``HTTPException`` 会被映射成 ``http_410`` 这类无意义的码，见
``app/core/exceptions.py`` 的 ``_STATUS_CODES``）。
"""
from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, Query
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.credits import get_credit_policy
from app.core.deps import get_current_admin, get_current_user
from app.core.exceptions import AppException
from app.core.rate_limit import rate_limit_user
from app.db import get_db
from app.models import User
from app.schemas import (
    CreditAccountOut,
    CreditAccountRowOut,
    CreditAdjustRequest,
    LedgerEntryOut,
    LedgerPageOut,
    RedeemBatchCreate,
    RedeemBatchCreateOut,
    RedeemBatchOut,
    RedeemBatchProgressOut,
    RedeemCodeOut,
    RedeemRequest,
    RedeemResultOut,
    VoidBatchOut,
)
from app.services import credit_service, redeem_service
from app.services.credit_service import CreditError

router = APIRouter(prefix="/api/credits", tags=["credits"])
admin_router = APIRouter(prefix="/api/admin", tags=["admin-credits"])


def _to_app_exception(exc: CreditError) -> AppException:
    return AppException(exc.status_code, exc.code, exc.message)


# --------------------------------------------------------------------------- #
# 用户侧
# --------------------------------------------------------------------------- #
@router.get("/me", response_model=CreditAccountOut)
async def my_credits(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> CreditAccountOut:
    """余额。账户行不存在时返回 0（不顺手创建，读接口不该有写副作用）。"""
    account = await credit_service.read_account(db, user.id)
    return CreditAccountOut(
        balance=int(account.balance) if account else 0,
        lifetime_granted=int(account.lifetime_granted) if account else 0,
        lifetime_consumed=int(account.lifetime_consumed) if account else 0,
        enforced=get_credit_policy().enforced,
    )


@router.post(
    "/redeem",
    response_model=RedeemResultOut,
    dependencies=[Depends(rate_limit_user(10, 60, "credits-redeem"))],
)
async def redeem(
    payload: RedeemRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> RedeemResultOut:
    try:
        result = await redeem_service.redeem(db, user_id=user.id, raw_code=payload.code)
    except CreditError as exc:
        raise _to_app_exception(exc)
    # 服务层只 flush，提交归路由（见 redeem_service 的事务边界约定）。
    await db.commit()
    return RedeemResultOut(
        credits_added=result.credits_added,
        balance=result.balance,
        batch_name=result.batch_name,
    )


@router.get("/ledger", response_model=LedgerPageOut)
async def my_ledger(
    limit: int = Query(default=50, ge=1, le=200),
    cursor: str | None = Query(default=None),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> LedgerPageOut:
    rows, next_cursor = await credit_service.ledger_page(
        db, user.id, limit=limit, cursor=cursor
    )
    return LedgerPageOut(
        entries=[LedgerEntryOut.model_validate(r) for r in rows],
        next_cursor=next_cursor,
    )


# --------------------------------------------------------------------------- #
# 管理侧
# --------------------------------------------------------------------------- #
@admin_router.post("/redeem-batches", response_model=RedeemBatchCreateOut)
async def create_redeem_batch(
    payload: RedeemBatchCreate,
    admin: User = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db),
) -> RedeemBatchCreateOut:
    """生成一批兑换码。

    响应里的 ``codes`` 是明文，**只在这一次返回**。库里只写 peppered
    HMAC-SHA256 哈希，此后再无法取回明文 —— 管理员必须当场导出。
    """
    try:
        batch, codes = await redeem_service.create_batch(
            db,
            admin_id=admin.id,
            name=payload.name,
            credits_per_code=payload.credits_per_code,
            count=payload.count,
            expires_at=payload.expires_at,
            note=payload.note,
        )
    except CreditError as exc:
        raise _to_app_exception(exc)
    await db.commit()
    await db.refresh(batch)
    return RedeemBatchCreateOut(
        batch=RedeemBatchOut.model_validate(batch), codes=codes
    )


@admin_router.get("/redeem-batches", response_model=list[RedeemBatchProgressOut])
async def list_redeem_batches(
    limit: int = Query(default=100, ge=1, le=500),
    admin: User = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db),
) -> list[RedeemBatchProgressOut]:
    rows = await redeem_service.list_batches(db, limit=limit)
    return [
        RedeemBatchProgressOut(
            batch=RedeemBatchOut.model_validate(row.batch),
            total=row.total,
            redeemed=row.redeemed,
            void=row.void,
            active=row.active,
        )
        for row in rows
    ]


@admin_router.get("/redeem-batches/{batch_id}/codes", response_model=list[RedeemCodeOut])
async def list_redeem_codes(
    batch_id: uuid.UUID,
    limit: int = Query(default=200, ge=1, le=1000),
    offset: int = Query(default=0, ge=0),
    admin: User = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db),
) -> list[RedeemCodeOut]:
    """批次内码列表。只返回 6 位前缀，绝不含哈希或明文。"""
    rows = await redeem_service.list_codes(
        db, batch_id=batch_id, limit=limit, offset=offset
    )
    return [RedeemCodeOut.model_validate(r) for r in rows]


@admin_router.post("/redeem-batches/{batch_id}/void", response_model=VoidBatchOut)
async def void_redeem_batch(
    batch_id: uuid.UUID,
    admin: User = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db),
) -> VoidBatchOut:
    """作废该批所有未使用的码。已兑换的不受影响（分已经到用户账上了）。"""
    voided = await redeem_service.void_batch(db, batch_id=batch_id)
    await db.commit()

    # 作废是破坏性操作，留审计。best-effort，用自己的会话。
    from app.services import audit_service

    await audit_service.log(
        actor_id=admin.id,
        action="credits:void_batch",
        target=str(batch_id),
        detail={"voided": voided},
    )
    return VoidBatchOut(voided=voided)


@admin_router.get("/credits/accounts", response_model=list[CreditAccountRowOut])
async def list_credit_accounts(
    search: str | None = Query(default=None),
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    admin: User = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db),
) -> list[CreditAccountRowOut]:
    rows = await credit_service.list_accounts(
        db, search=search, limit=limit, offset=offset
    )
    return [
        CreditAccountRowOut(
            user_id=user.id,
            email=user.email,
            username=user.username,
            balance=int(account.balance) if account else 0,
            lifetime_granted=int(account.lifetime_granted) if account else 0,
            lifetime_consumed=int(account.lifetime_consumed) if account else 0,
        )
        for user, account in rows
    ]


@admin_router.get("/credits/ledger", response_model=LedgerPageOut)
async def admin_user_ledger(
    user_id: uuid.UUID = Query(...),
    limit: int = Query(default=50, ge=1, le=200),
    cursor: str | None = Query(default=None),
    admin: User = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db),
) -> LedgerPageOut:
    """查看某个用户的流水 —— 计费争议的关键证据。

    与用户侧 ``/api/credits/ledger`` 同构（同样的游标分页与行形状），只是
    ``user_id`` 由管理员指定。计费投诉（"一条消息扣了我 3000 分"）时运营
    通过它看到 reason / ref / note / actor，而不是去倒临时 SQL。
    """
    target = (
        await db.execute(select(User).where(User.id == user_id))
    ).scalar_one_or_none()
    if target is None:
        raise AppException(404, "user_not_found", "用户不存在")

    rows, next_cursor = await credit_service.ledger_page(
        db, user_id, limit=limit, cursor=cursor
    )
    return LedgerPageOut(
        entries=[LedgerEntryOut.model_validate(r) for r in rows],
        next_cursor=next_cursor,
    )


@admin_router.post("/credits/adjust", response_model=CreditAccountRowOut)
async def adjust_credits(
    payload: CreditAdjustRequest,
    admin: User = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db),
) -> CreditAccountRowOut:
    """手动加 / 扣积分。走同一套账本，并留下审计事件。"""
    target = (
        await db.execute(select(User).where(User.id == payload.user_id))
    ).scalar_one_or_none()
    if target is None:
        raise AppException(404, "user_not_found", "用户不存在")

    try:
        await credit_service.adjust(
            db,
            payload.user_id,
            delta=payload.delta,
            actor_id=admin.id,
            note=payload.note,
        )
    except CreditError as exc:
        raise _to_app_exception(exc)
    account = await credit_service.read_account(db, payload.user_id)
    await db.commit()

    # 审计：best-effort，用自己的会话，失败不影响调分结果。
    from app.services import audit_service

    await audit_service.log(
        actor_id=admin.id,
        action="credits:adjust",
        target=str(payload.user_id),
        detail={"delta": payload.delta, "note": payload.note},
    )

    return CreditAccountRowOut(
        user_id=target.id,
        email=target.email,
        username=target.username,
        balance=int(account.balance) if account else 0,
        lifetime_granted=int(account.lifetime_granted) if account else 0,
        lifetime_consumed=int(account.lifetime_consumed) if account else 0,
    )
