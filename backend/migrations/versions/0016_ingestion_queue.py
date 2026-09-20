"""文档摄取改成持久队列：租约 / 次数 / 退避.

Revision ID: 0016_ingestion_queue
Revises: 0015_rag_provenance_and_settings
Create Date: 2026-09-20

``Document`` 这一行本身就是队列条目（见 app/services/ingestion_queue.py 顶部为
什么不另建表），所以这里加的只是调度元数据：

* ``ingest_lease_expires_at`` / ``ingest_claimed_by``：谁在做、做到什么时候。
  崩溃接管从「等 10 分钟然后可能和还活着的进程撞车」变成「租约到期即可被接手，
  而丢掉租约的那个进程事后写不回结果」（``finish`` 按 owner 判定）。
* ``ingest_attempts`` / ``ingest_next_retry_at``：坏文件不再被无限重排；失败按
  指数退避排到将来某一时刻，而不是立刻再试一遍。

全是加列/加索引，没有回填：既有行的 ``ingest_lease_expires_at`` 为 NULL，claim
谓词把 NULL 视为「没被人拿住」，所以历史上卡在 pending/parsing 的文档会被直接
接管，不需要一次修补。``ingest_attempts`` 用 server_default 而不是回填，也是
为这个目的。

与 0011/0012/0014/0015 一样带守卫：这几张表由 ``0000_initial`` 的
``Base.metadata.create_all`` 按「当前模型」建出来，空库路径上列和索引天生就在，
只有比 0000 更老的生产库才需要真的补。
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0016_ingestion_queue"
down_revision: Union[str, None] = "0015_rag_provenance_and_settings"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_STATUS_INDEX = "ix_documents_status"

# Mirrors the model exactly (``nullable`` included): a drifted column would make
# the empty-DB path (create_all from the model) and a migrated production DB two
# different schemas under the same alembic revision.
_DOCUMENT_COLUMNS: tuple[tuple[str, sa.types.TypeEngine, str | None, bool], ...] = (
    ("ingest_attempts", sa.Integer(), "0", False),
    ("ingest_next_retry_at", sa.DateTime(timezone=True), None, True),
    ("ingest_lease_expires_at", sa.DateTime(timezone=True), None, True),
    ("ingest_claimed_by", sa.String(length=128), None, True),
)


def _tables() -> set[str]:
    return set(sa.inspect(op.get_bind()).get_table_names())


def _existing_columns(table: str, tables: set[str]) -> set[str]:
    if table not in tables:
        return set()
    return {col["name"] for col in sa.inspect(op.get_bind()).get_columns(table)}


def _existing_indexes(table: str, tables: set[str]) -> set[str]:
    if table not in tables:
        return set()
    return {ix["name"] for ix in sa.inspect(op.get_bind()).get_indexes(table)}


def upgrade() -> None:
    tables = _tables()
    if "documents" not in tables:
        return
    have = _existing_columns("documents", tables)
    for name, kind, server_default, nullable in _DOCUMENT_COLUMNS:
        if name in have:
            continue
        op.add_column(
            "documents",
            # ``ingest_attempts`` is NOT NULL, so the server default is also what
            # fills every pre-existing row.
            sa.Column(name, kind, nullable=nullable, server_default=server_default),
        )
    if _STATUS_INDEX not in _existing_indexes("documents", tables):
        # claim() 每轮都按 status 过滤可领取的行。
        op.create_index(_STATUS_INDEX, "documents", ["status"])


def downgrade() -> None:
    tables = _tables()
    if "documents" not in tables:
        return
    if _STATUS_INDEX in _existing_indexes("documents", tables):
        op.drop_index(_STATUS_INDEX, table_name="documents")
    have = _existing_columns("documents", tables)
    for name, _kind, _default, _nullable in _DOCUMENT_COLUMNS:
        if name in have:
            op.drop_column("documents", name)
