"""initial schema baseline — create every table from the ORM metadata.

Revision ID: 0000_initial
Revises:
Create Date: 2026-08-09

This is the root migration. It materializes the full current schema in one step
from ``Base.metadata`` so a fresh production database can be brought up with
``alembic upgrade head``. Previously the chain was additive-only and assumed the
base tables already existed (created via ``create_all``), and env.py's sync URL
requires psycopg2 which was not declared — both are fixed here (psycopg2-binary
is now in requirements.txt).

The downstream additive migrations (0001-0004) each guard their ``upgrade()``
with an inspector ``has_column``/``has_table`` check so they no-op cleanly on a
baseline-created DB while still running normally on a legacy DB upgrading through
the chain.
"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op

# Importing app.db registers the SQLite UUID/JSONB compile shim (so create_all
# works on sqlite too), and importing app.models registers every model on
# Base.metadata. env.py has already put backend/ on sys.path.
import app.db  # noqa: F401
import app.models  # noqa: F401
from app.db import Base

revision: str = "0000_initial"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    Base.metadata.create_all(op.get_bind())


def downgrade() -> None:
    Base.metadata.drop_all(op.get_bind())
