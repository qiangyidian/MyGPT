"""积分账户与账本的读写。

三条并发纪律，全部落在数据库机制上而非应用层自觉：

1. **余额变动先锁账户行** —— :func:`_locked_account` 用 ``SELECT ... FOR UPDATE``。
   Postgres 上渲染成真行锁；SQLite 方言直接忽略该子句，而 SQLite 本身是单写者
   模型，所以测试库语义依然正确，一套代码不用分叉。
2. **发放 / 扣费幂等** —— 靠 ``uq_credit_ledger_ref`` 唯一部分索引。重复调用会
   撞约束，捕获后按幂等命中处理（返回 ``None``）。捕获时必须用
   ``begin_nested()`` 开 SAVEPOINT，否则 IntegrityError 会毒化整个外层事务
   （Postgres 上后续任何语句都会失败）。
3. **余额只通过本模块变动** —— 别处直接改 ``CreditAccount.balance`` 会绕过账本，
   让对账 SQL 报警。

本模块所有函数只 **flush，从不 commit** —— 事务边界归调用方所有：
聊天扣费必须与消息写入同事务原子提交，因此这层一旦提交就会破坏该保证。
"""
from __future__ import annotations

import logging
import uuid
from datetime import datetime, UTC

from sqlalchemy import select, tuple_
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.credits import compute_charge, get_credit_policy
from app.models import CreditAccount, CreditLedger, Message, User

logger = logging.getLogger(__name__)


class CreditError(Exception):
    """积分业务错误。API 层捕获后转成 :class:`AppException`。"""

    def __init__(self, code: str, message: str, status_code: int = 400) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code


def _is_unique_violation(exc: IntegrityError) -> bool:
    """跨方言判断是否唯一约束冲突。

    psycopg/asyncpg 用 SQLSTATE 23505，SQLite 用 "UNIQUE constraint failed"
    文案。两者都判断，避免为了可移植性引入方言分支。
    """
    orig = getattr(exc, "orig", None)
    if getattr(orig, "pgcode", None) == "23505":
        return True
    return "unique" in str(orig).lower()


# --------------------------------------------------------------------------- #
# 账户
# --------------------------------------------------------------------------- #
async def read_account(db: AsyncSession, user_id: uuid.UUID) -> CreditAccount | None:
    """只读账户，不存在返回 None（不创建）。"""
    return await db.get(CreditAccount, user_id)


async def get_or_create_account(
    db: AsyncSession, user_id: uuid.UUID
) -> CreditAccount:
    """取得账户行，不存在则创建。

    正常路径下账户行永远存在（迁移回填 + 注册时创建），这里是兜底。
    并发创建时输的一方撞唯一约束，回滚到 SAVEPOINT 后重查即可。
    """
    account = await db.get(CreditAccount, user_id)
    if account is not None:
        return account
    try:
        async with db.begin_nested():
            account = CreditAccount(user_id=user_id, balance=0)
            db.add(account)
            await db.flush()
        return account
    except IntegrityError:
        # 并发下别人先建好了 —— 重新取一次。
        account = await db.get(CreditAccount, user_id)
        if account is None:  # pragma: no cover - 只可能在异常被误判时发生
            raise
        return account


