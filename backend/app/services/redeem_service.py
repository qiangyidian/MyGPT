"""兑换码批次管理与兑换。

兑换是一个 CAS（compare-and-swap），不用应用层锁：

    UPDATE redeem_codes SET status='redeemed', ... WHERE id=? AND status='active'

``rowcount == 1`` 才算抢到。并发下只有一个事务能赢，输的一方拿到 0 行，走
"已被兑换"分支。这比"先 SELECT 判断再 UPDATE"可靠 —— 后者在 READ COMMITTED
下两个事务可以同时读到 active。

账本侧还有第二道闸：``credit_service.grant`` 用 ``ref_type='redeem_code'`` 的
唯一约束保证同一个码只会加一次分。CAS 与唯一约束各自独立，任一生效都不会
出现重复加分。
"""
from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from datetime import datetime, UTC

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.credits import (
    code_prefix,
    generate_code,
    get_credit_policy,
    hash_code,
    normalize_code,
)
from app.models import RedeemCode, RedeemCodeBatch
from app.services import credit_service
from app.services.credit_service import CreditError

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class RedeemResult:
    credits_added: int
    balance: int
    batch_name: str


@dataclass(frozen=True)
class BatchProgress:
    batch: RedeemCodeBatch
    total: int
    redeemed: int
    void: int
    active: int


async def create_batch(
    db: AsyncSession,
    *,
    admin_id: uuid.UUID | None,
    name: str,
    credits_per_code: int,
    count: int,
    expires_at: datetime | None = None,
    note: str | None = None,
) -> tuple[RedeemCodeBatch, list[str]]:
    """生成一批码，返回 ``(批次, 明文码列表)``。

    **这是明文码唯一一次出现的地方。** 库里只写哈希，调用方必须立刻展示 /
    导出，之后系统内再也拿不到明文。
    """
    policy = get_credit_policy()
    if int(credits_per_code) <= 0:
        raise CreditError("redeem_batch_invalid_credits", "每个兑换码的积分必须为正数")
    if int(count) <= 0:
        raise CreditError("redeem_batch_invalid_count", "生成数量必须为正数")
    if int(count) > policy.max_codes_per_batch:
        raise CreditError(
            "redeem_batch_too_large",
            f"单批最多生成 {policy.max_codes_per_batch} 个兑换码",
        )

    if expires_at is not None:
        expiry = expires_at
        if expiry.tzinfo is None:  # naive 输入按 UTC 处理
            expiry = expiry.replace(tzinfo=UTC)
        if expiry <= datetime.now(UTC):
            raise CreditError(
                "redeem_batch_invalid_expiry",
                "有效期必须晚于当前时间",
                400,
            )

    batch = RedeemCodeBatch(
        name=(name or "").strip() or "未命名批次",
        credits_per_code=int(credits_per_code),
        note=note,
        created_by=admin_id,
        expires_at=expires_at,
    )
    db.add(batch)
    await db.flush()

    plaintext: list[str] = []
    seen: set[str] = set()
    for _ in range(int(count)):
        # 极小概率与同批已生成的明文碰撞（也极可能与库里已存在的碰撞）——
        # 重试几次，用哈希去重。
        for _attempt in range(8):
            candidate = generate_code()
            normalized = normalize_code(candidate)
            digest = hash_code(normalized)
            if digest in seen:
                continue
            seen.add(digest)
            plaintext.append(candidate)
            db.add(
                RedeemCode(
                    batch_id=batch.id,
                    code_hash=digest,
                    code_prefix=code_prefix(normalized),
                    status="active",
                )
            )
            break
        else:  # pragma: no cover - 80 bit 空间下不可能连续 8 次碰撞
            raise CreditError("redeem_code_collision", "兑换码生成冲突，请重试")

    await db.flush()
    return batch, plaintext


