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

唯一例外是轮次准入 :func:`admit_turn` / :func:`release_turn_hold`：预留的整个
意义就是"在别人开始之前已经落库"，跟着一个要到整轮结束才 commit 的长事务走
等于没有预留。所以它们自己开会话、自己 commit，并且不碰调用方的事务。
"""
from __future__ import annotations

import asyncio
import logging
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import case, func, select, tuple_
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.credits import CreditPolicy, compute_charge, get_credit_policy
from app.core.like import LIKE_ESCAPE, like_pattern
from app.models import CreditAccount, CreditLedger, Message, User
from app.observability import observe_counter

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
    count_toward_lifetime: bool = True,
) -> CreditLedger | None:
    """写流水 + 更新余额。唯一约束冲突时返回 None（幂等命中）。

    调用方必须已经持有账户行锁（见 :func:`_locked_account`），否则并发下
    ``balance_after`` 会算错。

    ``count_toward_lifetime=False`` 给"临时占位"类流水（轮次预留 / 退还）用：
    ``lifetime_granted`` 与 ``lifetime_consumed`` 是对账 SQL 的口径，预留既不
    是发放也不是消耗 —— 计入的话一次充值前的普通对话就会把两个累计量各抬高
    ``turn_reserve``，而预留与退还在余额上互相抵零、看不出来。
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
    if count_toward_lifetime:
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
# 轮次准入（finding 40）
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class TurnHold:
    """一次准入留下的预留句柄。轮次结束时必须交给 :func:`release_turn_hold`。"""

    user_id: uuid.UUID
    turn_key: str
    amount: int


async def admit_turn(
    user_id: uuid.UUID,
    *,
    turn_key: str | None = None,
    policy: CreditPolicy | None = None,
    session_factory: Any | None = None,
) -> TurnHold | None:
    """预留一轮的额度；余额不足抛 :class:`CreditError`（402）。

    为什么必须是"锁行 + 写一条预留流水"，而不是读一下余额：准入检查原本用
    :func:`read_account`（无锁快照读），N 个并发轮次读到同一个 ``balance=1``
    会全部放行 —— 单进程里 ``asyncio`` 的交错点正好落在 ``await`` 上，所以这
    不是理论问题，是必然发生的。这里走 :func:`_locked_account`（Postgres 上是
    真 ``SELECT ... FOR UPDATE``），预留额以 ``reason='turn_hold'`` 的负向流水
    **落库**，所以第二个轮次拿到锁时读到的已经是扣掉预留后的余额。

    三条边界：

    * ``ref_type='turn_hold'`` + 同一 ``turn_key`` 撞唯一部分索引 —— 重复准入
      按幂等处理（不再扣一次预留）。
    * 预留额是 ``policy.turn_reserve``，与 ``max_credits_per_turn`` 无关：前者
      是"见底就挡"的下界，后者是单轮扣费上界（见 :func:`app.credits.compute_charge`）。
    * 观察模式（``enforced=False``）直接返回 ``None``，不锁行、不写流水 ——
      与扣费侧同一套语义，免得准入偷偷变成拦截。

    进程在预留之后、结算之前崩溃：这条预留留在账上（余额被压低 ``turn_reserve``），
    由 :func:`release_stale_turn_holds` 回收 —— 见该函数的说明。
    """
    p = policy or get_credit_policy()
    if not p.enforced:
        return None
    reserve = int(p.turn_reserve)
    if reserve <= 0:
        # 预留额配成 0 = 只做"余额>0"的检查，但那是无锁读，等于回到 race。
        # 明确拒绝这种配法，而不是假装它安全。
        reserve = 1
    key = turn_key or str(uuid.uuid4())
    factory = session_factory or _default_session_factory()
    # 预留必须**独立提交**才算预留：跟着调用方的事务一起 flush，就等于在
    # "整轮结束才提交"的事务里锁了个只有自己看得到的余额。所以这里刻意开一
    # 个自己的会话，而不是复用聊天请求那个长事务的 session —— 后者此时还没
    # 有任何待写入的行，提前 commit 也不会连带提交别人的工作。
    async with factory() as session:
        account = await _locked_account(session, user_id)
        balance = int(account.balance)
        if balance < reserve:
            # 告警口径（见 deploy/monitoring/rules/mygpt.yml）：拒绝率是"用户已经
            # 见底"最直接信号，所以计数必须发生在这里而不是 API 层 —— 观察模式
            # 与配额层挡掉的原因不同，混在一个标签里就没法定位。
            observe_counter("credit.admission_rejected", 1, outcome="insufficient")
            raise CreditError(
                "insufficient_credits",
                "积分不足，请先兑换后再继续对话",
                status_code=402,
            )
        entry = await _write_entry(
            session,
            account,
            delta=-reserve,
            reason="turn_hold",
            ref_type="turn_hold",
            ref_id=key,
            actor_id=None,
            note=None,
            count_toward_lifetime=False,
        )
        await session.commit()
    if entry is None:
        # 幂等命中：这一轮已经预留过了（重试/重放）。按已放行处理，不再扣一次。
        logger.debug("admit_turn: duplicate hold for %s ignored", key)
    return TurnHold(user_id=user_id, turn_key=key, amount=reserve)


