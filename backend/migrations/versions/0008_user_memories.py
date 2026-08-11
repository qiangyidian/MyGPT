"""user_memories: opt-in semantic long-term user memory (Task 7).

Revision ID: 0008_user_memories
Revises: 0007_enterprise_workflow
Create Date: 2026-08-11

Adds ONE additive table — ``user_memories`` — for user-scoped semantic memory
that crosses conversations. Distinct from ``conversation_memories`` (which is
conversation-scoped with a NOT-NULL ``conversation_id``): a UserMemory is
opt-in (``active`` defaults False), tenant-isolated by ``user_id``, and carries
provenance (source message / conversation) + confidence + expiry.

On a baseline-created DB (create_all already ran against the full metadata
incl. this table) the upgrade is a guarded no-op, matching the 0001/0003/0007
convention.
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0008_user_memories"
down_revision: Union[str, None] = "0007_enterprise_workflow"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _json_type() -> sa.types.TypeEngine:
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        from sqlalchemy.dialects.postgresql import JSONB
        return JSONB()
    return sa.JSON()


def _uuid_type() -> sa.types.TypeEngine:
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        from sqlalchemy.dialects.postgresql import UUID
        return UUID(as_uuid=True)
    return sa.String(length=36)


def upgrade() -> None:
    bind = op.get_bind()
    existing = set(sa.inspect(bind).get_table_names())
    if "user_memories" in existing:
        return

    op.create_table(
        "user_memories",
        sa.Column("id", _uuid_type(), primary_key=True),
        sa.Column("user_id", _uuid_type(), nullable=False),
        sa.Column(
            "memory_type", sa.String(length=32), nullable=False, server_default="fact"
        ),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("structured_value", _json_type(), nullable=True),
        sa.Column(
            "confidence", sa.Float(), nullable=False, server_default="0.5"
        ),
        sa.Column("source_message_id", _uuid_type(), nullable=True),
        sa.Column("source_conversation_id", _uuid_type(), nullable=True),
        # Opt-in: a candidate is inactive until the user activates it.
        sa.Column(
            "active", sa.Boolean(), nullable=False, server_default=sa.false()
        ),
        sa.Column(
            "confirmed_by_user",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("embedding_id", sa.String(length=128), nullable=True),
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
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["source_message_id"], ["messages.id"], ondelete="SET NULL"
        ),
        sa.ForeignKeyConstraint(
            ["source_conversation_id"], ["conversations.id"], ondelete="SET NULL"
        ),
    )
    op.create_index("ix_user_memories_user_id", "user_memories", ["user_id"])
    op.create_index("ix_user_memories_active", "user_memories", ["active"])


def downgrade() -> None:
    op.drop_index("ix_user_memories_active", table_name="user_memories")
    op.drop_index("ix_user_memories_user_id", table_name="user_memories")
    op.drop_table("user_memories")
