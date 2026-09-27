"""管理侧 Agent 运行时观测路由（只读 + 管理员鉴权）。

为什么单独开一个文件而不是扩展 ``/api/agent-runs``：

* 用户侧列表是「我自己这一页会话的运行」，为实时轮询优化（一次带回 steps /
  approvals / tool_calls / graph 全量）；管理员要的是**跨用户翻页**的观测面，
  照搬那个形状会把 50 × 全量 JSON 压在列表接口上。
* 管理员需要的是运行「账」—— 谁的、哪个 profile、花了多少 token / 成本 / 积分、
  跑了多久、卡在哪个门。这些字段在用户侧响应里没有位置（``AgentRun`` 行本身也
  不存 token/成本，它们在 ``AgentStep`` / ``Message`` / ``credit_ledger`` 上）。

本模块只读，不改任何运行状态；写操作仍走 ``/api/agent-runs/{id}/...``（自带
ownership + admin 放行）。任何 key / secret 都不出现在响应里：step 载荷后端落库
前已脱敏（见 ``AgentStep.input_redacted``），tool_calls 由用户侧详情提供。
"""
from __future__ import annotations

import uuid
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import get_current_admin
from app.core.like import LIKE_ESCAPE, like_pattern
from app.db import get_db
from app.models import (
    AgentRun,
    AgentStep,
    Conversation,
    CreditLedger,
    Message,
    RunCommand,
    RunEvent,
    ToolApproval,
    User,
)

router = APIRouter(prefix="/api/admin", tags=["admin-runtime"])

NOT_FOUND = status.HTTP_404_NOT_FOUND

# ``agent_runs.status`` 的取值集合（见 app/models/agent_run.py 的注释）。
# 列表筛选按它白名单校验，避免把任意字符串拼进 SQL 条件里当作「有结果」的假象。
RUN_STATUSES = (
    "pending",
    "running",
    "waiting_approval",
    "paused",
    "completed",
    "failed",
    "cancelled",
)


# --------------------------------------------------------------------------- #
# 响应模型（本模块自带：管理侧观测面是这一个文件的事，不进 app/schemas 公共面）
# --------------------------------------------------------------------------- #
class AdminAgentRunRow(BaseModel):
    """列表里的一行运行 —— 跨用户观测所需的最小字段集。"""

    id: uuid.UUID
    conversation_id: uuid.UUID
    conversation_title: str | None = None
    user_id: uuid.UUID | None = None
    user_email: str | None = None
    user_username: str | None = None
    runtime: str
    flow_name: str
    status: str
    current_step: str
    created_at: datetime
    started_at: datetime | None = None
    finished_at: datetime | None = None
    duration_ms: int | None = None
    error_message: str | None = None
    # ---- 计划 / 门状态 ----
    plan_status: str = ""
    plan_present: bool = False
    paused_at: datetime | None = None
    gate_armed: bool = False
    # ---- 账 ----
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    cost_usd: float | None = None
    credits_consumed: int = 0
    step_count: int = 0
    pending_approvals: int = 0


class AdminAgentRunPage(BaseModel):
    items: list[AdminAgentRunRow]
    total: int
    limit: int
    offset: int


class AdminRunCommandRow(BaseModel):
    """一条持久控制命令（pause/resume/cancel/gate/approve/instruction）。"""

    id: uuid.UUID
    command_type: str
    payload: dict
    status: str
    created_at: datetime
    applied_at: datetime | None = None
    error: str | None = None


class AdminRunEventRow(BaseModel):
    """一条持久运行事件（``run_events`` 表，非 SSE）。"""

    id: uuid.UUID
    sequence: int
    event_type: str
    data: dict
    created_at: datetime


class AdminRunEventPage(BaseModel):
    items: list[AdminRunEventRow]
    total: int
    limit: int
    offset: int


class AdminRunSummary(BaseModel):
    """运行顶部的一张账卡：状态计数 + 当日 token / 成本汇总。"""

    total_runs: int
    running: int
    waiting_approval: int
    failed: int
    prompt_tokens: int
    completion_tokens: int
    cost_usd: float
    # 本窗口内已终态运行的平均耗时（毫秒）；无样本时 None。
    avg_duration_ms: int | None = None


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
def _duration_ms(run: AgentRun) -> int | None:
    """已知的运行时长：started→finished，其次 started→现在（未终态的活运行）。"""
    if run.started_at is None:
        return None
    end = run.finished_at
    if end is None:
        if run.status in ("completed", "failed", "cancelled"):
            # 终态但没写 finished_at（老数据 / 崩溃窗口）：用创建时间兜底，
            # 宁可给出偏大的值也不隐藏一次失败。
            end = run.created_at
        else:
            # 仍在跑：给出「已经跑了多久」。与 started_at 同时区，减法安全。
            end = datetime.now(run.started_at.tzinfo)
    delta = (end - run.started_at).total_seconds() * 1000
    return int(delta) if delta >= 0 else None


