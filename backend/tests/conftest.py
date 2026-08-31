"""Shared pytest fixtures.

Test isolation strategy:
  * Override ``DATABASE_URL`` to an in-memory SQLite (aiosqlite) database BEFORE
    the app modules import their engine, so the whole stack runs without
    postgres / redis / qdrant.
  * The ORM models use PostgreSQL-only column types (UUID, JSONB). With SQLite
    we transparently remap those to SQLite-friendly equivalents via SQLAlchemy
    compile-time event listeners so ``create_all`` works on the in-memory DB.
  * ``get_db`` is overridden on the FastAPI app so every request uses the test
    session bound to the in-memory engine.
  * A seed user is created so tests can mint an access token without hitting the
    register endpoint first (each test can still register its own users).

Requires (add to backend requirements): ``aiosqlite``, ``httpx``.
"""
from __future__ import annotations

import asyncio
import os
import sys
import uuid
from typing import AsyncIterator

# --------------------------------------------------------------------------- #
# 1. Force the test DATABASE_URL + a stable FERNET key BEFORE importing the app.
# --------------------------------------------------------------------------- #
# A fresh in-memory database per process. ``:memory:`` shared across the one
# connection pool we build below keeps all sessions on the same physical DB.
os.environ["DATABASE_URL"] = "sqlite+aiosqlite:///:memory:"
os.environ["ENV"] = "test"
os.environ["AUTO_CREATE_TABLES"] = "false"
# Tests exercise the INLINE executor; durable dispatch has its own dedicated
# suites. Pinning this also isolates tests from a production .env that sets
# BACKGROUND_WORKER=redis (the chat API would otherwise route test traffic
# to a worker that doesn't exist in the test process).
os.environ["BACKGROUND_WORKER"] = "inprocess"
# Redis is not available in tests -> auth_service degrades to in-memory set.
os.environ["REDIS_URL"] = "redis://localhost:6399/0"
# CrewAI (Phase 1+) uses an internal memory cache + telemetry that try to talk
# to Redis/analytics endpoints on Crew construction. In tests there is no
# Redis, so opt out of telemetry and disable CrewAI's short-term memory path.
os.environ["CREWAI_TELEMETRY_OPT_OUT"] = "True"
os.environ["OTEL_SDK_DISABLED"] = "true"
os.environ["CREWAI_DISABLE_MEMORY"] = "true"
# A fixed Fernet key (generated once, base64 of 32 url-safe bytes) so
# encrypt/decrypt round-trips deterministically within a test run.
os.environ.setdefault(
    "FERNET_KEY",
    "ZmDfcTF7_60GrrY167zsiPd67pEvs0aGOv2oasOM1Pg=",
)

# Make sure ``backend`` is importable as the repo root for ``app``.
_BACKEND_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if _BACKEND_DIR not in sys.path:
    sys.path.insert(0, _BACKEND_DIR)

# --------------------------------------------------------------------------- #
# 2. Import app modules (after env is set) + wire SQLite dialect compatibility.
# --------------------------------------------------------------------------- #
import pytest
import pytest_asyncio
from sqlalchemy import event
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.engine import Engine
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.core.config import get_settings  # noqa: E402
from app.core.security import hash_password  # noqa: E402
from app.db import Base  # noqa: E402
from app.db import get_db as production_get_db  # noqa: E402
from app.models import *  # noqa: E402,F401,F403  -- ensure all models register on metadata


def _install_global_uuid_compilation() -> None:
    """Fallback: also teach the PG UUID/JSONB types to compile for SQLite.

    The per-table column remap above is the primary mechanism, but some flows
    access these types directly; registering a SQLite compiler keeps them safe.
    """
    from sqlalchemy.ext.compiler import compiles

    @compiles(UUID, "sqlite")
    def _compile_uuid_sqlite(type_, compiler, **kw):  # noqa: ANN001
        return "VARCHAR(36)"

    @compiles(JSONB, "sqlite")
    def _compile_jsonb_sqlite(type_, compiler, **kw):  # noqa: ANN001
        return "JSON"


# --------------------------------------------------------------------------- #
# 3. Test engine + session + dependency override.
# --------------------------------------------------------------------------- #
# Re-create settings so it picks up the env override (get_settings is lru_cached;
# clear it first so the in-memory URL wins).
get_settings.cache_clear()
_settings = get_settings()
assert _settings.DATABASE_URL.startswith("sqlite"), "DATABASE_URL must be sqlite in tests"

test_engine = create_async_engine(
    _settings.DATABASE_URL,
    echo=False,
    future=True,
    # For in-memory SQLite, keep a single connection alive for the whole engine
    # lifetime so all sessions share the same physical database.
    poolclass=__import__("sqlalchemy.pool", fromlist=["StaticPool"]).StaticPool,
    connect_args={"check_same_thread": False},
)

TestSessionLocal = async_sessionmaker(
    bind=test_engine, class_=AsyncSession, expire_on_commit=False, autoflush=False
)


async def _init_db() -> None:
    """Create all tables on the in-memory SQLite database."""
    _install_global_uuid_compilation()
    async with test_engine.begin() as conn:
        # Remap PG-only types per-table just before they are emitted.
        await conn.run_sync(Base.metadata.create_all)


