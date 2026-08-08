"""FastAPI application factory + lifespan wiring.

Single entrypoint: ``create_app()`` builds the configured app. ``app`` is created
at import time so ``uvicorn app.main:app`` works without ceremony.

Responsibilities (in order):
  1. configure structured logging;
  2. on startup -> create tables + seed data (``init_db``);
  3. register the global exception handlers (uniform JSON error envelope);
  4. add CORS using ``settings.cors_origins``;
  5. include every feature router under ``app.api``;
  6. expose ``GET /health``.

Nothing else is mounted here — static files, websockets, sub-apps all live in
their owning modules.
"""
from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api import (
    admin,
    agent_runs,
    auth,
    background_tasks,
    chat,
    chat_attachments,
    conversations,
    documents,
    knowledge_bases,
    memories,
    messages,
    models as models_api,
    projects,
    retrieval,
    tools,
)
from app.core.bootstrap import init_db
from app.core.config import get_settings
from app.core.exceptions import register_exception_handlers
from app.core.logging import configure_logging


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Startup: logging + DB; shutdown: nothing bespoke yet."""
    settings = get_settings()
    configure_logging("DEBUG" if settings.is_dev else "INFO")
    await init_db(app)
    # Start the cross-worker approval signal subscriber (no-op without Redis).
    from app.agents.approval_bus import approval_bus
    await approval_bus.start_subscriber()
    try:
        yield
    finally:
        await approval_bus.stop()
        # Close shared clients so their connection pools don't leak on reload /
        # graceful shutdown (each used to live for the process with no close).
        from app.rag.qdrant_store import close_vector_store
        await close_vector_store()
        from app.core.redis import close_redis
        await close_redis()


def create_app() -> FastAPI:
    settings = get_settings()
    app = FastAPI(
        title="AI Chat Platform",
        description="Multi-user AI chat with RAG, tool calling, and an admin console.",
        version="1.0.0",
        lifespan=lifespan,
    )

    # CORS: credentials=True so the httponly refresh cookie can be set/cleared
    # cross-origin from the frontend origin(s) in BACKEND_CORS_ORIGINS.
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    register_exception_handlers(app)

    # Each router owns its own ``/api/...`` prefix; include as-is.
    app.include_router(auth.router)
    app.include_router(conversations.router)
    app.include_router(chat.router)
    app.include_router(chat_attachments.router)
    app.include_router(messages.router)
    app.include_router(models_api.router)
    app.include_router(knowledge_bases.router)
    app.include_router(documents.router)
    app.include_router(retrieval.router)
    app.include_router(tools.router)
    app.include_router(admin.router)
    app.include_router(agent_runs.router)
    app.include_router(projects.router)
    app.include_router(memories.router)
    app.include_router(background_tasks.router)

    @app.get("/health", tags=["health"])
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    return app


app = create_app()
