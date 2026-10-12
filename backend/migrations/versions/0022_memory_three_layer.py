"""三层记忆：新表 + 新列 + 块键回填.

Revision ID: 0022_memory_three_layer
Revises: 0021_tool_toggles
Create Date: 2026-10-12

纯 additive：对旧代码零影响，所以后面几个阶段只"启用"这些列，不再产生迁移。唯一
需要动数据的是 ``conversation_memories.block_key`` 回填 —— ``state_store`` 一直在写
``memory_type IN ('task','summary')``，不回填的话 S5 按 block_key 读取时，正在进行
的会话会看不见自己的目标与摘要。

回填按 ``(conversation_id, memory_type)`` 取**最新一条**：历史数据里同一会话可能存在
两行 task（``upsert_goal`` 用的 ``scalar_one_or_none`` 挡不住并发），全量回填会直接
撞新加的唯一键。多余的行保持 NULL —— 它们本来就没有读者。
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import context, op
from sqlalchemy.dialects import postgresql

revision: str = "0022_memory_three_layer"
down_revision: Union[str, None] = "0021_tool_toggles"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _has_table(table: str) -> bool:
    # 离线模式没有可反射的连接；它的输入明确是 prior revision，直接发本版 DDL。
    if context.is_offline_mode():
        return False
    return table in sa.inspect(op.get_bind()).get_table_names()


def _has_column(table: str, column: str) -> bool:
    if context.is_offline_mode():
        return False
    return any(c["name"] == column for c in sa.inspect(op.get_bind()).get_columns(table))


def upgrade() -> None:
    # ---- user_memories：治理与双时态 ----
    if not _has_column("user_memories", "source"):
        op.add_column(
            "user_memories",
            sa.Column("source", sa.String(length=16), nullable=False, server_default="inferred"),
        )
        op.add_column(
            "user_memories",
            sa.Column("author", sa.String(length=16), nullable=False, server_default="extractor"),
        )
        op.add_column(
            "user_memories",
            sa.Column("importance", sa.Float(), nullable=False, server_default="0.5"),
        )
        op.add_column(
            "user_memories",
            sa.Column(
                "valid_from", sa.DateTime(timezone=True), nullable=False,
                server_default=sa.func.now(),
            ),
        )
        op.add_column(
            "user_memories", sa.Column("invalid_at", sa.DateTime(timezone=True), nullable=True)
        )
        op.add_column(
            "user_memories",
            sa.Column("superseded_by", postgresql.UUID(as_uuid=True), nullable=True),
        )
        op.add_column(
            "user_memories",
            sa.Column("use_count", sa.Integer(), nullable=False, server_default="0"),
        )
        op.add_column(
            "user_memories", sa.Column("last_used_at", sa.DateTime(timezone=True), nullable=True)
        )
        op.create_foreign_key(
            "fk_user_memories_superseded_by", "user_memories", "user_memories",
            ["superseded_by"], ["id"], ondelete="SET NULL",
        )
        op.create_index("ix_user_memories_source", "user_memories", ["source"])
        op.create_index("ix_user_memories_user_invalid", "user_memories", ["user_id", "invalid_at"])
        op.create_index(
            "ix_user_memories_user_superseded", "user_memories", ["user_id", "superseded_by"]
        )

    # ---- conversation_memories：块化 ----
    if not _has_column("conversation_memories", "block_key"):
        op.add_column(
            "conversation_memories", sa.Column("block_key", sa.String(length=32), nullable=True)
        )
        op.add_column(
            "conversation_memories",
            sa.Column("author", sa.String(length=16), nullable=False, server_default="system"),
        )
        op.add_column(
            "conversation_memories",
            sa.Column("revision", sa.Integer(), nullable=False, server_default="1"),
        )
        op.create_unique_constraint(
            "uq_conversation_memories_block", "conversation_memories",
            ["conversation_id", "block_key"],
        )
        op.execute(
            """
            UPDATE conversation_memories m
               SET block_key = CASE m.memory_type
                                 WHEN 'task' THEN 'goal'
                                 WHEN 'summary' THEN 'summary'
                               END
             WHERE m.memory_type IN ('task', 'summary')
               AND m.id IN (
                   SELECT DISTINCT ON (conversation_id, memory_type) id
                     FROM conversation_memories
                    WHERE memory_type IN ('task', 'summary')
                    ORDER BY conversation_id, memory_type, updated_at DESC
               )
            """
        )

    # ---- memory_events ----
    if not _has_table("memory_events"):
        op.create_table(
            "memory_events",
            sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
            sa.Column(
                "user_id", postgresql.UUID(as_uuid=True),
                sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True,
            ),
            sa.Column(
                "conversation_id", postgresql.UUID(as_uuid=True),
                sa.ForeignKey("conversations.id", ondelete="CASCADE"), nullable=True,
            ),
            sa.Column(
                "message_id", postgresql.UUID(as_uuid=True),
                sa.ForeignKey("messages.id", ondelete="SET NULL"), nullable=True,
            ),
            sa.Column("kind", sa.String(length=24), nullable=False),
            sa.Column(
                "memory_id", postgresql.UUID(as_uuid=True),
                sa.ForeignKey("user_memories.id", ondelete="SET NULL"), nullable=True,
            ),
            sa.Column("payload", postgresql.JSONB(), nullable=False, server_default="{}"),
            # 幂等锚点（见 models/memory_event.py 的说明：不用 partial index，因为测试库是
            # SQLite，postgresql_where 会被忽略而 unique 仍然生效）。
            sa.Column("dedup_key", sa.String(length=96), nullable=True),
            sa.Column(
                "created_at", sa.DateTime(timezone=True), nullable=False,
                server_default=sa.func.now(),
            ),
            sa.Column(
                "updated_at", sa.DateTime(timezone=True), nullable=False,
                server_default=sa.func.now(),
            ),
        )
        op.create_index("ix_memory_events_user_id", "memory_events", ["user_id"])
        op.create_index("ix_memory_events_conversation_id", "memory_events", ["conversation_id"])
        op.create_index("uq_memory_events_dedup", "memory_events", ["dedup_key"], unique=True)

    # ---- memory_settings ----
    if not _has_table("memory_settings"):
        op.create_table(
            "memory_settings",
            sa.Column(
                "user_id", postgresql.UUID(as_uuid=True),
                sa.ForeignKey("users.id", ondelete="CASCADE"), primary_key=True,
            ),
            sa.Column("facts_enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
            sa.Column("working_enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
            sa.Column("episodes_enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
            sa.Column(
                "created_at", sa.DateTime(timezone=True), nullable=False,
                server_default=sa.func.now(),
            ),
            sa.Column(
                "updated_at", sa.DateTime(timezone=True), nullable=False,
                server_default=sa.func.now(),
            ),
        )


def downgrade() -> None:
    """回退会丢掉已回填的 block_key 与整张 memory_events。

    memory_events 是审计面（"它为什么把这条偏好改错了"的唯一证据），删它不该是一次
    顺手操作 —— 真实部署里跑这一步必须是人决定的。
    """
    if _has_table("memory_settings"):
        op.drop_table("memory_settings")
    if _has_table("memory_events"):
        op.drop_index("uq_memory_events_dedup", table_name="memory_events")
        op.drop_index("ix_memory_events_conversation_id", table_name="memory_events")
        op.drop_index("ix_memory_events_user_id", table_name="memory_events")
        op.drop_table("memory_events")
    if _has_column("conversation_memories", "block_key"):
        op.drop_constraint(
            "uq_conversation_memories_block", "conversation_memories", type_="unique"
        )
        op.drop_column("conversation_memories", "revision")
        op.drop_column("conversation_memories", "author")
        op.drop_column("conversation_memories", "block_key")
    if _has_column("user_memories", "source"):
        op.drop_index("ix_user_memories_user_superseded", table_name="user_memories")
        op.drop_index("ix_user_memories_user_invalid", table_name="user_memories")
        op.drop_index("ix_user_memories_source", table_name="user_memories")
        op.drop_constraint("fk_user_memories_superseded_by", "user_memories", type_="foreignkey")
        for col in (
            "last_used_at", "use_count", "superseded_by", "invalid_at",
            "valid_from", "importance", "author", "source",
        ):
            op.drop_column("user_memories", col)