async def _override_get_db() -> AsyncIterator[AsyncSession]:
    """Replacement dependency bound to the test session factory."""
    async with TestSessionLocal() as session:
        try:
            yield session
        except Exception:
            await session.rollback()
            raise


# Lazily-imported FastAPI app handle (the routers live in app.main, built by the
# router agent). We import inside the fixture so collection does not fail if the
# app module is momentarily absent during early development.
def _get_app():
    from app.main import app  # noqa: WPS433  -- late import on purpose

    return app


# --------------------------------------------------------------------------- #
# 4. Pytest hooks / fixtures.
# --------------------------------------------------------------------------- #
@pytest.fixture(scope="session", autouse=True)
def _crewai_in_process_locks():
    """Force CrewAI's lock backend to an in-process one in tests.

    CrewAI's task-output storage locks via Redis when ``REDIS_URL`` is set, but
    tests have no Redis. Swap the backend for a trivial in-process lock so
    building/running a Crew never blocks on a dead Redis connection.
    """
    from contextlib import contextmanager
    import threading

    _lock = threading.Lock()

    @contextmanager
    def _inproc_lock(name, timeout=120, **kwargs):
        import time

        deadline = time.monotonic() + timeout
        while not _lock.acquire(False):
            if time.monotonic() > deadline:
                raise TimeoutError(f"lock {name} timeout")
            time.sleep(0.01)
        try:
            yield
        finally:
            _lock.release()

    try:
        from crewai_core.lock_store import set_lock_backend

        set_lock_backend(_inproc_lock)
    except Exception:
        pass  # crewai not installed -> nothing to do
    yield


@pytest.fixture(scope="session")
def event_loop():
    """One event loop for the whole session so session-scoped async fixtures work."""
    loop = asyncio.new_event_loop()
    yield loop
    loop.close()


@pytest_asyncio.fixture(scope="session")
async def seeded_db() -> AsyncSession:
    """Create tables once for the session and seed an admin + a normal user."""
    await _init_db()

    async with TestSessionLocal() as session:
        from app.models import User

        admin = User(
            email="admin-test@example.com",
            username="admin-test",
            password_hash=hash_password("AdminPass123"),
            role="admin",
            is_active=True,
        )
        # ID fixed so tests can reference the seeded user without a DB lookup.
        seeded = User(
            id=uuid.UUID("00000000-0000-0000-0000-000000000001"),
            email="seeded@example.com",
            username="seeded",
            password_hash=hash_password("SeededPass123"),
            role="user",
            is_active=True,
        )
        session.add_all([admin, seeded])
        await session.commit()
        await session.refresh(seeded)
    yield None  # tables + seed persist for the whole session (StaticPool)


@pytest_asyncio.fixture
async def db_session(seeded_db) -> AsyncIterator[AsyncSession]:
    """Per-test session sharing the seeded in-memory database."""
    async with TestSessionLocal() as session:
        yield session


@pytest_asyncio.fixture
async def client(seeded_db):
    """An ``httpx.AsyncClient`` bound to the FastAPI app via ASGI transport.

    ``get_db`` is overridden so request handlers use the test session factory,
    which is itself bound to the in-memory engine.
    """
    import httpx

    app = _get_app()
    from app.services.chat_service import chat_service

    previous_persistence_factory = chat_service._persistence_session_factory
    chat_service._persistence_session_factory = TestSessionLocal
    app.dependency_overrides[production_get_db] = _override_get_db
    try:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport, base_url="http://testserver"
        ) as ac:
            yield ac
    finally:
        chat_service._persistence_session_factory = previous_persistence_factory
        app.dependency_overrides.pop(production_get_db, None)


# --------------------------------------------------------------------------- #
# 5. Helper: mint an access token for the seeded user.
# --------------------------------------------------------------------------- #
def get_access_token(user_id: str | uuid.UUID | None = None) -> str:
    """Return a signed access JWT for the seeded (or given) user.

    Uses the same ``create_access_token`` helper the auth flow uses, so the token
    is accepted by ``get_current_user``.
    """
    from app.core.security import create_access_token

    subject = str(user_id) if user_id is not None else "00000000-0000-0000-0000-000000000001"
    return create_access_token(subject=subject)


def auth_headers(token: str | None = None) -> dict[str, str]:
    """Convenience: build the ``Authorization: Bearer ...`` header."""
    return {"Authorization": f"Bearer {token or get_access_token()}"}


@pytest_asyncio.fixture
def auth_token() -> str:
    """A valid access token for the seeded normal user."""
    return get_access_token()


@pytest_asyncio.fixture
def admin_token() -> str:
    """A valid access token for the seeded admin user (looked up dynamically)."""
    import asyncio as _asyncio

    async def _resolve_admin_id() -> str:
        from app.models import User

        async with TestSessionLocal() as session:
            from sqlalchemy import select

            row = await session.execute(
                select(User).where(User.email == "admin-test@example.com")
            )
            user = row.scalar_one()
            return str(user.id)

    admin_id = _asyncio.get_event_loop().run_until_complete(_resolve_admin_id())
    return get_access_token(admin_id)
