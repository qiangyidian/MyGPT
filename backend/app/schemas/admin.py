from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel


class AdminUserUpdate(BaseModel):
    role: str | None = None
    is_active: bool | None = None


class UsageStat(BaseModel):
    date: str
    conversations: int = 0
    messages: int = 0
    user_messages: int = 0
    assistant_messages: int = 0
    tool_calls: int = 0


class SystemStatus(BaseModel):
    db: str = "unknown"
    redis: str = "unknown"
    qdrant: str = "unknown"
    users: int = 0
    conversations: int = 0
    documents: int = 0
    uptime_s: float = 0.0


class AuditLogOut(BaseModel):
    id: uuid.UUID
    actor_id: uuid.UUID | None
    action: str
    target: str | None
    detail: dict | None = None
    created_at: datetime


class AuditEventRow(AuditLogOut):
    """审计行 + 操作人身份。``actor_id`` 是 UUID，运营看不懂，所以带出邮箱与用户名。"""

    actor_email: str | None = None
    actor_username: str | None = None


class AuditEventPage(BaseModel):
    items: list[AuditEventRow]
    total: int
    limit: int
    offset: int


class UsageMetrics(BaseModel):
    """一组用量求和项。

    ``requests`` 数的是 ``role='assistant'`` 的行：一次模型请求落一行回答，
    provider 的 usage（token 与 ``cost_usd``）只写在这些行上，所以「请求数」是它，
    而不是把所有消息行一起数。``messages`` 连用户提问一起算。
    """

    messages: int = 0
    user_messages: int = 0
    requests: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    cost_usd: float = 0.0


class UsageReportRow(UsageMetrics):
    """报表的一行。``key`` / ``label`` 的含义随 ``group_by`` 变化：按天是日期，
    按模型是模型名（没有模型名的行归到「未记录模型」），按用户是用户 id。
    """

    key: str
    label: str
    email: str | None = None
    username: str | None = None


class UsageReportPage(BaseModel):
    # 实际生效的区间（含两端），由服务端把缺省值补齐后回显。
    start: str
    end: str
    group_by: str
    items: list[UsageReportRow]
    # 分组总数（不是消息数），给翻页用。
    total: int
    limit: int
    offset: int
    # 整个区间的合计，与这一页装了哪几行无关。
    totals: UsageMetrics


class FeatureFlagOut(BaseModel):
    """一个运营开关的**生效结论**，不是环境变量的原文。

    ``enabled`` 与 ``value`` 是"照现在这套配置，平台实际会怎么做"，``source`` 是"想
    改它去改哪一项"。这两件事必须分开回显：好几个开关是"看着开了、其实没开"的形态
    （引擎要总开关与 profile 名单同时成立；``python_exec`` 在生产要显式放行**且**真有
    隔离后端），只把 env 原文抄给运营，等于让他们自己去重新推导一遍判定式。
    """

    key: str
    label: str
    # 归组用（engine / billing / tools / rag / access / exposure），界面按组渲染。
    group: str
    enabled: bool
    # 展示值：布尔开关是 "true"/"false"，名单类是逗号串，路径类是路径本身。
    value: str
    # 改哪儿：一个或多个环境变量名（判定式跨多个变量时全部列出，否则运营只改一个
    # 就以为改完了）。
    source: str
    # 一句话讲清它管到什么、以及那个"看着开了其实没开"的坑。
    note: str


class FeatureFlagPage(BaseModel):
    """全量开关 + 这份结论是哪个进程在什么时候算的。

    带上时间戳与 ENV 是因为这些值来自**本进程启动时读到的配置**：多副本滚动发版期间
    两个副本可以给出不同答案，没有这一行就会有人拿一张截图去争论配置对不对。
    """

    generated_at: datetime
    env: str
    flags: list[FeatureFlagOut]
