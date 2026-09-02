"""Async database engine, session factory, declarative Base, and the FastAPI dependency."""
from __future__ import annotations

from collections.abc import AsyncGenerator

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase

from app.core.config import get_settings

settings = get_settings()


# When running on SQLite (dev / demo / tests without the conftest shim), the ORM
# models use PostgreSQL-only types (UUID, JSONB). Teach SQLite how to render
# them so ``create_all`` succeeds and dev-on-sqlite works out of the box. These
# compilers are no-ops on Postgres (they only apply to the sqlite dialect).
if settings.DATABASE_URL.startswith("sqlite"):
    from sqlalchemy.dialects.postgresql import JSONB, UUID
    from sqlalchemy.ext.compiler import compiles

    @compiles(UUID, "sqlite")
    def _compile_uuid_sqlite(type_, compiler, **kw):  # noqa: ANN001
        return "VARCHAR(36)"

    @compiles(JSONB, "sqlite")
    def _compile_jsonb_sqlite(type_, compiler, **kw):  # noqa: ANN001
        return "JSON"


# Pool sizing: a streaming chat turn holds its request session for the whole
# stream (agent runs can run up to the Hermes budget of 900s), so the asyncpg
# defaults (pool_size=5, max_overflow=10) are starved by ~15 concurrent turns.
# Explicit headroom keeps short API calls from queueing behind streams.
# SQLite (dev/tests) uses a single connection — pass pool args only for server DBs.
_engine_kwargs: dict = {
    "echo": False,
    "pool_pre_ping": True,
    "future": True,
}
if not settings.DATABASE_URL.startswith("sqlite"):
    _engine_kwargs.update(
        pool_size=max(10, int(getattr(settings, "DB_POOL_SIZE", 20))),
        max_overflow=int(getattr(settings, "DB_MAX_OVERFLOW", 20)),
        pool_recycle=1800,  # recycle before DB-side idle timeouts kill conns
    )

engine = create_async_engine(settings.DATABASE_URL, **_engine_kwargs)

AsyncSessionLocal = async_sessionmaker(
    bind=engine, class_=AsyncSession, expire_on_commit=False, autoflush=False
)


class Base(DeclarativeBase):
    """Declarative base shared by all ORM models."""


async def get_db() -> AsyncGenerator[AsyncSession, None]:
    """FastAPI dependency: yields a session and rolls back on exception."""
    async with AsyncSessionLocal() as session:
        try:
            yield session
        except Exception:
            await session.rollback()
            raise
