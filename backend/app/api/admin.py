"""Admin router: user management, usage stats, system health.

Every route requires an admin user (``get_current_admin``). User mutations guard the
last admin so the system can't be locked out.
"""
from __future__ import annotations

import uuid
from datetime import date, datetime, timedelta, UTC

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import get_current_admin
from app.core.like import LIKE_ESCAPE, escape_like, like_pattern
from app.db import get_db
from app.models import AuditEvent, User
from app.schemas import (
    AdminUserUpdate,
    AuditEventPage,
    AuditEventRow,
    FeatureFlagPage,
    ToolInfo,
    ToolToggleRequest,
    UsageReportPage,
    UserOut,
)
from app.services import admin_service, audit_service, tool_service, tool_toggles

router = APIRouter(prefix="/api/admin", tags=["admin"])

NOT_FOUND = status.HTTP_404_NOT_FOUND
BAD = status.HTTP_400_BAD_REQUEST

# 审计列表一页最多带回多少条（``limit`` 查询参数的上限，也是导出的单页大小）。
AUDIT_MAX_LIMIT = 500
AUDIT_DEFAULT_LIMIT = 50


@router.get("/agent-runtime")
async def agent_runtime_status(
    admin: User = Depends(get_current_admin),
) -> dict[str, object]:
    """Diagnostics for the multi-agent (CrewAI) runtime, admin-gated.

    Answers, in one place, "why did 专家模式 run a single agent?": whether the
    CrewAI flag is on, whether the package actually imports on THIS host (with
    the concrete import error when not), and which dispatch path
    (inline/durable) the chat API uses — the durable worker path historically
    forced single-agent regardless of the route.
    """
    import asyncio
    import sys

    from app.agents.orchestrator import chat_orchestrator
    from app.core.config import get_settings

    settings = get_settings()
    # The first check may import crewai (seconds, disk-heavy) — run it in a
    # thread so the event loop isn't blocked for other requests.
    available, reason = await asyncio.to_thread(chat_orchestrator._crewai_status)
    return {
        "crewai_enabled": bool(getattr(settings, "CREWAI_ENABLED", False)),
        "crewai_available": available,
        "detail": reason,
        "background_worker": settings.BACKGROUND_WORKER,
        "agent_workflow_engine": settings.AGENT_WORKFLOW_ENGINE,
        "python": sys.version.split()[0],
    }


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


@router.get("/usage", response_model=UsageReportPage)
async def usage_report(
    start: date | None = Query(default=None, description="UTC 日历日，含当天"),
    end: date | None = Query(default=None, description="UTC 日历日，含当天"),
    group_by: str = Query(default="day", description="day | model | user"),
    limit: int = Query(
        default=100, ge=1, le=admin_service.USAGE_MAX_LIMIT
    ),
    offset: int = Query(default=0, ge=0),
    admin: User = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db),
) -> UsageReportPage:
    """用量报表：消息数 / 请求数 / token / 成本，按天、按模型或按用户分组。

    区间是 **UTC 日历日且两端都含当天**，缺省给最近 ``USAGE_DEFAULT_DAYS`` 天。
    ``/api/admin/stats`` 仍然是仪表盘那个形状（按天、近 14 天 + 组件状态），
    报表要的是能换维度、能翻页、能导出的另一份数据，两者共用同一条聚合 SQL 路径。
    """
    if group_by not in admin_service.USAGE_GROUP_BY:
        raise HTTPException(
            BAD,
            f"未知分组维度：{group_by}（可选：{'、'.join(admin_service.USAGE_GROUP_BY)}）",
        )
    resolved_end = end or datetime.now(UTC).date()
    resolved_start = start or (
        resolved_end - timedelta(days=admin_service.USAGE_DEFAULT_DAYS - 1)
    )
    if resolved_start > resolved_end:
        raise HTTPException(BAD, "开始日期不能晚于结束日期")
    if (resolved_end - resolved_start).days + 1 > admin_service.USAGE_MAX_RANGE_DAYS:
        raise HTTPException(
            BAD, f"日期区间最多 {admin_service.USAGE_MAX_RANGE_DAYS} 天"
        )

    return await admin_service.usage_report(
        db,
        start=resolved_start,
        end=resolved_end,
        group_by=group_by,
        limit=limit,
        offset=offset,
    )


@router.get("/feature-flags", response_model=FeatureFlagPage)
async def feature_flags(
    admin: User = Depends(get_current_admin),
) -> FeatureFlagPage:
    """每个运营开关的**生效结论**（条目 34④）。只读。

    回的是结论不是原文：引擎要总开关与灰度名单同时成立，``python_exec`` 在生产要显式
    放行 **且** 真有隔离后端，``/docs`` 在生产默认关与 ``DOCS_ENABLED`` 的默认值无关。
    把 ``.env`` 抄给运营，等于让他们自己重推一遍这些判定式 —— 而推错的方向永远是
    "以为已经开了"。

    不做写：这些值多数在进程启动时读一次（runner 工厂、策略对象都是启动期构造），
    界面上当场改出来的状态和重启后的状态不一致，比不让人改更危险。要按工具粒度的即时
    开关，那是 ``tool_toggles``（后台「工具」页）负责的事。
    """
    from app.core.config import get_settings
    from app.services.feature_flags import effective_flags

    settings = get_settings()
    return FeatureFlagPage(
        generated_at=datetime.now(UTC),
        env=settings.ENV,
        flags=effective_flags(settings),
    )


