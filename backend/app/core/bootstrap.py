"""Application bootstrap: schema creation + idempotent seed data.

``init_db(app)`` runs during lifespan startup. It is deliberately defensive and
idempotent so re-running on an existing DB never duplicates seed rows or wipes
user data:

* optionally creates tables (dev convenience; prod uses Alembic migrations);
* seeds the bootstrap admin user from ``ADMIN_*`` env if missing;
* seeds a system-wide default chat ``ModelConfig`` from ``MODEL_*`` env;
* seeds a ``Mock (演示)`` provider so the app works with zero external model;
* seeds a default embedding ``ModelConfig`` from ``EMBEDDING_*`` env.

All seed rows are system-wide (``user_id=None``).
"""
from __future__ import annotations

from fastapi import FastAPI
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.core.config import get_settings
from app.core.logging import get_logger
from app.core.security import encrypt_secret, hash_password
from app.db import AsyncSessionLocal, Base
from app.models import ModelConfig, User

# Make sure every model is imported so Base.metadata sees all tables
# (create_all only emits tables already registered on the metadata).
import app.models  # noqa: F401  (side effect: register models)

logger = get_logger(__name__)


async def _create_tables() -> None:
    from app.db import engine

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


async def _seed_admin(session) -> None:
    settings = get_settings()
    stmt = select(User).where(
        (User.email == settings.ADMIN_EMAIL) | (User.username == settings.ADMIN_USERNAME)
    )
    existing = (await session.execute(stmt)).scalar_one_or_none()
    if existing is not None:
        return

    admin = User(
        email=settings.ADMIN_EMAIL,
        username=settings.ADMIN_USERNAME,
        password_hash=hash_password(settings.ADMIN_PASSWORD),
        role="admin",
        is_active=True,
    )
    session.add(admin)
    try:
        await session.commit()
    except IntegrityError:
        # Concurrent start with another worker won the race; rollback + ignore.
        await session.rollback()
        return
    logger.info("Seeded admin user", email=settings.ADMIN_EMAIL)


async def _seed_model(
    session,
    *,
    name: str,
    provider: str,
    base_url: str,
    api_key: str,
    model_name: str,
    is_embedding: bool = False,
    embedding_model_name: str | None = None,
    supports_stream: bool = True,
    supports_tools: bool = False,
) -> ModelConfig | None:
    """Insert a system-wide config if a row with the same (provider, model_name,
    is_embedding) signature doesn't already exist. Returns the row or None."""
    stmt = select(ModelConfig).where(
        ModelConfig.user_id.is_(None),
        ModelConfig.provider == provider,
        ModelConfig.model_name == model_name,
        ModelConfig.is_embedding.is_(is_embedding),
    )
    existing = (await session.execute(stmt)).scalar_one_or_none()
    if existing is not None:
        return None

    cfg = ModelConfig(
        user_id=None,
        name=name,
        provider=provider,
        api_base_url=base_url,
        api_key_encrypted=encrypt_secret(api_key) if api_key else "",
        model_name=model_name,
        embedding_model_name=embedding_model_name,
        supports_stream=supports_stream,
        supports_tools=supports_tools,
        is_embedding=is_embedding,
    )
    session.add(cfg)
    try:
        await session.commit()
    except IntegrityError:
        await session.rollback()
        return None
    return cfg


async def _seed_default_models(session) -> None:
    settings = get_settings()

    chat = await _seed_model(
        session,
        name="Default Chat Model",
        provider=settings.MODEL_PROVIDER,
        base_url=settings.MODEL_API_BASE_URL,
        api_key=settings.MODEL_API_KEY,
        model_name=settings.MODEL_NAME,
        is_embedding=False,
        supports_stream=True,
    )
    if chat is not None:
        logger.info("Seeded default chat model", model=settings.MODEL_NAME)

    # Mock provider: zero-dependency demo model so the app is usable out of the
    # box even without an LLM endpoint configured.
    mock = await _seed_model(
        session,
        name="Mock (演示)",
        provider="mock",
        base_url="http://localhost/v1",
        api_key="",
        model_name="mock-model",
        is_embedding=False,
        supports_stream=True,
        supports_tools=False,
    )
    if mock is not None:
        logger.info("Seeded Mock provider config")

    embedding = await _seed_model(
        session,
        name="Default Embedding Model",
        provider=settings.MODEL_PROVIDER,
        base_url=settings.EMBEDDING_API_BASE_URL,
        api_key=settings.EMBEDDING_API_KEY,
        model_name=settings.EMBEDDING_MODEL_NAME,
        is_embedding=True,
        supports_stream=False,
    )
    if embedding is not None:
        logger.info("Seeded default embedding model", model=settings.EMBEDDING_MODEL_NAME)


async def init_db(app: FastAPI) -> None:
    """Run schema creation + seeds. Safe to call on every startup."""
    settings = get_settings()

    if settings.AUTO_CREATE_TABLES:
        try:
            await _create_tables()
        except Exception:
            # Don't silently swallow schema-creation failures — create_all can't
            # ALTER existing tables to add new columns, so a masked error here
            # surfaces later as confusing 500s on endpoints that SELECT the
            # missing column. Log loudly; the app still starts.
            logger.exception("create_all failed; schema may be incomplete")

    async with AsyncSessionLocal() as session:
        await _seed_admin(session)
        await _seed_default_models(session)