async def _gate_state(db: AsyncSession, run_ids: list[uuid.UUID]) -> dict[uuid.UUID, bool]:
    """每个 run 最后一个 ``gate`` 命令的 enabled —— 计划门当前是否上着。

    门状态只在进程内的 ``RunControl.gate_requested`` 上，库里唯一的事实来源就
    是这条持久命令队列（见 run_environment 的 drain：重放 gate 命令恢复旗标）。
    """
    if not run_ids:
        return {}
    rows = (
        await db.execute(
            select(RunCommand.run_id, RunCommand.payload, RunCommand.created_at)
            .where(RunCommand.run_id.in_(run_ids), RunCommand.command_type == "gate")
            .order_by(RunCommand.created_at.asc())
        )
    ).all()
    armed: dict[uuid.UUID, bool] = {}
    for run_id, payload, _created in rows:
        armed[run_id] = bool((payload or {}).get("enabled"))
    return armed


def _gate_display(run: AgentRun, armed: bool) -> bool:
    """列表里展示的闸状态：计划已经决定过（确认 / 改过）就不再显示「等着确认」。

    引擎在收到 confirmed/updated 后自己清进程内的闸旗标，但不会回写一条
    ``enabled=false`` 的 gate 命令 —— 队列里最后一条仍然是「上闸」。展示层必须
    按 ``plan_status`` 收口，否则一个早就跑完的深度研究会一直挂着「等待确认」。
    """
    if not armed:
        return False
    return (run.plan_status or "") not in ("confirmed", "updated")


def _messages_keyed(rows) -> dict[uuid.UUID, Message]:
    out: dict[uuid.UUID, Message] = {}
    for m in rows:
        out[m.id] = m
    return out


