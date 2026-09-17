"""Credits + redeem codes (预付费积分与兑换码).

Revision ID: 0014_credits_redeem
Revises: 0013_wechat_identities
Create Date: 2026-09-17

四张表：credit_accounts（权威余额）、credit_ledger（只追加账本）、
redeem_code_batches、redeem_codes。

两处关键点：

1. ``uq_credit_ledger_ref`` 是**唯一部分索引**（``WHERE ref_type IS NOT NULL``），
   让"同一轮对话扣两次"和"同一个码兑两次"在数据库层面不可能发生。
2. upgrade 会为所有存量用户**回填** credit_accounts 行。不可省：没有回填，
   老用户就不存在账户行，"余额为 0"和"账户不存在"在排查时会变成两件事。

守卫式写法（与 0012 / 0013 一致）：对由 ``create_all`` 而非迁移建库的库也安全。
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0014_credits_redeem"
down_revision: Union[str, None] = "0013_wechat_identities"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_TABLES = (
    "credit_accounts",
    "credit_ledger",
    "redeem_code_batches",
    "redeem_codes",
)


def _has_table(table: str) -> bool:
    bind = op.get_bind()
    return table in sa.inspect(bind).get_table_names()


def upgrade() -> None:
    if not _has_table("credit_accounts"):
        op.create_table(
            "credit_accounts",
            sa.Column("user_id", postgresql.UUID(as_uuid=True), primary_key=True),
            sa.Column("balance", sa.BigInteger(), nullable=False, server_default="0"),
            sa.Column(
                "lifetime_granted", sa.BigInteger(), nullable=False, server_default="0"
            ),
            sa.Column(
                "lifetime_consumed", sa.BigInteger(), nullable=False, server_default="0"
            ),
            sa.Column(
                "created_at",
                sa.DateTime(timezone=True),
                server_default=sa.func.now(),
                nullable=False,
            ),
            sa.Column(
                "updated_at",
                sa.DateTime(timezone=True),
                server_default=sa.func.now(),
                nullable=False,
            ),
            sa.ForeignKeyConstraint(
                ["user_id"], ["users.id"], name="fk_credit_accounts_user", ondelete="CASCADE"
            ),
        )

    if not _has_table("credit_ledger"):
        op.create_table(
            "credit_ledger",
            sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
            sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=False),
            sa.Column("delta", sa.BigInteger(), nullable=False),
            sa.Column("balance_after", sa.BigInteger(), nullable=False),
            sa.Column("reason", sa.String(length=32), nullable=False),
            sa.Column("ref_type", sa.String(length=32), nullable=True),
            sa.Column("ref_id", sa.String(length=64), nullable=True),
            sa.Column("actor_id", postgresql.UUID(as_uuid=True), nullable=True),
            sa.Column("note", sa.Text(), nullable=True),
            sa.Column(
                "created_at",
                sa.DateTime(timezone=True),
                server_default=sa.func.now(),
                nullable=False,
            ),
            sa.ForeignKeyConstraint(
                ["user_id"], ["users.id"], name="fk_credit_ledger_user", ondelete="CASCADE"
            ),
            sa.ForeignKeyConstraint(
                ["actor_id"], ["users.id"], name="fk_credit_ledger_actor", ondelete="SET NULL"
            ),
        )
        op.create_index("ix_credit_ledger_user_id", "credit_ledger", ["user_id"])
        op.create_index(
            "ix_credit_ledger_user_created", "credit_ledger", ["user_id", "created_at"]
        )
        # 幂等根基：同一 (ref_type, ref_id, reason) 只能一行。
        op.create_index(
            "uq_credit_ledger_ref",
            "credit_ledger",
            ["ref_type", "ref_id", "reason"],
            unique=True,
            postgresql_where=sa.text("ref_type IS NOT NULL"),
        )

    if not _has_table("redeem_code_batches"):
        op.create_table(
            "redeem_code_batches",
            sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
            sa.Column("name", sa.String(length=128), nullable=False),
            sa.Column("credits_per_code", sa.BigInteger(), nullable=False),
            sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("note", sa.Text(), nullable=True),
            sa.Column("created_by", postgresql.UUID(as_uuid=True), nullable=True),
            sa.Column(
                "created_at",
                sa.DateTime(timezone=True),
                server_default=sa.func.now(),
                nullable=False,
            ),
            sa.Column(
                "updated_at",
                sa.DateTime(timezone=True),
                server_default=sa.func.now(),
                nullable=False,
            ),
            sa.ForeignKeyConstraint(
                ["created_by"],
                ["users.id"],
                name="fk_redeem_batch_creator",
                ondelete="SET NULL",
            ),
            sa.CheckConstraint(
                "credits_per_code > 0", name="ck_redeem_batch_credits_positive"
            ),
        )

    if not _has_table("redeem_codes"):
        op.create_table(
            "redeem_codes",
            sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
            sa.Column("batch_id", postgresql.UUID(as_uuid=True), nullable=False),
            sa.Column("code_hash", sa.String(length=64), nullable=False),
            sa.Column("code_prefix", sa.String(length=8), nullable=False),
            sa.Column(
                "status", sa.String(length=16), nullable=False, server_default="active"
            ),
            sa.Column("redeemed_by", postgresql.UUID(as_uuid=True), nullable=True),
            sa.Column("redeemed_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column(
                "created_at",
                sa.DateTime(timezone=True),
                server_default=sa.func.now(),
                nullable=False,
            ),
            sa.ForeignKeyConstraint(
                ["batch_id"],
                ["redeem_code_batches.id"],
                name="fk_redeem_codes_batch",
                ondelete="CASCADE",
            ),
            sa.ForeignKeyConstraint(
                ["redeemed_by"], ["users.id"], name="fk_redeem_codes_redeemer", ondelete="SET NULL"
            ),
        )
        op.create_index("ix_redeem_codes_batch_id", "redeem_codes", ["batch_id"])
        op.create_index("ix_redeem_codes_code_hash", "redeem_codes", ["code_hash"], unique=True)
        op.create_index(
            "ix_redeem_codes_batch_status", "redeem_codes", ["batch_id", "status"]
        )

    # 回填：每个存量用户一行账户（余额 0）。幂等，重复执行安全。
    # `WHERE true` 在 INSERT...SELECT + ON CONFLICT 里不可省：SQLite 的解析器
    # 会把 ON 误认成 JOIN 子句（官方文档明确要求加 WHERE 消歧），Postgres 上无影响。
    if not _has_table("__never__"):
        op.execute(
            """
            INSERT INTO credit_accounts
                (user_id, balance, lifetime_granted, lifetime_consumed)
            SELECT id, 0, 0, 0 FROM users WHERE true
            ON CONFLICT (user_id) DO NOTHING
            """
        )


def downgrade() -> None:
    for table in reversed(_TABLES):
        if _has_table(table):
            op.drop_table(table)
