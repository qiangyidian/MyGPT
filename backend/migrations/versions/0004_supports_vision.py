"""Add model_configs.supports_vision (multimodal image input).

Revision ID: 0004_supports_vision
Revises: 0003_projects_and_tasks
Create Date: 2026-08-05 00:00:00

Adds a single boolean column so each configured model endpoint can declare
whether it accepts OpenAI ``image_url`` content parts. Defaults to False
(not nullable, with a server_default so existing rows back-fill cleanly).
batch_alter_table keeps SQLite ALTER working for the test suite.
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0004_supports_vision"
down_revision: Union[str, None] = "0003_projects_and_tasks"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table("model_configs") as batch:
        batch.add_column(
            sa.Column(
                "supports_vision",
                sa.Boolean(),
                nullable=False,
                server_default=sa.sql.expression.false(),
            )
        )


def downgrade() -> None:
    with op.batch_alter_table("model_configs") as batch:
        batch.drop_column("supports_vision")