async def _locked_account(db: AsyncSession, user_id: uuid.UUID) -> CreditAccount:
    """取账户行并加行锁。所有余额变动的唯一入口。

    ``populate_existing=True`` 不是可选项：会话的 identity map 里可能已经有这个
    account 对象，而 SQLAlchemy 默认**不会**用查询结果覆盖已加载的属性。那样
    即使 ``FOR UPDATE`` 拿到了新行，``account.balance`` 仍是旧值，
    :func:`_write_entry` 会基于陈旧的余额算出错误的 ``balance_after`` 和最终
    余额 —— 在并发充值/扣费下就是直接算错钱。加上它强制用锁定读到的新值覆盖。
    """
    await get_or_create_account(db, user_id)
    result = await db.execute(
        select(CreditAccount)
        .where(CreditAccount.user_id == user_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    return result.scalar_one()


async def _write_entry(
    db: AsyncSession,
    account: CreditAccount,
    *,
    delta: int,
    reason: str,
    ref_type: str | None,
    ref_id: str | None,
    actor_id: uuid.UUID | None,
    note: str | None,
) -> CreditLedger | None:
    """写流水 + 更新余额。唯一约束冲突时返回 None（幂等命中）。

    调用方必须已经持有账户行锁（见 :func:`_locked_account`），否则并发下
    ``balance_after`` 会算错。
    """
    new_balance = int(account.balance) + int(delta)
    entry = CreditLedger(
        user_id=account.user_id,
        delta=int(delta),
        balance_after=new_balance,
        reason=reason,
        ref_type=ref_type,
        ref_id=ref_id,
        actor_id=actor_id,
        note=note,
    )
    try:
        async with db.begin_nested():
            db.add(entry)
            await db.flush()
    except IntegrityError as exc:
        if not _is_unique_violation(exc):
            raise
        return None

    account.balance = new_balance
    if delta > 0:
        account.lifetime_granted = int(account.lifetime_granted) + int(delta)
    elif delta < 0:
        account.lifetime_consumed = int(account.lifetime_consumed) + (-int(delta))
    await db.flush()
    return entry


# --------------------------------------------------------------------------- #
# 发放 / 扣费 / 调分
# --------------------------------------------------------------------------- #
async def grant(
    db: AsyncSession,
    user_id: uuid.UUID,
    *,
    amount: int,
    reason: str,
    ref_type: str | None = None,
    ref_id: str | None = None,
    actor_id: uuid.UUID | None = None,
    note: str | None = None,
) -> CreditLedger | None:
    """发放积分。``amount`` 必须为正。相同 ref 重复调用返回 None。"""
    if amount <= 0:
        raise CreditError("credit_grant_invalid", "发放积分必须为正数")
    account = await _locked_account(db, user_id)
    return await _write_entry(
        db,
        account,
        delta=int(amount),
        reason=reason,
        ref_type=ref_type,
        ref_id=ref_id,
        actor_id=actor_id,
        note=note,
    )


async def charge_usage(
    db: AsyncSession,
    user_id: uuid.UUID,
    *,
    amount: int,
    ref_type: str,
    ref_id: str,
    note: str | None = None,
) -> CreditLedger | None:
    """扣减积分。相同 ``(ref_type, ref_id, 'usage')`` 重复调用返回 None。"""
    if amount <= 0:
        return None
    account = await _locked_account(db, user_id)
    return await _write_entry(
        db,
        account,
        delta=-int(amount),
        reason="usage",
        ref_type=ref_type,
        ref_id=ref_id,
        actor_id=None,
        note=note,
    )


async def adjust(
    db: AsyncSession,
    user_id: uuid.UUID,
    *,
    delta: int,
    actor_id: uuid.UUID,
    note: str | None,
) -> CreditLedger:
    """管理员手动调分。``delta`` 非零且绝对值不超过策略上限。

    调分不设唯一约束（``ref_type=None``），管理员可以多次调整同一用户 —— 这
    正是唯一索引必须是**部分**索引的原因。
    """
    if int(delta) == 0:
        raise CreditError("credit_adjust_zero", "调分数量不能为 0")
    cap = get_credit_policy().max_adjust
    if abs(int(delta)) > cap:
        raise CreditError(
            "credit_adjust_too_large",
            f"单次调分绝对值不能超过 {cap}",
        )
    account = await _locked_account(db, user_id)
    entry = await _write_entry(
        db,
        account,
        delta=int(delta),
        reason="admin_adjust",
        ref_type=None,
        ref_id=None,
        actor_id=actor_id,
        note=note,
    )
    if entry is None:  # pragma: no cover - ref 为 NULL 不会撞唯一约束
        raise CreditError("credit_adjust_failed", "调分失败")
    return entry


# --------------------------------------------------------------------------- #
# 挂到聊天轮次上的扣费
# --------------------------------------------------------------------------- #
async def charge_message_credits(
    db: AsyncSession, user_id: uuid.UUID, message: Message
) -> int:
    """按 ``message`` 上已落地的服务端用量扣积分，返回实际扣减数（0 = 未扣）。

    读的是 :func:`app.services.chat_service._apply_usage_accounting` 写入的
    权威字段（服务端实测，永不信客户端）。幂等由 ``ref_type='message'`` 的
    唯一索引保证。

    零消耗（无 usage 的失败轮、mock 响应）不扣，也不创建账户行 —— 避免给
    从未产生消耗的用户凭空建行。
    """
    policy = get_credit_policy()
    amount = compute_charge(
        message.cost_usd,
        message.total_tokens or (
            (message.prompt_tokens or 0) + (message.completion_tokens or 0)
        ),
        policy,
    )
    if amount <= 0:
        return 0
    if getattr(message, "id", None) is None:
        # 防御：正常路径下 assistant 占位行在流式开始前就已提交，id 一定在。
        await db.flush()
    entry = await charge_usage(
        db,
        user_id,
        amount=amount,
        ref_type="message",
        ref_id=str(message.id),
    )
    return amount if entry is not None else 0


# --------------------------------------------------------------------------- #
# 查询
# --------------------------------------------------------------------------- #
def encode_cursor(entry: CreditLedger) -> str:
    """游标 = ``{created_at.isoformat()}_{id}``。

    次级排序键用 id，避免同一时间戳的流水在分页时漏读或重读。
    """
    return f"{entry.created_at.isoformat()}_{entry.id}"


def decode_cursor(cursor: str) -> tuple[datetime, uuid.UUID] | None:
    """解析游标。格式非法返回 None（按第一页处理，不报错）。"""
    try:
        ts, _, raw_id = cursor.rpartition("_")
        return datetime.fromisoformat(ts), uuid.UUID(raw_id)
    except (ValueError, AttributeError):
        return None


async def ledger_page(
    db: AsyncSession, user_id: uuid.UUID, *, limit: int, cursor: str | None = None
) -> tuple[list[CreditLedger], str | None]:
    """倒序流水页。返回 ``(rows, next_cursor)``，``next_cursor`` 为 None 表示到底。"""
    size = max(1, min(int(limit), 200))
    stmt = select(CreditLedger).where(CreditLedger.user_id == user_id)
    if cursor:
        decoded = decode_cursor(cursor)
        if decoded is not None:
            ts, entry_id = decoded
            stmt = stmt.where(
                tuple_(CreditLedger.created_at, CreditLedger.id) < tuple_(ts, entry_id)
            )
    # 多取一行用于判断是否还有下一页。
    stmt = stmt.order_by(
        CreditLedger.created_at.desc(), CreditLedger.id.desc()
    ).limit(size + 1)
    rows = list((await db.execute(stmt)).scalars().all())
    has_more = len(rows) > size
    rows = rows[:size]
    return rows, (encode_cursor(rows[-1]) if has_more and rows else None)


async def list_accounts(
    db: AsyncSession, *, search: str | None, limit: int, offset: int
) -> list[tuple[User, CreditAccount]]:
    """用户余额列表（后台用）。搜索匹配邮箱或用户名。"""
    stmt = (
        select(User, CreditAccount)
        .outerjoin(CreditAccount, CreditAccount.user_id == User.id)
        .order_by(User.created_at.desc())
        .limit(max(1, min(int(limit), 500)))
        .offset(max(0, int(offset)))
    )
    if search:
        needle = f"%{search.strip()}%"
        stmt = stmt.where(User.email.ilike(needle) | User.username.ilike(needle))
    return [(row[0], row[1]) for row in (await db.execute(stmt)).all()]