async def redeem(db: AsyncSession, *, user_id: uuid.UUID, raw_code: str) -> RedeemResult:
    """兑换一个码。失败抛 :class:`CreditError`（带 HTTP 状态码与稳定 code）。"""
    normalized = normalize_code(raw_code or "")
    if not normalized:
        raise CreditError("redeem_code_not_found", "兑换码不存在，请检查是否输入有误", 404)

    row = (
        await db.execute(
            select(RedeemCode, RedeemCodeBatch)
            .join(RedeemCodeBatch, RedeemCode.batch_id == RedeemCodeBatch.id)
            .where(RedeemCode.code_hash == hash_code(normalized))
        )
    ).first()
    if row is None:
        raise CreditError("redeem_code_not_found", "兑换码不存在，请检查是否输入有误", 404)

    code, batch = row
    if code.status == "void":
        raise CreditError("redeem_code_void", "该兑换码已作废", 410)
    if code.status == "redeemed":
        raise CreditError("redeem_code_used", "该兑换码已被使用", 409)
    if batch.expires_at is not None:
        expires_at = batch.expires_at
        if expires_at.tzinfo is None:  # SQLite 取回来可能是 naive
            expires_at = expires_at.replace(tzinfo=UTC)
        if expires_at <= datetime.now(UTC):
            raise CreditError("redeem_code_expired", "该兑换码已过期", 410)

    # CAS：并发下只有一个事务能拿到 rowcount == 1。
    claimed = await db.execute(
        update(RedeemCode)
        .where(RedeemCode.id == code.id, RedeemCode.status == "active")
        .values(status="redeemed", redeemed_by=user_id, redeemed_at=datetime.now(UTC))
    )
    if claimed.rowcount != 1:
        # 同一瞬间被别人抢走了。
        raise CreditError("redeem_code_used", "该兑换码已被使用", 409)

    await credit_service.grant(
        db,
        user_id,
        amount=int(batch.credits_per_code),
        reason="redeem",
        ref_type="redeem_code",
        ref_id=str(code.id),
        actor_id=user_id,
        note=batch.name,
    )
    account = await credit_service.read_account(db, user_id)
    await db.flush()
    return RedeemResult(
        credits_added=int(batch.credits_per_code),
        balance=int(account.balance) if account else int(batch.credits_per_code),
        batch_name=batch.name,
    )


async def list_batches(db: AsyncSession, *, limit: int = 100) -> list[BatchProgress]:
    """批次列表含核销进度。一次聚合查询，不是每批一条 count。"""
    progress = (
        select(
            RedeemCode.batch_id.label("batch_id"),
            func.count().label("total"),
            func.sum(_status_flag("redeemed")).label("redeemed"),
            func.sum(_status_flag("void")).label("void"),
            func.sum(_status_flag("active")).label("active"),
        )
        .group_by(RedeemCode.batch_id)
        .subquery()
    )
    rows = (
        await db.execute(
            select(RedeemCodeBatch, progress.c.total, progress.c.redeemed, progress.c.void, progress.c.active)
            .outerjoin(progress, progress.c.batch_id == RedeemCodeBatch.id)
            .order_by(RedeemCodeBatch.created_at.desc())
            .limit(max(1, min(int(limit), 500)))
        )
    ).all()
    return [
        BatchProgress(
            batch=row[0],
            total=int(row[1] or 0),
            redeemed=int(row[2] or 0),
            void=int(row[3] or 0),
            active=int(row[4] or 0),
        )
        for row in rows
    ]


def _status_flag(status: str):
    """把 ``status == '<value>'`` 变成一个 0/1 求和项。"""
    from sqlalchemy import case

    return case((RedeemCode.status == status, 1), else_=0)


async def list_codes(
    db: AsyncSession, *, batch_id: uuid.UUID, limit: int = 200, offset: int = 0
) -> list[RedeemCode]:
    rows = (
        await db.execute(
            select(RedeemCode)
            .where(RedeemCode.batch_id == batch_id)
            .order_by(RedeemCode.created_at.asc())
            .limit(max(1, min(int(limit), 1000)))
            .offset(max(0, int(offset)))
        )
    ).scalars().all()
    return list(rows)


async def void_batch(db: AsyncSession, *, batch_id: uuid.UUID) -> int:
    """作废该批所有 ``active`` 码，返回作废数量。已兑换的不受影响。"""
    result = await db.execute(
        update(RedeemCode)
        .where(RedeemCode.batch_id == batch_id, RedeemCode.status == "active")
        .values(status="void")
    )
    await db.flush()
    return int(result.rowcount or 0)
