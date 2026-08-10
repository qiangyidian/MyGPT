"""enterprise workflow: durable events, commands, leases, attempts.

Revision ID: 0007_enterprise_workflow
Revises: 0006_model_capabilities
Create Date: 2026-08-10

Adds four additive tables that make agent workflows durable and resumable:
  * run_events      — append-only, per-run monotonic event log (replay source)
  * run_commands    — exactly-once control-command queue (pause/resume/...)
  * run_leases      — one live execution lease per run (optimistic fencing)
  * agent_attempts  — per-step retry/usage accounting (reserved for Task 6)

Uses native PostgreSQL UUID + JSONB (alembic targets PG); on a baseline-created
DB (0000_initial already ran create_all against the full metadata incl. these
tables) the upgrade is a guarded no-op, matching the 0001/0003 convention.
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0007_enterprise_workflow"
down_revision: Union[str, None] = "0006_model_capabilities"
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
    if {
        "run_events",
        "run_commands",
        "run_leases",
        "agent_attempts",
    } <= existing:
        return

    # ------------------------------------------------------------------ #
    # run_events
    # ------------------------------------------------------------------ #
    if "run_events" not in existing:
        op.create_table(
            "run_events",
            sa.Column("id", _uuid_type(), primary_key=True),
            sa.Column("run_id", _uuid_type(), nullable=False),
            sa.Column("sequence", sa.Integer(), nullable=False),
            sa.Column("event_type", sa.String(length=64), nullable=False),
            sa.Column("data", _json_type(), nullable=False),
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
                ["run_id"], ["agent_runs.id"], ondelete="CASCADE"
            ),
            sa.UniqueConstraint(
                "run_id", "sequence", name="uq_run_events_run_id_sequence"
            ),
        )
        op.create_index("ix_run_events_run_id", "run_events", ["run_id"])

    # ------------------------------------------------------------------ #
    # run_commands
    # ------------------------------------------------------------------ #
    if "run_commands" not in existing:
        op.create_table(
            "run_commands",
            sa.Column("id", _uuid_type(), primary_key=True),
            sa.Column("run_id", _uuid_type(), nullable=False),
            sa.Column("command_type", sa.String(length=32), nullable=False),
            sa.Column("payload", _json_type(), nullable=False),
            sa.Column(
                "status",
                sa.String(length=32),
                nullable=False,
                server_default="pending",
            ),
            sa.Column("claimed_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("claimed_by", sa.String(length=128), nullable=True),
            sa.Column("applied_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("error", sa.Text(), nullable=True),
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
                ["run_id"], ["agent_runs.id"], ondelete="CASCADE"
            ),
        )
        op.create_index("ix_run_commands_run_id", "run_commands", ["run_id"])
        op.create_index("ix_run_commands_status", "run_commands", ["status"])

    # ------------------------------------------------------------------ #
    # run_leases
    # ------------------------------------------------------------------ #
    if "run_leases" not in existing:
        op.create_table(
            "run_leases",
            sa.Column("id", _uuid_type(), primary_key=True),
            sa.Column("run_id", _uuid_type(), nullable=False),
            sa.Column("owner", sa.String(length=128), nullable=False),
            sa.Column(
                "version", sa.Integer(), nullable=False, server_default="1"
            ),
            sa.Column("acquired_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
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
                ["run_id"], ["agent_runs.id"], ondelete="CASCADE"
            ),
            sa.UniqueConstraint("run_id", name="uq_run_leases_run_id"),
        )

    # ------------------------------------------------------------------ #
    # agent_attempts
    # ------------------------------------------------------------------ #
    if "agent_attempts" not in existing:
        op.create_table(
            "agent_attempts",
            sa.Column("id", _uuid_type(), primary_key=True),
            sa.Column("run_id", _uuid_type(), nullable=False),
            sa.Column("step_key", sa.String(length=128), nullable=False),
            sa.Column(
                "attempt_number",
                sa.Integer(),
                nullable=False,
                server_default="1",
            ),
            sa.Column(
                "status",
                sa.String(length=32),
                nullable=False,
                server_default="pending",
            ),
            sa.Column("usage", _json_type(), nullable=True),
            sa.Column("error", sa.Text(), nullable=True),
            sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
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
                ["run_id"], ["agent_runs.id"], ondelete="CASCADE"
            ),
        )
        op.create_index(
            "ix_agent_attempts_run_id", "agent_attempts", ["run_id"]
        )


def downgrade() -> None:
    op.drop_index("ix_agent_attempts_run_id", table_name="agent_attempts")
    op.drop_table("agent_attempts")

    op.drop_table("run_leases")

    op.drop_index("ix_run_commands_status", table_name="run_commands")
    op.drop_index("ix_run_commands_run_id", table_name="run_commands")
    op.drop_table("run_commands")

    op.drop_index("ix_run_events_run_id", table_name="run_events")
    op.drop_table("run_events")
