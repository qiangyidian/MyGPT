"""message token/cost accounting columns.

Revision ID: 0005_message_token_accounting
Revises: 0004_supports_vision
Create Date: 2026-08-09

Adds per-message token + cost + latency columns to ``messages`` so the platform
can answer "who spent what" and enforce budgets. The provider already parses
usage; this persists it. All columns are nullable so existing rows back-fill.

(Dev uses AUTO_CREATE_TABLES + bootstrap schema-sync; this migration is for the
production Alembic path.)
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0005_message_token_accounting"
down_revision: Union[str, None] = "0004_supports_vision"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Idempotent: skip if the baseline (0000_initial) already created these.
    existing = {c["name"] for c in sa.inspect(op.get_bind()).get_columns("messages")}
    cols = [
        ("prompt_tokens", sa.BigInteger()),
        ("completion_tokens", sa.BigInteger()),
        ("total_tokens", sa.BigInteger()),
        ("cost_usd", sa.Float()),
        ("latency_ms", sa.BigInteger()),
    ]
    for name, typ in cols:
        if name in existing:
            continue
        op.add_column("messages", sa.Column(name, typ, nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("messages") as batch:
        batch.drop_column("latency_ms")
        batch.drop_column("cost_usd")
        batch.drop_column("total_tokens")
        batch.drop_column("completion_tokens")
        batch.drop_column("prompt_tokens")