@router.get("/tools", response_model=list[ToolInfo])
async def admin_tool_catalog(
    admin: User = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db),
) -> list[ToolInfo]:
    """工具目录 + 每个工具的启停状态（条目 34③）。

    与 ``GET /api/tools`` 的差别不只是权限：那一份**不含**已被停用的工具（让用户看见
    一个点上去必然失败的工具，比看不见它更糟），而这一份必须连被关掉的都列出来 ——
    界面上不摆出来，就没有第二个地方能把它重新打开。
    """
    return await tool_service.catalog_for_admin(db)


@router.post("/tools/{tool_name}/toggle", response_model=ToolInfo)
async def toggle_admin_tool(
    tool_name: str,
    payload: ToolToggleRequest,
    admin: User = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db),
) -> ToolInfo:
    """启用 / 停用一个工具，并留下一条能追问的审计。

    名字不认识时是 404 而不是"先把这行存下来"：``tool_name`` 就是主键，接受任意字符串
    等于允许攒出一堆谁也对不上的幽灵开关 —— 工具改名之后最明显，那一行会一直"停用"
    着一个不存在的名字，而真正同名的工具照常执行，看上去却像已经关了。
    """
    name = (tool_name or "").strip()
    catalog = {item.name: item for item in await tool_service.catalog_for_admin(db)}
    if name not in catalog:
        raise HTTPException(NOT_FOUND, f"没有名为 {name!r} 的工具，请从目录里选")

    row = await tool_toggles.set_enabled(
        db,
        tool_name=name,
        enabled=payload.enabled,
        admin_id=admin.id,
        note=payload.note,
    )
    # 审计走自己的会话（best-effort）：这条记录是"谁在什么时候关掉了执行面"的唯一
    # 凭据，但一次审计写入失败不该把已经生效的开关回滚掉。
    await audit_service.log(
        actor_id=admin.id,
        action="tools:toggle",
        target=name,
        detail={"enabled": row.enabled, "note": row.note},
    )
    return catalog[name].model_copy(
        update={"enabled": row.enabled, "toggle_note": row.note}
    )


@router.get("/audit", response_model=AuditEventPage)
async def audit_log(
    action: str | None = Query(default=None, description="精确匹配的 action"),
    action_prefix: str | None = Query(
        default=None, description='action 前缀，如 "credits:"'
    ),
    actor: str | None = Query(
        default=None, description="操作人：用户 id（精确）或邮箱 / 用户名（模糊）"
    ),
    q: str | None = Query(default=None, description="关键字：匹配 target"),
    start: date | None = Query(default=None, description="UTC 日历日，含当天"),
    end: date | None = Query(default=None, description="UTC 日历日，含当天"),
    limit: int = Query(default=AUDIT_DEFAULT_LIMIT, ge=1, le=AUDIT_MAX_LIMIT),
    offset: int = Query(default=0, ge=0),
    admin: User = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db),
) -> AuditEventPage:
    """审计事件（工具调用、审批、登录、积分操作）的筛选 + 分页列表。

    日期区间与用量报表同源（``admin_service.day_filter``：UTC 日历日、两端都含当天）。
    筛选必须在 SQL 里做：一旦分页，前端手上只有这一页，在浏览器里过滤等于
    「只在这一页里找」。
    """
    if start is not None and end is not None and start > end:
        raise HTTPException(BAD, "开始日期不能晚于结束日期")

    conds = list(admin_service.day_filter(AuditEvent.created_at, start, end))
    if action:
        conds.append(AuditEvent.action == action)
    if action_prefix:
        # 前缀匹配 = 转义后的输入 + 一个我们自己加的尾 %（不是子串匹配）。
        conds.append(
            AuditEvent.action.ilike(
                escape_like(action_prefix) + "%", escape=LIKE_ESCAPE
            )
        )
    keyword = (q or "").strip()
    if keyword:
        conds.append(
            AuditEvent.target.ilike(like_pattern(keyword), escape=LIKE_ESCAPE)
        )
    person = (actor or "").strip()
    if person:
        try:
            conds.append(AuditEvent.actor_id == uuid.UUID(person))
        except ValueError:
            pattern = like_pattern(person)
            conds.append(
                or_(
                    User.email.ilike(pattern, escape=LIKE_ESCAPE),
                    User.username.ilike(pattern, escape=LIKE_ESCAPE),
                )
            )

    total = (
        await db.execute(
            select(func.count())
            .select_from(AuditEvent)
            .outerjoin(User, AuditEvent.actor_id == User.id)
            .where(*conds)
        )
    ).scalar_one()

    rows = (
        await db.execute(
            select(AuditEvent, User.email, User.username)
            .outerjoin(User, AuditEvent.actor_id == User.id)
            .where(*conds)
            # ``id`` 只是并列时的定序键：同一瞬间写进多条事件时，没有它的 OFFSET
            # 翻页会看见重复行或漏行（规矩与 ``redeem_service.list_batches`` 一致）。
            .order_by(AuditEvent.created_at.desc(), AuditEvent.id)
            .offset(max(0, int(offset)))
            .limit(max(1, min(int(limit), AUDIT_MAX_LIMIT)))
        )
    ).all()

    return AuditEventPage(
        items=[
            AuditEventRow(
                id=event.id,
                actor_id=event.actor_id,
                action=event.action,
                target=event.target,
                detail=event.detail,
                created_at=event.created_at,
                actor_email=email,
                actor_username=username,
            )
            for event, email, username in rows
        ],
        total=int(total),
        limit=limit,
        offset=offset,
    )
