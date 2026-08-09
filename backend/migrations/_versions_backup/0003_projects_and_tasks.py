"""Phase 3: Projects + Background tasks.

Revision ID: 0003_projects_and_tasks
Revises: 0002_phase1_product
Create Date: 2026-07-19 12:00:00

Adds:
  * projects              — user-defined conversation groupings
  * background_tasks      — durable async work queue (inprocess worker)
  * conversation_memories already exists; no schema change here.

Conversation.project_id (added in 0002) is a soft reference to projects.id
(no hard FK) so deleting a project leaves conversations intact. All new
columns nullable / defaulted; batch_alter_table + dialect-conditional types
match the 0001/0002 conventions.
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0003_projects_and_tasks"
down_revision: Union[str, None] = "0002_phase1_product"
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
    op.create_table(
        "projects",
        sa.Column("id", _uuid_type(), primary_key=True),
        sa.Column("user_id", _uuid_type(), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("color", sa.String(length=16), nullable=False, server_default="#6366f1"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
    )
    op.create_index("ix_projects_user_id", "projects", ["user_id"])

    op.create_table(
        "background_tasks",
        sa.Column("id", _uuid_type(), primary_key=True),
        sa.Column("user_id", _uuid_type(), nullable=True),
        sa.Column("kind", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False, server_default="pending"),
        sa.Column("payload", _json_type(), nullable=False),
        sa.Column("result", _json_type(), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("conversation_id", _uuid_type(), nullable=True),
        sa.Column("scheduled_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["conversation_id"], ["conversations.id"], ondelete="SET NULL"),
    )
    op.create_index("ix_background_tasks_user_id", "background_tasks", ["user_id"])
    op.create_index("ix_background_tasks_kind", "background_tasks", ["kind"])
    op.create_index("ix_background_tasks_status", "background_tasks", ["status"])
    op.create_index("ix_background_tasks_conversation_id", "background_tasks", ["conversation_id"])


def downgrade() -> None:
    op.drop_index("ix_background_tasks_conversation_id", table_name="background_tasks")
    op.drop_index("ix_background_tasks_status", table_name="background_tasks")
    op.drop_index("ix_background_tasks_kind", table_name="background_tasks")
    op.drop_index("ix_background_tasks_user_id", table_name="background_tasks")
    op.drop_table("background_tasks")

    op.drop_index("ix_projects_user_id", table_name="projects")
    op.drop_table("projects")
