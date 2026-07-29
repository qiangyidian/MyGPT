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


engine = create_async_engine(
    settings.DATABASE_URL,
    echo=False,
    pool_pre_ping=True,
    future=True,
)

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
