"""Add durable model capability columns.

Revision ID: 0006_model_capabilities
Revises: 0005_message_token_accounting
Create Date: 2026-08-09

All columns have server defaults and are backfilled before enforcing NOT NULL,
covering both production migrations and dev databases where schema sync may
have previously created nullable columns.
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "0006_model_capabilities"
down_revision: Union[str, None] = "0005_message_token_accounting"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


_BOOLEAN_COLUMNS = (
    "supports_parallel_tools",
    "supports_audio_input",
    "supports_audio_output",
    "supports_image_generation",
    "supports_structured_output",
    "supports_reasoning_effort",
)
_OUTPUT_PARAMETER = "output_token_parameter"


def upgrade() -> None:
    existing = {
        column["name"]
        for column in sa.inspect(op.get_bind()).get_columns("model_configs")
    }
    with op.batch_alter_table("model_configs") as batch:
        for name in _BOOLEAN_COLUMNS:
            if name not in existing:
                batch.add_column(
                    sa.Column(
                        name,
                        sa.Boolean(),
                        nullable=True,
                        server_default=sa.sql.expression.false(),
                    )
                )
        if _OUTPUT_PARAMETER not in existing:
            batch.add_column(
                sa.Column(
                    _OUTPUT_PARAMETER,
                    sa.String(length=32),
                    nullable=True,
                    server_default="max_tokens",
                )
            )

    for name in _BOOLEAN_COLUMNS:
        op.execute(
            sa.text(
                f'UPDATE model_configs SET "{name}" = false WHERE "{name}" IS NULL'
            )
        )
    op.execute(
        sa.text(
            "UPDATE model_configs SET output_token_parameter = 'max_tokens' "
            "WHERE output_token_parameter IS NULL"
        )
    )

    with op.batch_alter_table("model_configs") as batch:
        for name in _BOOLEAN_COLUMNS:
            batch.alter_column(
                name,
                existing_type=sa.Boolean(),
                nullable=False,
                server_default=sa.sql.expression.false(),
            )
        batch.alter_column(
            _OUTPUT_PARAMETER,
            existing_type=sa.String(length=32),
            nullable=False,
            server_default="max_tokens",
        )


def downgrade() -> None:
    existing = {
        column["name"]
        for column in sa.inspect(op.get_bind()).get_columns("model_configs")
    }
    with op.batch_alter_table("model_configs") as batch:
        if _OUTPUT_PARAMETER in existing:
            batch.drop_column(_OUTPUT_PARAMETER)
        for name in reversed(_BOOLEAN_COLUMNS):
            if name in existing:
                batch.drop_column(name)
