"""Phase 1 product upgrade: chat attachments, message feedback, conversation
management (pin/archive/preview/branch), and agent-run plan/instruction
reservation columns.

Revision ID: 0002_phase1_product
Revises: 0001_agent_graph
Create Date: 2026-07-19 00:00:00

Adds:
  * conversations.is_pinned / is_archived / last_message_preview
  * conversations.parent_conversation_id (self-ref) / branch_from_message_id / project_id
  * agent_runs.plan / plan_status / user_instructions / paused_at / resume_token
  * new table chat_attachments (per-message/per-conversation file uploads)
  * new table message_feedback (thumbs up/down, unique per user+message)

All new columns are nullable or carry a server_default so existing rows are
compatible. Uses JSONB on Postgres and JSON elsewhere; UUID on Postgres and
VARCHAR(36) elsewhere (matches the ORM dialect choice and the SQLite shims in
app/db.py + tests/conftest.py). batch_alter_table keeps SQLite ALTER working.
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0002_phase1_product"
down_revision: Union[str, None] = "0001_agent_graph"
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
    # ---- conversations: management columns ----
    with op.batch_alter_table("conversations") as batch:
        batch.add_column(
            sa.Column("is_pinned", sa.Boolean(), nullable=False, server_default=sa.sql.expression.false())
        )
        batch.add_column(
            sa.Column("is_archived", sa.Boolean(), nullable=False, server_default=sa.sql.expression.false())
        )
        batch.add_column(sa.Column("last_message_preview", sa.String(length=280), nullable=True))
        batch.add_column(sa.Column("parent_conversation_id", _uuid_type(), nullable=True))
        batch.add_column(sa.Column("branch_from_message_id", _uuid_type(), nullable=True))
        batch.add_column(sa.Column("project_id", _uuid_type(), nullable=True))

    op.create_index("ix_conversations_parent_conversation_id", "conversations", ["parent_conversation_id"])
    op.create_index("ix_conversations_project_id", "conversations", ["project_id"])
    # Pin-first + non-archived list filtering.
    op.create_index(
        "ix_conversations_user_archived_pinned_updated",
        "conversations",
        ["user_id", "is_archived", "is_pinned", "updated_at"],
    )

    # ---- agent_runs: plan / instruction reservation ----
    with op.batch_alter_table("agent_runs") as batch:
        batch.add_column(sa.Column("plan", _json_type(), nullable=True))
        batch.add_column(
            sa.Column("plan_status", sa.String(length=32), nullable=False, server_default="")
        )
        batch.add_column(sa.Column("user_instructions", _json_type(), nullable=True))
        batch.add_column(sa.Column("paused_at", sa.DateTime(timezone=True), nullable=True))
        batch.add_column(
            sa.Column("resume_token", sa.String(length=64), nullable=False, server_default="")
        )

    # ---- chat_attachments ----
    op.create_table(
        "chat_attachments",
        sa.Column("id", _uuid_type(), primary_key=True),
        sa.Column("user_id", _uuid_type(), nullable=False),
        sa.Column("conversation_id", _uuid_type(), nullable=False),
        sa.Column("message_id", _uuid_type(), nullable=True),
        sa.Column("filename", sa.String(length=255), nullable=False),
        sa.Column("original_filename", sa.String(length=255), nullable=False),
        sa.Column("mime_type", sa.String(length=255), nullable=False, server_default=""),
        sa.Column("size_bytes", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("storage_key", sa.String(length=512), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False, server_default="uploading"),
        sa.Column("parse_status", sa.String(length=32), nullable=False, server_default="pending"),
        sa.Column("extracted_text", sa.Text(), nullable=True),
        sa.Column("preview_metadata", _json_type(), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("is_temporary", sa.Boolean(), nullable=False, server_default=sa.sql.expression.true()),
        sa.Column("knowledge_base_id", _uuid_type(), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
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
        sa.ForeignKeyConstraint(["conversation_id"], ["conversations.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["message_id"], ["messages.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["knowledge_base_id"], ["knowledge_bases.id"], ondelete="SET NULL"),
    )
    op.create_index("ix_chat_attachments_user_id", "chat_attachments", ["user_id"])
    op.create_index("ix_chat_attachments_conversation_id", "chat_attachments", ["conversation_id"])
    op.create_index("ix_chat_attachments_message_id", "chat_attachments", ["message_id"])
    op.create_index("ix_chat_attachments_knowledge_base_id", "chat_attachments", ["knowledge_base_id"])

    # ---- message_feedback ----
    op.create_table(
        "message_feedback",
        sa.Column("id", _uuid_type(), primary_key=True),
        sa.Column("user_id", _uuid_type(), nullable=False),
        sa.Column("message_id", _uuid_type(), nullable=False),
        sa.Column("conversation_id", _uuid_type(), nullable=False),
        sa.Column("rating", sa.String(length=8), nullable=False),
        sa.Column("reason", sa.String(length=64), nullable=True),
        sa.Column("comment", sa.Text(), nullable=True),
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
        sa.ForeignKeyConstraint(["message_id"], ["messages.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["conversation_id"], ["conversations.id"], ondelete="CASCADE"),
        sa.UniqueConstraint("user_id", "message_id", name="uq_message_feedback_user_message"),
    )
    op.create_index("ix_message_feedback_user_id", "message_feedback", ["user_id"])
    op.create_index("ix_message_feedback_message_id", "message_feedback", ["message_id"])
    op.create_index("ix_message_feedback_conversation_id", "message_feedback", ["conversation_id"])


def downgrade() -> None:
    op.drop_index("ix_message_feedback_conversation_id", table_name="message_feedback")
    op.drop_index("ix_message_feedback_message_id", table_name="message_feedback")
    op.drop_index("ix_message_feedback_user_id", table_name="message_feedback")
    op.drop_table("message_feedback")

    op.drop_index("ix_chat_attachments_knowledge_base_id", table_name="chat_attachments")
    op.drop_index("ix_chat_attachments_message_id", table_name="chat_attachments")
    op.drop_index("ix_chat_attachments_conversation_id", table_name="chat_attachments")
    op.drop_index("ix_chat_attachments_user_id", table_name="chat_attachments")
    op.drop_table("chat_attachments")

    with op.batch_alter_table("agent_runs") as batch:
        batch.drop_column("resume_token")
        batch.drop_column("paused_at")
        batch.drop_column("user_instructions")
        batch.drop_column("plan_status")
        batch.drop_column("plan")

    op.drop_index("ix_conversations_user_archived_pinned_updated", table_name="conversations")
    op.drop_index("ix_conversations_project_id", table_name="conversations")
    op.drop_index("ix_conversations_parent_conversation_id", table_name="conversations")
    with op.batch_alter_table("conversations") as batch:
        batch.drop_column("project_id")
        batch.drop_column("branch_from_message_id")
        batch.drop_column("parent_conversation_id")
        batch.drop_column("last_message_preview")
        batch.drop_column("is_archived")
        batch.drop_column("is_pinned")