# --------------------------------------------------------------------------- #
# 列表
# --------------------------------------------------------------------------- #
@router.get("/agent-runs", response_model=AdminAgentRunPage)
async def list_agent_runs(
    run_status: str | None = Query(default=None, alias="status"),
    flow_name: str | None = Query(default=None),
    runtime: str | None = Query(default=None),
    q: str | None = Query(default=None, description="按用户邮箱 / 用户名模糊匹配"),
    conversation_id: uuid.UUID | None = Query(default=None),
    limit: int = Query(default=25, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    admin: User = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db),
) -> AdminAgentRunPage:
    """跨用户的运行列表（管理员观测入口）。服务端分页 + 状态/profile 过滤。"""
    conds = []
    if run_status:
        # 白名单外的状态直接 400：让运营看见「筛错了」而不是「没有结果」。
        if run_status not in RUN_STATUSES:
            raise HTTPException(
                status.HTTP_400_BAD_REQUEST,
                f"未知状态：{run_status}（可选：{'、'.join(RUN_STATUSES)}）",
            )
        conds.append(AgentRun.status == run_status)
    if flow_name:
        conds.append(AgentRun.flow_name == flow_name)
    if runtime:
        conds.append(AgentRun.runtime == runtime)
    if conversation_id is not None:
        conds.append(AgentRun.conversation_id == conversation_id)
    if q and q.strip():
        like = like_pattern(q.strip())
        conds.append(
            or_(
                User.email.ilike(like, escape=LIKE_ESCAPE),
                User.username.ilike(like, escape=LIKE_ESCAPE),
            )
        )

    base = select(AgentRun).outerjoin(User, AgentRun.user_id == User.id)
    count_stmt = (
        select(func.count()).select_from(AgentRun).outerjoin(User, AgentRun.user_id == User.id)
    )
    for c in conds:
        base = base.where(c)
        count_stmt = count_stmt.where(c)

    total = (await db.execute(count_stmt)).scalar_one()

    runs = list(
        (
            await db.execute(
                base.order_by(AgentRun.created_at.desc()).limit(limit).offset(offset)
            )
        )
        .scalars()
        .all()
    )
    if not runs:
        return AdminAgentRunPage(items=[], total=int(total), limit=limit, offset=offset)

    run_ids = [r.id for r in runs]
    msg_ids = [r.message_id for r in runs if r.message_id is not None]
    conv_ids = [r.conversation_id for r in runs]
    user_ids = [r.user_id for r in runs if r.user_id is not None]

    # 批量聚合，绝不在循环里发查询（一页 25 行 × 4 张表会打满连接池）。
    step_agg: dict[uuid.UUID, tuple[int, int, int]] = {}
    for run_id, p, c, n in (
        await db.execute(
            select(
                AgentStep.run_id,
                func.coalesce(func.sum(AgentStep.prompt_tokens), 0),
                func.coalesce(func.sum(AgentStep.completion_tokens), 0),
                func.count(AgentStep.id),
            )
            .where(AgentStep.run_id.in_(run_ids))
            .group_by(AgentStep.run_id)
        )
    ).all():
        step_agg[run_id] = (int(p or 0), int(c or 0), int(n or 0))

    messages = _messages_keyed(
        (
            await db.execute(select(Message).where(Message.id.in_(msg_ids)))
        ).scalars().all()
        if msg_ids
        else []
    )
    credits_by_msg: dict[str, int] = {}
    if msg_ids:
        for ref_id, delta in (
            await db.execute(
                select(CreditLedger.ref_id, CreditLedger.delta)
                .where(
                    CreditLedger.ref_type == "message",
                    CreditLedger.reason == "usage",
                    CreditLedger.ref_id.in_([str(m) for m in msg_ids]),
                )
            )
        ).all():
            if ref_id:
                # 消耗是负 delta；同一 message 只会有一行（部分唯一索引）。
                credits_by_msg[ref_id] = credits_by_msg.get(ref_id, 0) + max(0, -int(delta))

    pending_by_run: dict[uuid.UUID, int] = {}
    for run_id, cnt in (
        await db.execute(
            select(ToolApproval.run_id, func.count(ToolApproval.id))
            .where(ToolApproval.run_id.in_(run_ids), ToolApproval.status == "pending")
            .group_by(ToolApproval.run_id)
        )
    ).all():
        pending_by_run[run_id] = int(cnt)

    user_rows = (
        (await db.execute(select(User).where(User.id.in_(user_ids)))).scalars().all()
        if user_ids
        else []
    )
    users = {u.id: u for u in user_rows}
    conv_rows = (
        (
            await db.execute(select(Conversation).where(Conversation.id.in_(conv_ids)))
        )
        .scalars()
        .all()
        if conv_ids
        else []
    )
    convs = {c.id: c for c in conv_rows}
    gates = await _gate_state(db, run_ids)

    items: list[AdminAgentRunRow] = []
    for r in runs:
        sp, sc, sn = step_agg.get(r.id, (0, 0, 0))
        msg = messages.get(r.message_id) if r.message_id is not None else None
        # token 终账以 provider 回报的 message 数为准（含规划器/verifier 等辅助
        # 调用），step 聚合只覆盖逐步骤部分；取两者较大值，宁可高估不低估。
        p_tokens = max(sp, int(msg.prompt_tokens or 0)) if msg else sp
        c_tokens = max(sc, int(msg.completion_tokens or 0)) if msg else sc
        user = users.get(r.user_id) if r.user_id is not None else None
        conv = convs.get(r.conversation_id)
        items.append(
            AdminAgentRunRow(
                id=r.id,
                conversation_id=r.conversation_id,
                conversation_title=(conv.title if conv else None),
                user_id=r.user_id,
                user_email=(user.email if user else None),
                user_username=(user.username if user else None),
                runtime=r.runtime,
                flow_name=r.flow_name,
                status=r.status,
                current_step=r.current_step,
                created_at=r.created_at,
                started_at=r.started_at,
                finished_at=r.finished_at,
                duration_ms=_duration_ms(r),
                error_message=r.error_message,
                plan_status=r.plan_status or "",
                plan_present=bool(r.plan),
                paused_at=r.paused_at,
                gate_armed=_gate_display(r, gates.get(r.id, False)),
                prompt_tokens=p_tokens,
                completion_tokens=c_tokens,
                total_tokens=int(msg.total_tokens or 0) if msg else p_tokens + c_tokens,
                cost_usd=(float(msg.cost_usd) if msg and msg.cost_usd is not None else None),
                credits_consumed=credits_by_msg.get(str(r.message_id), 0)
                if r.message_id is not None
                else 0,
                step_count=sn,
                pending_approvals=pending_by_run.get(r.id, 0),
            )
        )

    return AdminAgentRunPage(items=items, total=int(total), limit=limit, offset=offset)


