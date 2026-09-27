"""Admin operations: user management, usage stats, system health.

Health checks are defensive — a component that can't be reached reports ``down``
rather than raising, so the status endpoint itself never 500s.
"""
from __future__ import annotations

import logging
import time
from datetime import date, datetime, timedelta, UTC

from sqlalchemy import case, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Conversation, Document, Message, User
from app.schemas import (
    SystemStatus,
    UsageMetrics,
    UsageReportPage,
    UsageReportRow,
    UsageStat,
)

logger = logging.getLogger(__name__)

# Process start (monotonic) for the real uptime_s in system_status.
_PROCESS_START = time.monotonic()

# 报表的分组维度。新增一个就要同时给 ``usage_report`` 的 ``key_expr`` 加分支。
USAGE_GROUP_BY = ("day", "model", "user")
# 一次报表最多跨多少天：聚合是扫 ``messages``，给上界才不会让一个手滑的
# 「2000-01-01」把管理后台变成全表扫描。
USAGE_MAX_RANGE_DAYS = 366
USAGE_DEFAULT_DAYS = 30
# 一次请求带回的分组行数上界（``limit`` 查询参数也用它钳位）。
USAGE_MAX_LIMIT = 500
# ``messages.model_name`` 可空（老数据与 system 行），空名归到这个桶里，
# 否则报表末尾会挂一行空白。
UNRECORDED_MODEL_LABEL = "未记录模型"


async def list_users(db: AsyncSession) -> list[User]:
    result = await db.execute(select(User).order_by(User.created_at.desc()))
    return list(result.scalars().all())


async def update_user(
    db: AsyncSession, user_id, *, role: str | None = None, is_active: bool | None = None
) -> User | None:
    user = await db.get(User, user_id)
    if user is None:
        return None
    if role in ("user", "admin"):
        user.role = role
    if is_active is not None:
        if user.is_active and not bool(is_active):
            # Deactivation also invalidates every access token already in the
            # user's hands (belt to the is_active check in get_current_user).
            user.token_version = int(user.token_version or 0) + 1
        user.is_active = bool(is_active)
    await db.commit()
    await db.refresh(user)
    return user


async def usage_stats(db: AsyncSession, days: int = 14) -> list[UsageStat]:
    """最近 ``days`` 个 UTC 自然日的按天用量（仪表盘的紧凑卡片，响应形状不变）。

    与报表共用一条聚合路径：以前这里把整段 Message 行（含 ``content`` TEXT）
    拉进 Python 再累加，数据一长就是几万行；现在 GROUP BY 在 SQL 里做。
    """
    today = datetime.now(UTC).date()
    page = await usage_report(
        db,
        start=today - timedelta(days=max(1, days) - 1),
        end=today,
        group_by="day",
        limit=USAGE_MAX_LIMIT,
    )
    return [
        UsageStat(
            date=row.key,
            messages=row.messages,
            user_messages=row.user_messages,
            assistant_messages=row.requests,
        )
        for row in page.items
    ]


def day_filter(column, start: date | None, end: date | None) -> list:
    """UTC 日历日区间 → 时间列的筛选条件，**两端都含当天**。

    结束日推到次日零点做开区间，而不是「当天 23:59:59」：上界取到秒会漏掉同一
    秒内更晚的值（``messages.created_at`` 是微秒精度）。

    绑定的边界都带 UTC 偏移。Postgres 按 ``timestamptz`` 比较；SQLite 的
    DATETIME 把带偏移的值按它自己的墙上时间落成字符串（取回来是 naive，见
    ``app/core/datetime_utils.py`` 讲的这个方言坑），写入侧同样都是 UTC，所以两侧
    口径一致。唯一残留的方言差异：``server_default=func.now()`` 在 SQLite 上只到
    秒，正好落在 UTC 零点那一瞬的行会被下界判给前一天。
    """
    conds = []
    if start is not None:
        conds.append(column >= datetime(start.year, start.month, start.day, tzinfo=UTC))
    if end is not None:
        conds.append(
            column
            < datetime(end.year, end.month, end.day, tzinfo=UTC) + timedelta(days=1)
        )
    return conds


def _day_expr(db: AsyncSession):
    """把 ``created_at`` 归到 UTC 日历日（字符串 ``YYYY-MM-DD``）。"""
    if _is_sqlite(db):
        return func.strftime("%Y-%m-%d", Message.created_at)
    # ``date_trunc('day', timestamptz)`` 按**会话时区**切日，同一份数据在
    # UTC+8 的库上会给出不同的报表；先用 timezone('UTC', ...) 落成无偏移的时间。
    return func.to_char(func.timezone("UTC", Message.created_at), "YYYY-MM-DD")


def _metrics():
    """一组行的求和项。可空列（tokens / cost_usd）不参与 SUM，全部 coalesce 兜底。"""
    return (
        func.count().label("messages"),
        func.coalesce(
            func.sum(case((Message.role == "user", 1), else_=0)), 0
        ).label("user_messages"),
        func.coalesce(
            func.sum(case((Message.role == "assistant", 1), else_=0)), 0
        ).label("requests"),
        func.coalesce(func.sum(Message.prompt_tokens), 0).label("prompt_tokens"),
        func.coalesce(func.sum(Message.completion_tokens), 0).label("completion_tokens"),
        func.coalesce(func.sum(Message.total_tokens), 0).label("total_tokens"),
        func.coalesce(func.sum(Message.cost_usd), 0.0).label("cost_usd"),
    )


