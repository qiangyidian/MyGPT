"""提示词库（prompt / template library）。

一行 = 一个可复用的提示词模板。``user_id IS NULL`` 表示系统预置模板，与
``ModelConfig`` 的 system-wide 行同一套约定：所有人可读，只有管理员可改。预置
内容由迁移 ``0018_prompt_templates`` 灌进数据库（不是代码里的常量），所以改一句
预置提示词只要改数据，不需要发版。

``content`` 里可以带 ``${var}`` / ``{{var}}`` 占位符。占位符的识别与插入位置由
前端负责（``frontend/src/lib/prompt-library.ts``，有单测），服务端刻意不做替换：
把用户填的变量值直接拼进模板再发给模型，等于把「变量」变成一条注入通道，而模板
的价值恰恰是让用户看见将要发出去的确切文本。
"""
from __future__ import annotations

import uuid

from sqlalchemy import ForeignKey, Index, Integer, String, Text, text
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base
from app.models._mixins import TimestampMixin

DEFAULT_CATEGORY = "通用"


class PromptTemplate(Base, TimestampMixin):
    __tablename__ = "prompt_templates"

    __table_args__ = (
        Index(
            "uq_prompt_templates_system_title",
            "title",
            unique=True,
            # 预置模板标题唯一（迁移 0017 靠它保证重复执行不灌出两份），用户自己
            # 的模板允许重名。两个方言都得给 where，否则测试库（SQLite）根本不
            # 强制这个约束，守卫就成了摆设。
            postgresql_where=text("user_id IS NULL"),
            sqlite_where=text("user_id IS NULL"),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    # NULL = 系统预置模板（人人可读，仅管理员可写）。索引与 ModelConfig 一致：
    # 列表查询永远是「我的 + 预置」这两种归属。
    user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=True, index=True
    )
    title: Mapped[str] = mapped_column(String(128), nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    # server_default：0017 在旧库上是建表语句，NOT NULL 列没有默认值就无法为
    # 「不写该列」的插入路径服务（也为了让 create_all 与迁移两条路完全同构）。
    category: Mapped[str] = mapped_column(
        String(32),
        default=DEFAULT_CATEGORY,
        server_default=DEFAULT_CATEGORY,
        nullable=False,
        index=True,
    )
    # 自由标签。用 JSONB 而不是关联表：标签只在展示层聚合/筛选，从不按标签做
    # 关系查询，建表的收益抵不上多一次 join。
    tags: Mapped[list | None] = mapped_column(JSONB, nullable=True)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    # 预置模板之间的全局展示顺序（见迁移 0017 的 _PRESETS）；用户自己的模板用不到，
    # 恒为 0，列表按 updated_at 排。
    sort_order: Mapped[int] = mapped_column(
        Integer, default=0, server_default="0", nullable=False
    )
