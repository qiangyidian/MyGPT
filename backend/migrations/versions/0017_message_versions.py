"""消息版本历史表 message_versions（条目 31）。

Revision ID: 0017_message_versions
Revises: 0016_ingestion_queue
Create Date: 2026-09-20

编辑消息会原地覆盖 ``messages.content``，重新生成会直接 ``DELETE`` 上一条 assistant
回复——两件事都在静默销毁用户已经付过 token 的内容。这张表是覆盖/删除**之前**的
append-only 快照，附带当时的 ``metadata``（含引用来源），所以「切回上一版」不需要
重建引用，也不会把新版的答案说成旧版的来源。

两个值得说明的形状选择：

* ``message_id`` **不建外键**。这不是遗漏：regenerate 恰恰会删掉那一行，如果按
  ``ondelete="CASCADE"`` 建 FK，存档动作就等于在同一事务里把档删了。版本靠
  ``conversation_id`` 的级联回收，所以删会话仍然干净。
* 没有 ``updated_at``：版本不可变，改它就不是历史了。裁剪（每条消息最近 20 版、
  整段会话最近 200 版）发生在写入路径里，见
  ``app/services/message_versions.py::prune_versions``。

与 0011–0016 一样带守卫：空库路径上 ``0000_initial`` 的 ``create_all`` 已按当前
模型把表建好，这里只为比它更老的生产库补建。
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0017_message_versions"
down_revision: Union[str, None] = "0016_ingestion_queue"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_TABLE = "message_versions"
_COMPOSITE_INDEX = "ix_message_versions_message_created"


def _has_table(table: str) -> bool:
    return table in sa.inspect(op.get_bind()).get_table_names()


def _existing_indexes(table: str) -> set[str]:
    return {ix["name"] for ix in sa.inspect(op.get_bind()).get_indexes(table)}


def upgrade() -> None:
    if _has_table(_TABLE):
        return
    op.create_table(
        _TABLE,
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        # 无 FK：见模块 docstring —— 它必须活得比自己记录的那一行久。
        sa.Column("message_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "conversation_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("conversations.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("role", sa.String(length=32), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("metadata", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("model_name", sa.String(length=128), nullable=True),
        sa.Column("total_tokens", sa.BigInteger(), nullable=True),
        sa.Column("cost_usd", sa.Float(), nullable=True),
        sa.Column("origin", sa.String(length=16), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_message_versions_message_id", _TABLE, ["message_id"])
    op.create_index("ix_message_versions_conversation_id", _TABLE, ["conversation_id"])
    if _COMPOSITE_INDEX not in _existing_indexes(_TABLE):
        # 列版本按「某条消息 + 时间倒序」取，会话级列表按 conversation_id 走。
        op.create_index(
            _COMPOSITE_INDEX, _TABLE, ["message_id", "created_at"]
        )


def downgrade() -> None:
    if _has_table(_TABLE):
        op.drop_index(_COMPOSITE_INDEX, table_name=_TABLE)
        op.drop_table(_TABLE)
