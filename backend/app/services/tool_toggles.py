"""工具启停：库里的状态 → 策略层能同步读到的一份快照。

分两层是因为**读的一面多、写的一面少**：

* 读的那一面每秒都在发生 —— 每次装配工具清单、每次网关放行都要问"这个工具还开着
  吗"，而那两层都是同步代码（``is_tool_allowed`` / ``ToolRegistry.openai_schemas``）。
  在那里 await 一条 SELECT 等于把整个工具策略层改成异步，代价摊到每一次对话上。
* 写的那一面是运营点一下按钮，一天几次。

所以：库里是唯一真相，进程内留一份**带 TTL 的快照**，同步读快照、异步刷新快照。
默认 15 秒 —— 这个数不是性能调出来的，而是"出事时要停一个工具"能接受多慢：一条
run 通常几秒到几十秒，15 秒内所有活着的服务都会在任何一个带库的调用里重读一次。

**生效范围**：写的那个进程立刻更新（``set_enabled`` 直接在事务后重读），其余进程最
迟在下一次刷新时跟上。收紧执行面的是同步那道检查，不是这份快照的新鲜度。

失败口径：**沿用上一次读到的值**。数据库抖一下不该把被关掉的工具重新放开（那等于
有人刚按下的按钮凭空消失），也不该把所有工具当成关掉（那会让平台假死）。从未读到
过值时是空集，即"没人关过任何工具"，也就是这张表存在之前的行为。
"""
from __future__ import annotations

import logging
import time
import uuid
from collections.abc import Callable, Sequence
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.tool_toggle import ToolToggle

logger = logging.getLogger(__name__)

__all__ = [
    "SNAPSHOT_TTL_SECONDS",
    "clear_toggle",
    "disabled_tools",
    "get_state",
    "is_disabled",
    "list_overrides",
    "refresh",
    "refresh_with",
    "reload_from",
    "set_enabled",
    "snapshot_is_stale",
]

#: 快照的可用时长（秒）。见模块 docstring：这是"停一个工具最慢多久生效"。
SNAPSHOT_TTL_SECONDS = 15.0

# 进程内快照。模块级可变状态在这里是唯一可行的形状：读的那一面是同步的，拿不到
# session。``_loaded_once`` 用来区分"读到过，确实没人关工具"与"还什么都没读过"。
_disabled: frozenset[str] = frozenset()
_states: dict[str, ToolToggle] = {}
_loaded_once = False
_monotonic_at = 0.0


def disabled_tools() -> frozenset[str]:
    """当前快照里被关掉的工具名集合（同步、无 IO）。"""
    return _disabled


def is_disabled(tool_name: str) -> bool:
    """策略层要问的那一句：这个工具现在是不是被运营关掉了。"""
    return tool_name in _disabled


def snapshot_is_stale(now: float | None = None) -> bool:
    """快照是否已过 TTL（刷新点与测试据此判断"这次到底读库了没有"）。"""
    if not _loaded_once:
        return True
    return (time.monotonic() if now is None else now) - _monotonic_at >= SNAPSHOT_TTL_SECONDS


def _store(rows: Sequence[ToolToggle]) -> frozenset[str]:
    """把一批库行写进快照并把时钟归零（唯一的写入口，避免两处各算一份 disabled）。"""
    global _disabled, _states, _loaded_once, _monotonic_at
    _states = {row.tool_name: row for row in rows}
    _disabled = frozenset(name for name, row in _states.items() if not row.enabled)
    _loaded_once = True
    _monotonic_at = time.monotonic()
    return _disabled


async def refresh(
    session_factory: Callable[[], AsyncSession] | None = None,
) -> frozenset[str]:
    """按需重读库里的启停状态，返回最新快照。

    TTL 内的调用直接回快照，所以"每个 run 开始都刷一次"实际是每 15 秒一条 SELECT。
    """
    if _loaded_once and not snapshot_is_stale():
        return _disabled
    factory = session_factory
    if factory is None:
        from app.db import AsyncSessionLocal

        factory = AsyncSessionLocal
    try:
        async with factory() as db:
            rows = (await db.execute(select(ToolToggle))).scalars().all()
    except Exception:
        logger.warning("tool toggle refresh failed; keeping previous state", exc_info=True)
        return _disabled
    return _store(list(rows))


async def list_overrides(db: AsyncSession) -> list[ToolToggle]:
    """运营动过的那些行（后台目录要把状态标出来）。"""
    result = await db.execute(select(ToolToggle).order_by(ToolToggle.tool_name))
    return list(result.scalars().all())


async def get_state(db: AsyncSession, tool_name: str) -> ToolToggle | None:
    result = await db.execute(select(ToolToggle).where(ToolToggle.tool_name == tool_name))
    return result.scalar_one_or_none()


async def set_enabled(
    db: AsyncSession,
    *,
    tool_name: str,
    enabled: bool,
    admin_id: uuid.UUID | None = None,
    note: str | None = None,
) -> ToolToggle:
    """写下一次启停，并在同一进程里立刻让它可见（不等 TTL）。

    ``enabled=True`` 时也留一行：那行是"谁在什么时候把它打开的"的证据。要回到
    "没人动过"是 :func:`clear_toggle` 的职责。
    """
    name = (tool_name or "").strip()
    if not name:
        raise ValueError("tool_name 不能为空")
    row = await get_state(db, name)
    if row is None:
        row = ToolToggle(tool_name=name, enabled=enabled)
        db.add(row)
    row.enabled = enabled
    row.note = (note or "").strip() or None
    row.updated_by = admin_id
    # 逐行 Python 时间戳，与模型里的 default 同一个来源：一次事务里连改两行时
    # server_default 的事务起始时间会让"最后改的是哪个"排不出来。
    row.updated_at = datetime.now(UTC)
    await db.commit()
    await db.refresh(row)
    # 写完就地刷新，不留给调用方选：漏掉这一步的后果是"运营按了停用、界面显示已
    # 停用，而这个进程接下来 15 秒照旧放行"。
    await reload_from(db)
    return row


async def clear_toggle(db: AsyncSession, tool_name: str) -> bool:
    """删掉这一行 = 恢复代码默认（启用）。返回是否真的删掉了东西。"""
    row = await get_state(db, tool_name)
    if row is None:
        return False
    await db.delete(row)
    await db.commit()
    await reload_from(db)
    return True


async def reload_from(db: AsyncSession) -> frozenset[str]:
    """用**当前这个** session 重读快照。

    写完必须马上可见：运营按下"停用"之后，如果自己的进程还要等 15 秒才认账，那
    一次点击在这一秒内就没有任何效果 —— 而他要停的那个工具正在跑。不复用
    :func:`refresh` 是因为它要开自己的 session（会把请求事务那条连接关掉）。
    """
    rows = (await db.execute(select(ToolToggle))).scalars().all()
    return _store(list(rows))


async def refresh_with(db: AsyncSession) -> frozenset[str]:
    """TTL 到了才用调用手上的 session 读一次库。

    这是给「每个 run 开始 / 每轮对话准入」这类**已经在事务里、又拿着 session** 的
    位置用的：在那里问一次开关的代价是每进程每 15 秒一条 SELECT，而不是每条消息
    一条。不新写一个后台轮询任务也是有意的 —— 启停是一个极少变更、又极少被问的值，
    为它单开一个循环只会多一个要监控、要选主、要优雅退出的东西。
    """
    if not snapshot_is_stale():
        return _disabled
    return await reload_from(db)
