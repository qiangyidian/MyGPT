"""connectors: encrypted tenant-scoped MCP connector definitions (Task 9).

Revision ID: 0009_connectors
Revises: 0008_user_memories
Create Date: 2026-08-11

Adds ONE additive table — ``connectors`` — for tenant-scoped MCP server
connector definitions: provider manifest snapshot, transport + command/URL,
Fernet-encrypted credentials (``credentials_enc``), granted OAuth scopes, and
an enable/disable flag. Tenant isolation is enforced by ``user_id`` on every
query (ConnectorService); the credentials column is ciphertext-only.

On a baseline-created DB (create_all already ran against the full metadata
incl. this table) the upgrade is a guarded no-op, matching the 0001/0003/
0007/0008 convention.
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0009_connectors"
down_revision: Union[str, None] = "0008_user_memories"
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
    if "connectors" in existing:
        return

    op.create_table(
        "connectors",
        sa.Column("id", _uuid_type(), primary_key=True),
        sa.Column("user_id", _uuid_type(), nullable=False),
        sa.Column("name", sa.String(length=128), nullable=False),
        sa.Column("provider", sa.String(length=64), nullable=False),
        # Manifest snapshot + scopes are JSONB on postgres, JSON on sqlite.
        sa.Column("manifest", _json_type(), nullable=False),
        sa.Column("transport", sa.String(length=16), nullable=False, server_default="stdio"),
        sa.Column("command_or_url", sa.String(length=512), nullable=False),
        # Fernet ciphertext (never plaintext).
        sa.Column(
            "credentials_enc",
            sa.String(length=4096),
            nullable=False,
            server_default="",
        ),
        sa.Column("oauth_scopes", _json_type(), nullable=False),
        sa.Column(
            "enabled",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
        sa.Column("extra", _json_type(), nullable=True),
        sa.Column("last_used_at", sa.DateTime(timezone=True), nullable=True),
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
    )
    op.create_index("ix_connectors_user_id", "connectors", ["user_id"])
    op.create_index("ix_connectors_provider", "connectors", ["provider"])


def downgrade() -> None:
    op.drop_index("ix_connectors_provider", table_name="connectors")
    op.drop_index("ix_connectors_user_id", table_name="connectors")
    op.drop_table("connectors")
