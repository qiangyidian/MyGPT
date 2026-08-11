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
    artifacts,
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
from app.observability import (
    bind_correlation_id,
    clear_correlation_id,
    get_correlation_id,
    new_correlation_id,
)


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
    # boot; no-op when no servers are configured). The static servers come from
    # the MCP_SERVERS setting (a JSON array); the live registry is published as
    # a process singleton so both runtimes merge its tools into their per-run
    # ToolRegistry via merge_mcp_tools().
    from app.agents.mcp_client import (
        McpClientRegistry,
        build_static_configs,
        set_live_mcp_registry,
    )
    mcp_registry = McpClientRegistry(build_static_configs(settings.MCP_SERVERS))
    app.state.mcp_registry = mcp_registry
    try:
        await mcp_registry.connect_all()
    except Exception:  # noqa: BLE001 — MCP must never block app boot
        logging.getLogger(__name__).warning(
            "mcp connect_all failed at boot; continuing", exc_info=True
        )
    # Publish the singleton regardless of connect outcome: merge_mcp_tools is a
    # no-op when the registry is empty/disconnected, so this never breaks a turn.
    set_live_mcp_registry(mcp_registry)
    try:
        yield
    finally:
        await approval_bus.stop()
        # Close MCP sessions so their subprocesses/HTTP pools don't leak.
        try:
            await mcp_registry.disconnect_all()
        except Exception:  # noqa: BLE001
            pass
        set_live_mcp_registry(None)
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
    # Correlation-ID middleware: mint (or accept an inbound X-Correlation-Id),
    # bind it into the observability contextvar so it propagates into every
    # structured log line + trace span for the request, and echo it back on the
    # response so a client/operator can correlate across the stack.
    app.add_middleware(CorrelationIdMiddleware)

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
    app.include_router(artifacts.router)

    @app.get("/health", tags=["health"])
    async def health() -> JSONResponse:
        # Lenient liveness probe: pings DB (hard dep) + Redis + Qdrant
        # concurrently. 200 when the DB is up (the app can serve degraded
        # without Redis/Qdrant); 503 only when the DB itself is down.
        from app.core.health import check_health
        result = await check_health()
        status_code = 200 if result["status"] == "ok" else 503
        return JSONResponse(result, status_code=status_code)

    @app.get("/ready", tags=["health"])
    async def ready() -> JSONResponse:
        # STRICT readiness gate (Task 11): 200 only when ALL components pass
        # (DB + migration head + Redis + Qdrant version compat + storage
        # writable + runner available + eligible chat model). 503 otherwise,
        # with a structured per-component body. This is the LB/k8s signal;
        # boot itself never requires readiness.
        from app.core.health import check_readiness
        result = await check_readiness()
        status_code = 200 if result["status"] == "ready" else 503
        return JSONResponse(result, status_code=status_code)

    return app


class CorrelationIdMiddleware:
    """ASGI middleware that mints/propagates a per-request correlation id.

    Reads an inbound ``X-Correlation-Id`` (or mints one), binds it into the
    observability contextvar (so it lands in every structured log + span), and
    echoes it back on the response as ``X-Correlation-Id``. Implemented as raw
    ASGI (not BaseHTTPMiddleware) so it stays cheap on the hot path and survives
    SSE / streaming responses without buffering.
    """

    _HEADER = "x-correlation-id"

    def __init__(self, app):  # noqa: ANN001
        self.app = app

    async def __call__(self, scope, receive, send):  # noqa: ANN001
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        inbound = None
        for k, v in scope.get("headers", []):
            if k.decode("latin-1").lower() == self._HEADER:
                inbound = v.decode("latin-1")
                break
        cid = inbound or new_correlation_id()
        bind_correlation_id(cid)

        async def _send(message):  # noqa: ANN202
            # Echo the correlation id on the response headers.
            if message["type"] == "http.response.start":
                headers = list(message.get("headers") or [])
                headers.append(
                    (self._HEADER.encode("latin-1"), cid.encode("latin-1"))
                )
                message["headers"] = headers
            await send(message)

        try:
            await self.app(scope, receive, _send)
        finally:
            clear_correlation_id()


app = create_app()
