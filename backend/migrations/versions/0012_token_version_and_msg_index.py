"""Token revocation (users.token_version) + messages hot-path composite index.

Revision ID: 0012_token_version_and_msg_index
Revises: 0011_cleanup_and_indexes
Create Date: 2026-09-05

1. ``users.token_version`` (int, default 0) — global access-token kill switch.
   ``issue_tokens`` embeds it as the ``ver`` claim; ``get_current_user`` rejects
   tokens whose ``ver`` is stale, so admin deactivation (which bumps it) and
   future "log out everywhere" flows invalidate already-issued tokens instantly.

2. ``messages (conversation_id, created_at)`` composite index — the hot path
   ``WHERE conversation_id = ? ORDER BY created_at DESC LIMIT n`` (conversation
   detail + history window) previously matched only the single-column
   ``conversation_id`` index and sorted every matching row.

Both steps are guarded/idempotent (safe against baseline-created DBs).
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0012_token_version_and_msg_index"
down_revision: Union[str, None] = "0011_cleanup_and_indexes"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _existing_columns(table: str) -> set[str]:
    bind = op.get_bind()
    return {col["name"] for col in sa.inspect(bind).get_columns(table)}


def _existing_index_names(table: str) -> set[str]:
    bind = op.get_bind()
    return {ix["name"] for ix in sa.inspect(bind).get_indexes(table)}


def upgrade() -> None:
    if "token_version" not in _existing_columns("users"):
        op.add_column(
            "users",
            sa.Column("token_version", sa.Integer(), nullable=False, server_default="0"),
        )

    if "ix_messages_conversation_created" not in _existing_index_names("messages"):
        op.create_index(
            "ix_messages_conversation_created",
            "messages",
            ["conversation_id", "created_at"],
        )


def downgrade() -> None:
    if "ix_messages_conversation_created" in _existing_index_names("messages"):
        op.drop_index("ix_messages_conversation_created", table_name="messages")
    if "token_version" in _existing_columns("users"):
        op.drop_column("users", "token_version")
