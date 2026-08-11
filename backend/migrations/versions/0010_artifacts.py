"""artifacts: first-class authorized blob references (Task 10).

Revision ID: 0010_artifacts
Revises: 0009_connectors
Create Date: 2026-08-11

Adds ONE additive table — ``artifacts`` — for first-class, tenant-scoped,
checksummed references to blobs produced or uploaded by the platform (tool
outputs, code bundles, screenshots, audio, images, generated documents, user
uploads). Tenant isolation is enforced by ``owner_id`` on every query
(ArtifactService); the opaque ``storage_key`` is never exposed to the model or
the client. sha256/size/media_type are computed at create time and the checksum
is re-verified on read.

On a baseline-created DB (create_all already ran against the full metadata
incl. this table) the upgrade is a guarded no-op, matching the 0001/0003/
0007/0008/0009 convention.
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0010_artifacts"
down_revision: Union[str, None] = "0009_connectors"
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
    if "artifacts" in existing:
        return

    op.create_table(
        "artifacts",
        sa.Column("id", _uuid_type(), primary_key=True),
        sa.Column("owner_id", _uuid_type(), nullable=False),
        sa.Column("run_id", _uuid_type(), nullable=True),
        sa.Column("step_id", _uuid_type(), nullable=True),
        # sha256 hex (64 chars).
        sa.Column("checksum", sa.String(length=64), nullable=False),
        sa.Column("media_type", sa.String(length=255), nullable=False, server_default="application/octet-stream"),
        sa.Column("size", sa.BigInteger(), nullable=False, server_default="0"),
        # Opaque storage backend key — NEVER exposed to the model/client.
        sa.Column("storage_key", sa.String(length=512), nullable=False),
        sa.Column("filename", sa.String(length=255), nullable=False, server_default="artifact"),
        # tool_output | spill | upload | generation
        sa.Column("source", sa.String(length=32), nullable=False, server_default="upload"),
        sa.Column("generator", _json_type(), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("retention_policy", sa.String(length=64), nullable=True),
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
        sa.ForeignKeyConstraint(["owner_id"], ["users.id"], ondelete="CASCADE"),
    )
    op.create_index("ix_artifacts_owner_id", "artifacts", ["owner_id"])
    op.create_index("ix_artifacts_run_id", "artifacts", ["run_id"])


def downgrade() -> None:
    op.drop_index("ix_artifacts_run_id", table_name="artifacts")
    op.drop_index("ix_artifacts_owner_id", table_name="artifacts")
    op.drop_table("artifacts")