async def release_turn_hold(
    hold: TurnHold | None, *, session_factory: Any | None = None
) -> CreditLedger | None:
    """退还预留（轮次结束，无论成功/失败/取消都要调用）。

    实际扣分由 :func:`charge_message_credits` 独立成账，所以这里只把预留原额
    加回去 —— 两条流水（hold / hold_release）互相抵零，对账时能一眼看出哪些
    轮次只留了 hold 没留 release（进程被杀的那批）。幂等：同一
    ``(turn_hold_release, turn_key)`` 撞唯一索引时返回 None。
    """
    if hold is None:
        return None
    factory = session_factory or _default_session_factory()
    async with factory() as session:
        account = await _locked_account(session, hold.user_id)
        entry = await _write_entry(
            session,
            account,
            delta=hold.amount,
            reason="turn_hold_release",
            ref_type="turn_hold_release",
            ref_id=hold.turn_key,
            actor_id=None,
            note=None,
            count_toward_lifetime=False,
        )
        await session.commit()
        return entry


async def release_stale_turn_holds(
    db: AsyncSession, *, older_than_seconds: int = 6 * 3600
) -> int:
    """回收没有配对 release 的历史预留（被杀进程留下的悬挂 hold）。

    准入的预留是"这一轮会跑完并结算"的承诺，进程被 OOMKill 时承诺兑现不了，
    余额就长期被压住。这里找出 ``turn_hold`` 有、对应 ``turn_hold_release`` 无、
    且已经老于单轮最长可能时长（默认 6 小时，与 retention 的周期同量级）的行，
    补一条 release。由 retention 清扫循环调用 —— 同一把 leader 锁下只会跑一个
    进程，所以不会和别的进程抢同一张 hold。

    时间截断在 Python 侧算，不用 ``now() - interval``：方言差异（SQLite 测试库
    没有 ``make_interval``）不该让这个回收路径分成两条代码。数据库里的
    ``created_at`` 是 UTC 带时区的，所以这里也用带时区的 UTC。
    """
    cutoff = datetime.now(UTC) - timedelta(
        seconds=max(int(older_than_seconds), 60)
    )
    released_keys = {
        str(row[0])
        for row in (
            await db.execute(
                select(CreditLedger.ref_id).where(
                    CreditLedger.reason == "turn_hold_release",
                    CreditLedger.ref_id.isnot(None),
                )
            )
        ).all()
    }
    holds = list(
        (
            await db.execute(
                select(CreditLedger)
                .where(
                    CreditLedger.reason == "turn_hold",
                    CreditLedger.created_at < cutoff,
                )
                .limit(500)
            )
        )
        .scalars()
        .all()
    )
    released = 0
    for entry in holds:
        if entry.ref_id is None or str(entry.ref_id) in released_keys:
            continue
        # delta 是负数（准入时扣掉的预留），退还时取反。
        if await _release_hold_in(
            db,
            TurnHold(
                user_id=entry.user_id,
                turn_key=str(entry.ref_id),
                amount=-int(entry.delta),
            ),
        ):
            released += 1
    return released


