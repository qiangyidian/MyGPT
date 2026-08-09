"""agent graph: graph_definition/graph_state on agent_runs, attribution on agent_steps

Revision ID: 0001_agent_graph
Revises:
Create Date: 2026-07-18 00:00:00

Adds multi-agent visualization persistence:
  * agent_runs.graph_definition  JSONB — static topology (nodes/edges/mode)
  * agent_runs.graph_state       JSONB — live node/edge status snapshot
  * agent_steps.agent_id         str   — stable graph node id (e.g. "researcher")
  * agent_steps.task_id          str   — CrewAI task id when available
  * agent_steps.parent_step_id   UUID  — optional parent step (handoff source)
  * agent_steps.metadata         JSONB — structured, redacted extras

These are additive columns; the existing create_all path (dev/test) picks them
up automatically. This migration is for production Alembic runs. Uses JSONB on
Postgres and falls back to JSON elsewhere via dialect-conditional types.
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0001_agent_graph"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _json_type() -> sa.types.TypeEngine:
    """JSONB on Postgres, JSON elsewhere (matches the ORM column dialect choice)."""
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        from sqlalchemy.dialects.postgresql import JSONB
        return JSONB()
    return sa.JSON()


def _uuid_type() -> sa.types.TypeEngine:
    """UUID on Postgres, VARCHAR(36) elsewhere (matches the ORM + SQLite test shim)."""
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        from sqlalchemy.dialects.postgresql import UUID
        return UUID(as_uuid=True)
    return sa.String(length=36)


def upgrade() -> None:
    # agent_runs: graph columns
    with op.batch_alter_table("agent_runs") as batch:
        batch.add_column(sa.Column("graph_definition", _json_type(), nullable=True))
        batch.add_column(sa.Column("graph_state", _json_type(), nullable=True))

    # agent_steps: attribution columns
    with op.batch_alter_table("agent_steps") as batch:
        batch.add_column(
            sa.Column("agent_id", sa.String(length=64), nullable=False, server_default="")
        )
        batch.add_column(
            sa.Column("task_id", sa.String(length=64), nullable=False, server_default="")
        )
        batch.add_column(sa.Column("parent_step_id", _uuid_type(), nullable=True))
        batch.add_column(sa.Column("metadata", _json_type(), nullable=True))

    op.create_index("ix_agent_steps_agent_id", "agent_steps", ["agent_id"])


def downgrade() -> None:
    op.drop_index("ix_agent_steps_agent_id", table_name="agent_steps")
    with op.batch_alter_table("agent_steps") as batch:
        batch.drop_column("metadata")
        batch.drop_column("parent_step_id")
        batch.drop_column("task_id")
        batch.drop_column("agent_id")
    with op.batch_alter_table("agent_runs") as batch:
        batch.drop_column("graph_state")
        batch.drop_column("graph_definition")
