"""给摄取队列加 ingest_claim_version（僵尸 worker 写回防护）.

Revision ID: 0019_ingest_claim_version
Revises: 0018_prompt_templates
Create Date: 2026-09-20

``ingest_claimed_by`` 记的是「谁」，不是「哪一次」：worker 名在进程重启后会重复，
``ingest_attempts`` 又会被 ``enqueue`` 归零，两条都能被 ABA 绕过 —— 卡死的第一个
worker 醒过来，仍能把它那一轮的结论写回已经被第二个 worker 改过的行上（覆盖对
方已落库的 indexed / failed / 重试计划）。队列需要一个只增不减的凭据，领取时随
那条条件 UPDATE 一起 +1，之后每一次写回都 ``WHERE ingest_claim_version = :claimed``
（见 app/services/ingestion_queue.py）。

只有加列：``server_default='0'`` 同时负责给既有行回填，所以历史行天然可用 —— 它们
的下一次领取会把它推到 1，谓词逻辑不变，也不需要一次修补式的 UPDATE。

与 0011–0018 一样带守卫：空库路径上 ``0000_initial`` 的 ``create_all`` 已按当前
模型把列建好，这里只为比它更老的生产库补建。``--sql`` 离线模式没有可查的库，
所以那条路径直接输出 DDL（离线脚本本来就是给一个已知状态的库跑的）。
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0019_ingest_claim_version"
down_revision: Union[str, None] = "0018_prompt_templates"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_COLUMN = "ingest_claim_version"


def _is_offline() -> bool:
    # 离线（--sql）时 get_bind() 拿到的是「打印用」的假连接，反射查不到任何东西，
    # 所以那条路径无条件出 DDL：离线脚本本来就是针对一个已知状态的库生成的。
    return bool(op.get_context().as_sql)


def _column_exists() -> bool:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if "documents" not in inspector.get_table_names():
        return False
    return _COLUMN in {col["name"] for col in inspector.get_columns("documents")}


def upgrade() -> None:
    if _is_offline() or not _column_exists():
        op.add_column(
            "documents",
            sa.Column(_COLUMN, sa.Integer(), nullable=False, server_default="0"),
        )


def downgrade() -> None:
    if _is_offline() or _column_exists():
        op.drop_column("documents", _COLUMN)
