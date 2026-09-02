"""Cleanup: drop orphaned background_tasks table + hot-path indexes.

Revision ID: 0011_cleanup_and_indexes
Revises: 0010_artifacts
Create Date: 2026-09-02

Two things in one migration (both low-risk, additive/drop-only):

1. Drop ``background_tasks`` — the table backed an orphaned Phase-3 router
   (``/api/background-tasks``) that no client ever called; the real durable
   queue lives in ``agent_runs`` + ``run_events`` (``agents/workflow/queue.py``).
   The router, service, model, and schemas were removed in the same commit.

2. Add indexes for the hot sort paths found during the perf audit:
   - ``conversations (user_id, is_archived, updated_at)`` — sidebar list
     sorts by ``is_pinned DESC, updated_at DESC`` filtered by user+archive.
   - ``agent_runs.created_at`` — list endpoint sorts by ``created_at DESC``
     (admin sees the whole table).

Both index creates are guarded: skipped when an equivalent index already
exists (baseline-created DBs may already have them).
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0011_cleanup_and_indexes"
down_revision: Union[str, None] = "0010_artifacts"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _uuid_type() -> sa.types.TypeEngine:
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        from sqlalchemy.dialects.postgresql import UUID
        return UUID(as_uuid=True)
    return sa.String(length=36)


def _existing_index_names(table: str) -> set[str]:
    bind = op.get_bind()
    return {ix["name"] for ix in sa.inspect(bind).get_indexes(table)}


def upgrade() -> None:
    bind = op.get_bind()
    tables = set(sa.inspect(bind).get_table_names())

    # 1. Drop the orphaned background_tasks table (drop is idempotent-ish:
    # only run when present).
    if "background_tasks" in tables:
        op.drop_table("background_tasks")

    # 2. Hot-path indexes.
    if "conversations" in tables:
        have = _existing_index_names("conversations")
        if "ix_conversations_user_archived_updated" not in have:
            op.create_index(
                "ix_conversations_user_archived_updated",
                "conversations",
                ["user_id", "is_archived", "updated_at"],
            )

    if "agent_runs" in tables:
        have = _existing_index_names("agent_runs")
        if "ix_agent_runs_created_at" not in have:
            op.create_index("ix_agent_runs_created_at", "agent_runs", ["created_at"])


def downgrade() -> None:
    bind = op.get_bind()
    tables = set(sa.inspect(bind).get_table_names())

    if "agent_runs" in tables:
        have = _existing_index_names("agent_runs")
        if "ix_agent_runs_created_at" in have:
            op.drop_index("ix_agent_runs_created_at", table_name="agent_runs")

    if "conversations" in tables:
        have = _existing_index_names("conversations")
        if "ix_conversations_user_archived_updated" in have:
            op.drop_index("ix_conversations_user_archived_updated", table_name="conversations")

    # background_tasks is not recreated on downgrade: the code that read it is
    # gone, so restoring the table would just re-add a dead one.
