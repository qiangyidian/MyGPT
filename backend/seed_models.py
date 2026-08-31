"""Seed system-wide model configs into the database.

Run from the backend/ directory so backend/.env (DB url + FERNET_KEY) is loaded:

    set SEED_API_KEY=sk-xxxx
    python seed_models.py

The API key is read from the SEED_API_KEY env var (never hardcoded here) and
stored encrypted in the DB. Re-runnable: existing rows are updated in place,
new rows are inserted. Edit the MODELS list below to add more.
"""
from __future__ import annotations

import asyncio
import os

from sqlalchemy import select

from app.core.security import encrypt_secret
from app.db import AsyncSessionLocal
from app.models import ModelConfig

BASE_URL = os.environ.get("SEED_BASE_URL", "http://139.155.139.221:8080/v1")
API_KEY = os.environ.get("SEED_API_KEY", "")

# (display name, model id). Add more here as needed.
MODELS: list[dict[str, str]] = [
    {"name": "GLM-5.2", "model_name": "glm-5.2"},
    {"name": "DeepSeek V4 Pro", "model_name": "deepseek-v4-pro"},
]


async def main() -> None:
    if not API_KEY:
        raise SystemExit("SEED_API_KEY env var is required")

    async with AsyncSessionLocal() as db:
        for m in MODELS:
            existing = (
                await db.execute(
                    select(ModelConfig).where(
                        ModelConfig.user_id.is_(None),
                        ModelConfig.provider == "openai-compatible",
                        ModelConfig.model_name == m["model_name"],
                    )
                )
            ).scalar_one_or_none()

            if existing is not None:
                existing.api_base_url = BASE_URL
                existing.api_key_encrypted = encrypt_secret(API_KEY)
                existing.supports_stream = True
                existing.supports_tools = True
                print(f"updated: {m['name']}  ({m['model_name']})")
            else:
                db.add(
                    ModelConfig(
                        user_id=None,
                        name=m["name"],
                        provider="openai-compatible",
                        api_base_url=BASE_URL,
                        api_key_encrypted=encrypt_secret(API_KEY),
                        model_name=m["model_name"],
                        supports_stream=True,
                        supports_tools=True,
                        max_context_tokens=131072,
                        max_tokens=8192,
                        temperature=0.7,
                        top_p=1.0,
                    )
                )
                print(f"created: {m['name']}  ({m['model_name']})")
        await db.commit()
    print("done.")


if __name__ == "__main__":
    asyncio.run(main())