def _default_session_factory() -> Any:
    """懒取进程级 session factory（避免 import 期就建连接池）。"""
    from app.db import AsyncSessionLocal

    return AsyncSessionLocal


async def _release_hold_in(
    db: AsyncSession, hold: TurnHold
) -> CreditLedger | None:
    """在**给定会话**里退还预留 —— 供悬挂 hold 回收这种批处理场景用。

    :func:`release_turn_hold` 自己开事务并 commit，而回收循环是一个大 sweep
    事务的一部分，逐条 commit 会让它失去可重入性（中途失败就留下半批）。
    """
    account = await _locked_account(db, hold.user_id)
    return await _write_entry(
        db,
        account,
        delta=hold.amount,
        reason="turn_hold_release",
        ref_type="turn_hold_release",
        ref_id=hold.turn_key,
        actor_id=None,
        note=None,
        count_toward_lifetime=False,
    )


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
    if term := (search or "").strip():
        pattern = like_pattern(term)
        stmt = stmt.where(
            User.email.ilike(pattern, escape=LIKE_ESCAPE)
            | User.username.ilike(pattern, escape=LIKE_ESCAPE)
        )
    return [(row[0], row[1]) for row in (await db.execute(stmt)).all()]


# --------------------------------------------------------------------------- #
# 池子水位（finding 38 的 credit-depletion 口径）
# --------------------------------------------------------------------------- #
async def read_pool_watermarks(
    db: AsyncSession,
) -> tuple[int, int, int, int]:
    """全表聚合：``(累计发放, 累计消耗, 余额<=0 的账户数, 余额<0 的账户数)``。

    一次查询而不是按用户循环：账户数是有上限的（每个用户一行），但分钟级重复
    扫全表仍然值得省掉。``balance<0`` 单独计数，因为余额为负是本设计的正常状态
    （超扣后再由兑换补回），它**突增**才是信号，绝对值不是。
    """
    stmt = select(
        func.coalesce(func.sum(CreditAccount.lifetime_granted), 0),
        func.coalesce(func.sum(CreditAccount.lifetime_consumed), 0),
        func.sum(case((CreditAccount.balance <= 0, 1), else_=0)),
        func.sum(case((CreditAccount.balance < 0, 1), else_=0)),
    )
    granted, consumed, at_zero, negative = (await db.execute(stmt)).one()
    return (
        int(granted or 0),
        int(consumed or 0),
        int(at_zero or 0),
        int(negative or 0),
    )


async def run_credit_pool_poller(
    stop: asyncio.Event | None = None,
    *,
    interval: float = 300.0,
    session_factory: Any | None = None,
) -> None:
    """把池子水位导出为 gauge：``credit_pool_*``。

    放在 API 进程而不是 worker，理由与
    :func:`app.agents.workflow.queue.run_queue_depth_poller` 相同：只有 API 暴露
    ``/metrics``，worker 观测到的值 Alertmanager 永远抓不到。

    默认 5 分钟一次（队列水位是 15 秒）：这是一次聚合扫描，而"现在有人见底了"
    的实时信号本来就该由 ``credit_admission_rejected_total`` 的速率提供 —— 那是
    计数、不扫表。扫描失败时跳过样本而不是写 0：把"聚合超时"报成"池子空了"会
    制造一条假的高优先级告警，比晚 5 分钟知道更糟。
    """
    from app.observability import observe_gauge

    poll = max(float(interval), 30.0)
    factory = session_factory
    while stop is None or not stop.is_set():
        try:
            async with (factory or _default_session_factory())() as session:
                granted, consumed, at_zero, negative = await read_pool_watermarks(session)
            observe_gauge("credit.pool_granted", float(granted))
            observe_gauge("credit.pool_consumed", float(consumed))
            observe_gauge("credit.pool_accounts_at_zero", float(at_zero))
            observe_gauge("credit.pool_accounts_negative", float(negative))
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.debug("credit pool watermark poll failed", exc_info=True)
        if stop is None:
            await asyncio.sleep(poll)
            continue
        try:
            await asyncio.wait_for(stop.wait(), timeout=poll)
        except TimeoutError:
            pass