def _row_metrics(row) -> dict:
    return {
        "messages": int(row.messages or 0),
        "user_messages": int(row.user_messages or 0),
        "requests": int(row.requests or 0),
        "prompt_tokens": int(row.prompt_tokens or 0),
        "completion_tokens": int(row.completion_tokens or 0),
        "total_tokens": int(row.total_tokens or 0),
        "cost_usd": float(row.cost_usd or 0.0),
    }


async def usage_report(
    db: AsyncSession,
    *,
    start: date,
    end: date,
    group_by: str = "day",
    limit: int = 100,
    offset: int = 0,
) -> UsageReportPage:
    """按天 / 按模型 / 按用户的用量报表（消息数、请求数、token、成本）。

    全部在 SQL 里 GROUP BY：``messages`` 上有 ``prompt_tokens`` /
    ``total_tokens`` / ``cost_usd``，把它们捞进 Python 再累加等于把报表变成一次
    全表传输。区间口径见 :func:`day_filter`（含两端、按 UTC 日切）。

    ``total`` 是分组数（给翻页用），``totals`` 是**整个区间**的合计而不是这一页
    的合计 —— 否则运营会把「本页成本」读成「本月成本」。
    """
    if group_by not in USAGE_GROUP_BY:
        raise ValueError(f"未知分组维度：{group_by}")

    conds = [
        Message.role.in_(("user", "assistant")),
        *day_filter(Message.created_at, start, end),
    ]
    requests_metric = func.coalesce(
        func.sum(case((Message.role == "assistant", 1), else_=0)), 0
    )

    if group_by == "day":
        key_expr = _day_expr(db)
        # 日期串在每个分组上唯一，时间序即全序，不需要 tie-break。
        order_exprs = [key_expr.asc()]
    elif group_by == "model":
        # coalesce 掉 NULL：既不会出现空白行，分组键也就是全序的最后一环。
        key_expr = func.coalesce(Message.model_name, "")
        order_exprs = [requests_metric.desc(), key_expr.asc()]
    else:
        key_expr = User.id
        order_exprs = [requests_metric.desc(), User.id.asc()]
    group_exprs = [key_expr]

    def base(cols):
        stmt = select(*cols)
        if group_by == "user":
            # 用户维度要落到人：messages 没有 user_id，得经 conversations 一跳。
            stmt = stmt.join(
                Conversation, Message.conversation_id == Conversation.id
            ).join(User, Conversation.user_id == User.id)
        return stmt.where(*conds)

    cols = [key_expr.label("key")]
    if group_by == "user":
        cols += [User.email.label("email"), User.username.label("username")]
    cols += list(_metrics())

    rows = (
        await db.execute(
            base(cols)
            .group_by(*group_exprs)
            .order_by(*order_exprs)
            .offset(max(0, int(offset)))
            .limit(max(1, min(int(limit), USAGE_MAX_LIMIT)))
        )
    ).all()

    totals_row = (await db.execute(base(list(_metrics())))).one()
    grouped = base([key_expr.label("k")]).group_by(*group_exprs).subquery()
    total = (await db.execute(select(func.count()).select_from(grouped))).scalar_one()

    items: list[UsageReportRow] = []
    for row in rows:
        key = str(row.key)
        if group_by == "model":
            label = key or UNRECORDED_MODEL_LABEL
        elif group_by == "user":
            label = str(row.email or row.username or key)
        else:
            label = key
        items.append(
            UsageReportRow(
                key=key,
                label=label,
                email=row.email if group_by == "user" else None,
                username=row.username if group_by == "user" else None,
                **_row_metrics(row),
            )
        )

    return UsageReportPage(
        start=start.isoformat(),
        end=end.isoformat(),
        group_by=group_by,
        items=items,
        total=int(total),
        limit=limit,
        offset=offset,
        totals=UsageMetrics(**_row_metrics(totals_row)),
    )


def _is_sqlite(db: AsyncSession) -> bool:
    return db.bind is not None and db.bind.dialect.name == "sqlite"


async def _count(db: AsyncSession, model) -> int:
    return (await db.execute(select(func.count()).select_from(model))).scalar_one()


async def _ping_db(db: AsyncSession) -> str:
    try:
        from sqlalchemy import text
        await db.execute(text("SELECT 1"))
        return "ok"
    except Exception:
        return "down"


async def _ping_redis() -> str:
    try:
        from redis.asyncio import Redis  # type: ignore

        from app.core.config import get_settings
        client = Redis.from_url(get_settings().REDIS_URL, decode_responses=True)
        await client.ping()
        await client.aclose()
        return "ok"
    except Exception:
        return "down"


async def _ping_qdrant() -> str:
    try:
        from app.rag.qdrant_store import get_vector_store
        store = get_vector_store()
        await store._client.get_collections()
        return "ok"
    except Exception:
        return "down"


async def system_status(db: AsyncSession) -> SystemStatus:
    return SystemStatus(
        db=await _ping_db(db),
        redis=await _ping_redis(),
        qdrant=await _ping_qdrant(),
        users=await _count(db, User),
        conversations=await _count(db, Conversation),
        documents=await _count(db, Document),
        uptime_s=round(time.monotonic() - _PROCESS_START, 1),
    )

