"""工具启停状态（条目 34③）：运营关掉某个工具时留下的那一行。

**只有被动过的工具才有行**，缺行 = 按代码默认（启用）。理由见迁移
``0021_tool_toggles`` 的 docstring：「恢复默认」于是就是删一行，而不是让运营记住
每个工具的开关键；表的大小也等于「被改过的工具数」。

为什么把状态放库里而不是环境变量：API 与 worker 是两个进程（k8s 上是多副本），
env 只在启动时读一次，改 env 要重启，而"立刻停掉一个正在闯祸的工具"恰恰最不能等
一次发版。
"""
from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, String, Text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base

__all__ = ["ToolToggle"]


class ToolToggle(Base):
    __tablename__ = "tool_toggles"

    # 主键是工具名本身（不是 UUID）：一个工具一条状态，upsert 天然幂等；而且 dump
    # 里直接读得懂"被关掉的是哪个"。名字来自注册表，改名就等于换了一个工具。
    tool_name: Mapped[str] = mapped_column(String(64), primary_key=True)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    # 关掉/打开的理由。可空，但界面会一直追问：一周后没人记得当初为什么停，
    # "重新打开"就成了一次无人负责的赌博。
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    # ON DELETE SET NULL：注销账号不该顺手抹掉这条运营证据 —— 它记的是平台发生过
    # 什么，不是那个人的资产（与 audit_events.actor_id 同一套取舍）。
    updated_by: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    )
    # 逐行 Python 时间戳：一次事务里 upsert 多行时 ``func.now()`` 拿到的是事务起始
    # 时间，"谁最后改的"就排不出来。
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        nullable=False,
    )
