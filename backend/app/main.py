"""FastAPI application factory + lifespan wiring.

Single entrypoint: ``create_app()`` builds the configured app. ``app`` is created
at import time so ``uvicorn app.main:app`` works without ceremony.
(nudge: reload to re-read .env)

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
from fastapi.middleware.gzip import GZipMiddleware
from starlette.responses import JSONResponse

from app.api import (
    admin,
    agent_runs,
    auth,
    background_tasks,
    chat,
    chat_attachments,
    connectors,
    conversations,
    documents,
    knowledge_bases,
    memories,
    messages,
    models as models_api,
    projects,
    retrieval,
    tools,
    usage,
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
    # Lazily connect configured MCP servers (failure-isolated: never crashes
    # boot; no-op when no servers are configured). The registry is attached to
    # app.state so the agent layer can route MCP tool calls through it.
    from app.agents.mcp_client import McpClientRegistry
    app.state.mcp_registry = McpClientRegistry(settings.mcp_servers if hasattr(settings, "mcp_servers") else [])
    try:
        await app.state.mcp_registry.connect_all()
    except Exception:  # noqa: BLE001 — MCP must never block app boot
        import logging
        logging.getLogger(__name__).warning("mcp connect_all failed at boot; continuing", exc_info=True)
    try:
        yield
    finally:
        await approval_bus.stop()
        # Close MCP sessions so their subprocesses/HTTP pools don't leak.
        registry = getattr(app.state, "mcp_registry", None)
        if registry is not None:
            try:
                await registry.disconnect_all()
            except Exception:  # noqa: BLE001
                pass
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
    # GZip compress large JSON / SSE-adjacent payloads (bounded bandwidth).
    app.add_middleware(GZipMiddleware, minimum_size=1024)
    # Baseline security response headers (CSP/HSTS/X-Frame-Options/…).
    from app.core.middleware import SecurityHeadersMiddleware
    app.add_middleware(SecurityHeadersMiddleware)

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
    app.include_router(memories.user_router)
    app.include_router(connectors.router)
    app.include_router(background_tasks.router)
    app.include_router(usage.router)

    @app.get("/health", tags=["health"])
    async def health() -> JSONResponse:
        # Real readiness probe: pings DB (hard dep) + Redis + Qdrant concurrently.
        # 200 when healthy, 503 when the DB (or all deps) are down so a
        # load-balancer / k8s readiness gate can pull the instance out.
        from app.core.health import check_health
        result = await check_health()
        status_code = 200 if result["status"] == "ok" else 503
        return JSONResponse(result, status_code=status_code)

    return app


app = create_app()