@router.get("/agent-runs/summary", response_model=AdminRunSummary)
async def agent_runs_summary(
    since: datetime | None = Query(default=None, description="统计窗口起点（默认近 24h）"),
    admin: User = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db),
) -> AdminRunSummary:
    """观测面板顶部的汇总卡：状态计数 + 窗口内 token / 成本 / 平均耗时。"""
    from datetime import UTC, timedelta

    start = since or (datetime.now(UTC) - timedelta(hours=24))

    counts = dict(
        (
            await db.execute(
                select(AgentRun.status, func.count())
                .where(AgentRun.created_at >= start)
                .group_by(AgentRun.status)
            )
        ).all()
    )
    total_runs = int(sum(counts.values()))
    token_row = (
        await db.execute(
            select(
                func.coalesce(func.sum(AgentStep.prompt_tokens), 0),
                func.coalesce(func.sum(AgentStep.completion_tokens), 0),
            ).where(AgentStep.created_at >= start)
        )
    ).one()
    cost = (
        await db.execute(
            select(func.coalesce(func.sum(Message.cost_usd), 0.0))
            .where(Message.created_at >= start)
        )
    ).scalar_one()
    dur = (
        await db.execute(
            select(
                func.avg(
                    func.extract("epoch", AgentRun.finished_at - AgentRun.started_at) * 1000
                )
            ).where(
                AgentRun.created_at >= start,
                AgentRun.started_at.is_not(None),
                AgentRun.finished_at.is_not(None),
            )
        )
    ).scalar_one()

    return AdminRunSummary(
        total_runs=total_runs,
        running=int(counts.get("running", 0)) + int(counts.get("pending", 0)),
        waiting_approval=int(counts.get("waiting_approval", 0)),
        failed=int(counts.get("failed", 0)),
        prompt_tokens=int(token_row[0] or 0),
        completion_tokens=int(token_row[1] or 0),
        cost_usd=float(cost or 0.0),
        avg_duration_ms=int(float(dur)) if dur is not None else None,
    )


# --------------------------------------------------------------------------- #
# 详情附属：持久命令队列 + 事件表
# --------------------------------------------------------------------------- #
async def _run_or_404(db: AsyncSession, run_id: uuid.UUID) -> AgentRun:
    run = await db.get(AgentRun, run_id)
    if run is None:
        raise HTTPException(NOT_FOUND, "运行不存在")
    return run


@router.get("/agent-runs/{run_id}/commands", response_model=list[AdminRunCommandRow])
async def list_run_commands(
    run_id: uuid.UUID,
    limit: int = Query(default=100, ge=1, le=500),
    admin: User = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db),
) -> list[AdminRunCommandRow]:
    """这个运行收下的持久控制命令（含计划门上闸/撤闸、审批、取消）。"""
    await _run_or_404(db, run_id)
    rows = (
        await db.execute(
            select(RunCommand)
            .where(RunCommand.run_id == run_id)
            .order_by(RunCommand.created_at.asc())
            .limit(limit)
        )
    ).scalars().all()
    return [
        AdminRunCommandRow(
            id=c.id,
            command_type=c.command_type,
            payload=dict(c.payload or {}),
            status=c.status,
            created_at=c.created_at,
            applied_at=c.applied_at,
            error=c.error,
        )
        for c in rows
    ]


@router.get("/agent-runs/{run_id}/events", response_model=AdminRunEventPage)
async def list_run_events(
    run_id: uuid.UUID,
    limit: int = Query(default=200, ge=1, le=1000),
    offset: int = Query(default=0, ge=0),
    admin: User = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db),
) -> AdminRunEventPage:
    """持久事件表的分页视图（审计用；实时跟随仍走 /events SSE）。"""
    await _run_or_404(db, run_id)
    total = (
        await db.execute(
            select(func.count()).select_from(RunEvent).where(RunEvent.run_id == run_id)
        )
    ).scalar_one()
    rows = (
        await db.execute(
            select(RunEvent)
            .where(RunEvent.run_id == run_id)
            .order_by(RunEvent.sequence.asc())
            .limit(limit)
            .offset(offset)
        )
    ).scalars().all()
    return AdminRunEventPage(
        items=[
            AdminRunEventRow(
                id=r.id,
                sequence=r.sequence,
                event_type=r.event_type,
                data=dict(r.data or {}),
                created_at=r.created_at,
            )
            for r in rows
        ],
        total=int(total),
        limit=limit,
        offset=offset,
    )
