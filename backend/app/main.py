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

import asyncio
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from app.api import (
    admin,
    admin_redeem,
    admin_runtime,
    agent_runs,
    artifacts,
    auth,
    chat,
    chat_attachments,
    connectors,
    conversations,
    credits,
    documents,
    knowledge_bases,
    memories,
    mentions,
    messages,
    message_versions,
    projects,
    prompts,
    retrieval,
    speech,
    tools,
    wechat,
)
from app.api import (
    models as models_api,
)
from app.core.bootstrap import init_db
from app.core.config import get_settings
from app.core.exceptions import register_exception_handlers
from app.core.logging import configure_logging
from app.observability import (
    bind_correlation_id,
    clear_correlation_id,
    new_correlation_id,
)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Startup: logging + DB; shutdown: nothing bespoke yet."""
    settings = get_settings()
    configure_logging("DEBUG" if settings.is_dev else "INFO")
    # One-line agent-runtime boot state (flag + dispatch mode only — NO crewai
    # import here: importing crewai blocks the event loop for seconds and, on
    # a small VPS, can push the boot past the deploy health-check window and
    # trigger a rollback loop. Importability is checked lazily on first
    # multi-agent turn and on demand via /api/admin/agent-runtime.)
    logging.getLogger(__name__).info(
        "agent runtime at boot: CREWAI_ENABLED=%s background_worker=%s",
        settings.CREWAI_ENABLED,
        settings.BACKGROUND_WORKER,
    )
    # Sandbox boot state, from the ONE runner construction point (never raises):
    # which mode, which permission profile, and whether that combination can
    # actually exec. A prod box left on SANDBOX_MODE=local while claiming code
    # execution is the misconfiguration this line exists to make visible — the
    # same verdict GET /ready reports.
    from app.agents.sandbox.factory import runner_descriptor

    _sandbox = runner_descriptor()
    logging.getLogger(__name__).log(
        logging.INFO if _sandbox.get("ok") else logging.ERROR,
        "sandbox runner at boot: %s",
        _sandbox,
    )
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
    except Exception:
        logging.getLogger(__name__).warning(
            "mcp connect_all failed at boot; continuing", exc_info=True
        )
    # Publish the singleton regardless of connect outcome: merge_mcp_tools is a
    # no-op when the registry is empty/disconnected, so this never breaks a turn.
    set_live_mcp_registry(mcp_registry)
    # Chat attachments still parse as fire-and-forget tasks, so a restart leaves
    # rows stuck in ``pending``/``parsing``; this sweeper re-enqueues them. It is
    # leader-gated on its own advisory lock (same mechanism and default as the
    # retention sweeper below), so scaling this API to N replicas yields one
    # sweeper plus N-1 standbys instead of N processes flipping the same expired
    # rows every interval.
    #
    # There is deliberately no separate boot-time pass here, unlike earlier
    # versions of this block: a rollout starts every replica at the same instant,
    # so "one bounded pass per process start" was N concurrent re-parses of the
    # same attachment (the in-process task dedup cannot see across processes).
    # The gate's first tick fires the moment the loop starts, so the previous
    # deployment's leftovers still get healed at boot — exactly once.
    from app.db import AsyncSessionLocal
    from app.services.stale_job_recovery import StaleJobSweeper

    stale_sweeper = StaleJobSweeper(AsyncSessionLocal)
    stale_sweeper.start()
    app.state.stale_job_sweeper = stale_sweeper
    # Knowledge-base ingestion: a durable queue whose rows are claimed under a
    # lease, so an upload that outlives its process is picked up by whoever next
    # polls and a permanently bad document stops being retried. Running the loop
    # here means a single-process deployment indexes without a dedicated worker;
    # a worker process may run the same loop concurrently (the claim is atomic).
    from app.services import ingestion_queue

    ingestion_worker = await ingestion_queue.build_worker()
    ingestion_queue.set_active_worker(ingestion_worker)
    ingestion_worker.start()
    app.state.ingestion_worker = ingestion_worker
    # Data-retention pass (audit TTL, terminal run-event pruning, orphan
    # upload cleanup) — data previously grew without bound.
    from app.services.retention import RetentionSweeper

    retention_sweeper = RetentionSweeper(AsyncSessionLocal)
    retention_sweeper.start()
    app.state.retention_sweeper = retention_sweeper
    # Queue-depth gauges for /metrics + Alertmanager (finding 38). Runs in the
    # API process on purpose: the worker exposes no scrape surface, so a queue
    # metric only it observed would be invisible to a scrape. Every replica
    # samples the same Redis, and a gauge is absolute, so replicas agree instead
    # of fighting (no leader needed).
    metric_tasks: list[asyncio.Task] = []
    if get_settings().PROMETHEUS_ENABLED:
        from app.agents.workflow.queue import run_queue_depth_poller
        from app.services.credit_service import run_credit_pool_poller

        # Queue depth (capacity changes fast, one sample is a Redis round-trip);
        # the credit pool less often (an aggregate scan, and the real-time
        # "someone hit zero" signal is the rejection counter instead). Both
        # cadences are settings, not literals: the queue one is what an operator
        # shortens while debugging a backlog, and each sample costs a round trip
        # against a Redis/DB that is already loaded.
        settings = get_settings()
        metric_tasks = [
            asyncio.create_task(
                run_queue_depth_poller(interval=settings.RUN_QUEUE_DEPTH_POLL_SECONDS)
            ),
            asyncio.create_task(
                run_credit_pool_poller(interval=settings.CREDIT_POOL_POLL_SECONDS)
            ),
        ]
    app.state.metric_pollers = metric_tasks
    try:
        yield
    finally:
        for task in metric_tasks:
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass
        ingestion_queue.set_active_worker(None)
        await ingestion_worker.stop()
        await retention_sweeper.stop()
        await stale_sweeper.stop()
        await approval_bus.stop()
        # Close MCP sessions so their subprocesses/HTTP pools don't leak.
        try:
            await mcp_registry.disconnect_all()
        except Exception:
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
    # Docs off in production (see ``Settings.docs_enabled``): the trio of
    # /docs, /redoc and /openapi.json publishes the entire route surface.
    docs_on = settings.docs_enabled
    app = FastAPI(
        title="AI Chat Platform",
        description="Multi-user AI chat with RAG, tool calling, and an admin console.",
        version="1.0.0",
        lifespan=lifespan,
        docs_url="/docs" if docs_on else None,
        redoc_url="/redoc" if docs_on else None,
        openapi_url="/openapi.json" if docs_on else None,
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
    # Versioned API alias: /api/v1/* is served by the same routers as /api/*.
    from app.core.middleware import ApiV1AliasMiddleware
    app.add_middleware(ApiV1AliasMiddleware)
    # Baseline security response headers (CSP/HSTS/X-Frame-Options/…).
    from app.core.middleware import RequestMetricsMiddleware, SecurityHeadersMiddleware
    app.add_middleware(SecurityHeadersMiddleware)
    # RED metrics per route template (requests / errors / latency) — exposed
    # via GET /metrics when PROMETHEUS_ENABLED. Without this the process had
    # embedded metric instrumentation but no request-level visibility at all.
    app.add_middleware(RequestMetricsMiddleware)
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
    app.include_router(message_versions.router)
    app.include_router(models_api.router)
    app.include_router(knowledge_bases.router)
    app.include_router(documents.router)
    app.include_router(retrieval.router)
    # @文件 / @知识库 内联引用的候选项（composer 的类型预测数据源）。
    app.include_router(mentions.router)
    app.include_router(prompts.router)
    app.include_router(tools.router)
    app.include_router(admin.router)
    app.include_router(agent_runs.router)
    app.include_router(projects.router)
    app.include_router(memories.router)
    app.include_router(memories.user_router)
    app.include_router(connectors.router)
    app.include_router(artifacts.router)
    app.include_router(wechat.router)
    app.include_router(credits.router)
    app.include_router(credits.admin_router)
    # 语音输入/播报：默认关闭（SPEECH_ENABLED=false），能力探测见 /api/speech/capabilities。
    app.include_router(speech.router)
    # 管理后台的观测与运营面（都是 admin-only，鉴权在每个路由的依赖里）。
    app.include_router(admin_runtime.router)
    app.include_router(admin_redeem.router)

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

    @app.get("/metrics", tags=["health"], include_in_schema=False)
    async def metrics(request: Request) -> Response:
        # Prometheus scrape endpoint. The app already registers counters and
        # histograms (LLM latency/tokens, tool calls, queue depth, HTTP RED)
        # — without this route they were instrumented but unreachable, so the
        # production deployment was effectively a black box. Gated on
        # PROMETHEUS_ENABLED (default off) so the exposition surface only
        # exists when an operator actually scrapes it.
        #
        # Authentication (finding 38): the exposition leaks route templates,
        # provider/model label values and queue depth — a roadmap for an
        # attacker, and it is served on the same public port as the app. So a
        # bearer token is required whenever METRICS_TOKEN is set; config.py
        # refuses to boot a non-dev deployment with PROMETHEUS_ENABLED on and
        # METRICS_TOKEN empty, which means the only way to get an unauthenticated
        # /metrics is to be in dev. Scrape configs carry it as
        # ``authorization: credentials`` from a secret, not a URL query (query
        # strings land in access logs and proxy caches).
        from prometheus_client import CONTENT_TYPE_LATEST, generate_latest

        if not get_settings().PROMETHEUS_ENABLED:
            raise HTTPException(status_code=404, detail="Not Found")
        import secrets as _secrets

        expected = get_settings().METRICS_TOKEN
        if expected:
            header = request.headers.get("authorization", "")
            scheme, _, token = header.partition(" ")
            if scheme.lower() != "bearer" or not _secrets.compare_digest(
                token.strip(), expected
            ):
                # 404, not 401: an unauthenticated probe must not learn that a
                # metrics surface exists here at all.
                raise HTTPException(status_code=404, detail="Not Found")
        return Response(
            content=generate_latest(),
            media_type=CONTENT_TYPE_LATEST,
        )

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

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
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

        async def _send(message):
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
