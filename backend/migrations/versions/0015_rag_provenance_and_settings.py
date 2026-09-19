"""RAG 溯源列 + 每库检索参数 + 关键词索引.

Revision ID: 0015_rag_provenance_and_settings
Revises: 0014_credits_redeem
Create Date: 2026-09-19

三件事，全部是「加列 / 加索引」，没有回填也没有破坏性变更：

1. ``document_chunks`` 的向量溯源列（``embedding_model`` / ``embedding_dim`` /
   ``content_sha256``）。换 embedding 模型过去是一次不可见的破坏：旧向量留在同
   一个 collection 里继续参与检索，维度和语义空间都不对，而库里没有任何地方能
   查出是哪一批块坏的。
2. ``knowledge_bases`` 的每库检索 / 切分参数。一律可空，NULL = 沿用全局默认，
   所以既有行行为不变（见 app/models/knowledge_base.py 的注释）。
3. Postgres 上给 ``document_chunks.content`` 建 ``pg_trgm`` 的 GIN 索引。混合检
   索的关键词一路是 ``ILIKE '%词%'``，btree 帮不上，大库上等于每轮对话一次全表
   扫。这里刻意**不用** tsvector/GIN 全文索引：全文是按 token 匹配的，而 PG 默认
   解析器不做中文分词，一整句中文会变成一个 token，「检索」就再也匹配不到
   「知识库检索」——召回会静默变差。trigram 与 ILIKE 是同一套子串语义，只提速，
   不改变返回的行。SQLite（测试与轻量部署）没有 pg_trgm，整段跳过。

   实测边界（postgres:16-alpine、50 万行；分析过程见 app/rag/keyword.py 顶部）：
   三个字符以上的词（拉丁词、较长的中文短语）确实走 trigram bitmap，成本从几千降到
   几十；但中文检索最常见的**两个字**的 bigram，规划器仍然选扫描 —— 两字产生的
   trigram 太不选择性，索引并不更快。所以这索引是一块有限收益的补丁，并没有把全表
   扫消掉：真正把每轮检索的工作量框住的是 ``knowledge_base_id`` 上的 btree（关键词
   检索始终按单个知识库发起）。

注意：这几张表由 ``0000_initial`` 的 ``Base.metadata.create_all`` 建出来，也就是
按「当前模型」建的 —— 空库路径上它们天生就带着这些新列。所以这里的守卫不是可有
可无的：它同时覆盖「新库（列已在，必须跳过）」与「比 0000 更老、直接 create_all
建出来的生产库（列不在，要补）」。风格与 0011/0012/0014 一致。
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0015_rag_provenance_and_settings"
down_revision: Union[str, None] = "0014_credits_redeem"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_TRGM_INDEX = "ix_document_chunks_content_trgm_gin"

_KB_COLUMNS: tuple[tuple[str, sa.types.TypeEngine], ...] = (
    ("top_k", sa.Integer()),
    ("score_threshold", sa.Float()),
    ("rerank_enabled", sa.Boolean()),
    ("chunk_size", sa.Integer()),
    ("chunk_overlap", sa.Integer()),
)

_CHUNK_COLUMNS: tuple[tuple[str, sa.types.TypeEngine], ...] = (
    ("embedding_model", sa.String(length=128)),
    ("embedding_dim", sa.Integer()),
    ("content_sha256", sa.String(length=64)),
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


def _add_missing(
    table: str, columns: tuple[tuple[str, sa.types.TypeEngine], ...], tables: set[str]
) -> None:
    have = _existing_columns(table, tables)
    for name, kind in columns:
        if name in have:
            continue
        op.add_column(table, sa.Column(name, kind, nullable=True))


def _try_pg_trgm_index() -> None:
    """Create the trigram index, tolerating an account that cannot install pg_trgm.

    Each statement runs in its own SAVEPOINT: with Alembic's transactional DDL a
    failure at the outer level would roll back the columns added above and block
    the whole deploy for a pure-performance index.
    """
    import logging

    bind = op.get_bind()
    log = logging.getLogger("alembic.runtime.migration")
    for stmt in (
        "CREATE EXTENSION IF NOT EXISTS pg_trgm",
        f"CREATE INDEX IF NOT EXISTS {_TRGM_INDEX} "
        "ON document_chunks USING gin (content gin_trgm_ops)",
    ):
        try:
            with bind.begin_nested():
                bind.execute(sa.text(stmt))
        except Exception as exc:
            log.warning(
                "[0015] skipped %s (%s) — keyword retrieval keeps working, "
                "just without the trigram index",
                stmt.split()[1],
                exc,
            )
            return


def upgrade() -> None:
    tables = _tables()
    _add_missing("knowledge_bases", _KB_COLUMNS, tables)
    _add_missing("document_chunks", _CHUNK_COLUMNS, tables)

    if "document_chunks" in tables:
        # 「这个库里还有哪些块是旧模型算的」——重建/巡检按这一列筛。
        if "ix_document_chunks_embedding_model" not in _existing_indexes(
            "document_chunks", tables
        ):
            op.create_index(
                "ix_document_chunks_embedding_model",
                "document_chunks",
                ["embedding_model"],
            )
        # pg_trgm 只在 Postgres 上存在。这一步用 SAVEPOINT 包住：这索引纯属提速，
        # 生产库的迁移账号若不是 superuser/扩展不可用，也不能把整次部署卡住 —— 在外
        # 层事务里直接失败的话，Alembic 的 transactional DDL 会把前面已加的列一起回滚。
        if op.get_bind().dialect.name == "postgresql":
            if _TRGM_INDEX not in _existing_indexes("document_chunks", tables):
                _try_pg_trgm_index()


def downgrade() -> None:
    tables = _tables()
    if "document_chunks" in tables:
        if op.get_bind().dialect.name == "postgresql":
            op.execute(sa.text(f"DROP INDEX IF EXISTS {_TRGM_INDEX}"))
        if "ix_document_chunks_embedding_model" in _existing_indexes(
            "document_chunks", tables
        ):
            op.drop_index(
                "ix_document_chunks_embedding_model", table_name="document_chunks"
            )
        have = _existing_columns("document_chunks", tables)
        for name, _kind in _CHUNK_COLUMNS:
            if name in have:
                op.drop_column("document_chunks", name)
    if "knowledge_bases" in tables:
        have = _existing_columns("knowledge_bases", tables)
        for name, _kind in _KB_COLUMNS:
            if name in have:
                op.drop_column("knowledge_bases", name)
