"""WeChat Official Account identity binding (sql2er-style scan login).

Revision ID: 0013_wechat_identities
Revises: 0012_token_version_and_msg_index
Create Date: 2026-09-17

``wechat_identities`` maps a WeChat follower's ``openid`` to the MyChat account
its scan logins resolve to. Kept as its own table (rather than a column on
``users``) so the UNIQUE constraint on ``openid`` is what prevents one account
from claiming another's already-bound WeChat.

Idempotent/guarded, matching 0012 — safe against databases created by
``create_all`` rather than by migrations.
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0013_wechat_identities"
down_revision: Union[str, None] = "0012_token_version_and_msg_index"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _has_table(table: str) -> bool:
    bind = op.get_bind()
    return table in sa.inspect(bind).get_table_names()


def upgrade() -> None:
    if _has_table("wechat_identities"):
        return
    op.create_table(
        "wechat_identities",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("openid", sa.String(length=64), nullable=False),
        sa.Column(
            "user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "bound_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
    )
    op.create_index(
        "ix_wechat_identities_openid", "wechat_identities", ["openid"], unique=True
    )
    op.create_index("ix_wechat_identities_user_id", "wechat_identities", ["user_id"])


def downgrade() -> None:
    if _has_table("wechat_identities"):
        op.drop_table("wechat_identities")
