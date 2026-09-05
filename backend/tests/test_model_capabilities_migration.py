from __future__ import annotations

import os
import sqlite3
import subprocess
import sys
import uuid
from pathlib import Path

import sqlalchemy as sa

from app.core.bootstrap import _sync_columns
from app.models.model_config import ModelConfig

CAPABILITY_DEFAULTS = {
    "supports_parallel_tools": "0",
    "supports_audio_input": "0",
    "supports_audio_output": "0",
    "supports_image_generation": "0",
    "supports_structured_output": "0",
    "supports_reasoning_effort": "0",
    "output_token_parameter": "max_tokens",
}


def test_capability_columns_have_database_defaults():
    for name in CAPABILITY_DEFAULTS:
        assert ModelConfig.__table__.c[name].server_default is not None


def test_dev_schema_sync_adds_capabilities_with_non_null_defaults():
    engine = sa.create_engine("sqlite:///:memory:")
    with engine.begin() as conn:
        conn.exec_driver_sql("CREATE TABLE model_configs (id VARCHAR(36) PRIMARY KEY)")
        _sync_columns(conn)
        info = {
            row[1]: row
            for row in conn.exec_driver_sql("PRAGMA table_info(model_configs)")
        }

    for name in CAPABILITY_DEFAULTS:
        assert info[name][3] == 1
        assert info[name][4] is not None


def test_model_capability_migration_upgrades_legacy_rows(tmp_path: Path):
    backend_dir = Path(__file__).resolve().parents[1]
    database_path = tmp_path / "capabilities.sqlite3"
    env = {
        **os.environ,
        "DATABASE_URL": f"sqlite+aiosqlite:///{database_path.as_posix()}",
        "ENV": "test",
    }

    def alembic(*args: str) -> None:
        subprocess.run(
            [sys.executable, "-m", "alembic", *args],
            cwd=backend_dir,
            env=env,
            check=True,
            capture_output=True,
            text=True,
        )

    alembic("upgrade", "head")
    with sqlite3.connect(database_path) as conn:
        revision = conn.execute("SELECT version_num FROM alembic_version").fetchone()[0]
        from app.core.health import REPO_MIGRATION_HEAD  # dynamic, never hardcoded

        assert revision == REPO_MIGRATION_HEAD
        info = {row[1]: row for row in conn.execute("PRAGMA table_info(model_configs)")}
        for name in CAPABILITY_DEFAULTS:
            assert info[name][3] == 1  # NOT NULL
            assert info[name][4] is not None  # database default

        # Create an existing row, roll the capability revision back off it, and
        # verify re-upgrade backfills the row rather than leaving NULL values.
        conn.execute(
            """
            INSERT INTO model_configs (
                id, name, provider, api_base_url, api_key_encrypted, model_name,
                supports_stream, supports_tools, supports_vision,
                max_context_tokens, max_tokens, temperature, top_p, is_embedding,
                created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
            """,
            (
                str(uuid.uuid4()), "legacy", "mock", "http://localhost/v1", "",
                "mock-model", 1, 0, 0, 8192, 1024, 0.7, 1.0, 0,
            ),
        )
        conn.commit()

    alembic("downgrade", "0005_message_token_accounting")
    # Reproduce a database previously touched by the old dev schema-sync:
    # columns exist, but are nullable and all legacy-row values are NULL.
    with sqlite3.connect(database_path) as conn:
        for name in CAPABILITY_DEFAULTS:
            sql_type = "VARCHAR(32)" if name == "output_token_parameter" else "BOOLEAN"
            conn.execute(f'ALTER TABLE model_configs ADD COLUMN "{name}" {sql_type}')
        conn.commit()
    alembic("upgrade", "0006_model_capabilities")

    with sqlite3.connect(database_path) as conn:
        row = conn.execute(
            """
            SELECT supports_parallel_tools, supports_audio_input,
                   supports_audio_output, supports_image_generation,
                   supports_structured_output, supports_reasoning_effort,
                   output_token_parameter
            FROM model_configs WHERE name = 'legacy'
            """
        ).fetchone()
        assert row == (0, 0, 0, 0, 0, 0, "max_tokens")
        info = {row[1]: row for row in conn.execute("PRAGMA table_info(model_configs)")}
        assert all(info[name][3] == 1 for name in CAPABILITY_DEFAULTS)
