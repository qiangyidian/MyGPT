"""Central configuration. All runtime knobs come from environment via pydantic-settings.

Nothing in the app should read os.environ directly — import `get_settings()` here.
"""
from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import List

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# Repo root (this file is backend/app/core/config.py → parents[3]). The config
# is loaded from the repo-root .env regardless of the current working directory:
# start.bat runs uvicorn from backend/, docker-compose from /app — both must see
# the SAME source for CREWAI_ENABLED / AGENT_DEMO_MODE. A CWD-local .env
# (backend/.env) is read too and takes per-key precedence, so a host-run dev DB
# URL (localhost) still overrides the docker service-name URL in the root .env.
_REPO_ROOT = Path(__file__).resolve().parents[3]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=(str(_REPO_ROOT / ".env"), ".env"),
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=True,
    )

    # ---- App ----
    ENV: str = "dev"
    AUTO_CREATE_TABLES: bool = True
    BACKEND_CORS_ORIGINS: str = "http://localhost:3000"

    # ---- Security ----
    JWT_SECRET: str = "please-change-this"
    JWT_ALGORITHM: str = "HS256"
    JWT_ACCESS_EXPIRE_MINUTES: int = 30
    JWT_REFRESH_EXPIRE_DAYS: int = 7
    FERNET_KEY: str = ""  # may be empty in dev (we generate one lazily, see security.py)

    # ---- Database ----
    DATABASE_URL: str = "postgresql+asyncpg://postgres:postgres@localhost:5432/ai_chat"
    POSTGRES_USER: str = "postgres"
    POSTGRES_PASSWORD: str = "postgres"
    POSTGRES_DB: str = "ai_chat"

    # ---- Redis ----
    REDIS_URL: str = "redis://localhost:6379/0"

    # ---- Vector DB ----
    QDRANT_URL: str = "http://localhost:6333"
    QDRANT_API_KEY: str = ""
    QDRANT_EMBEDDING_DIM: int = 1024

    # ---- Default model ----
    MODEL_PROVIDER: str = "openai-compatible"
    MODEL_API_BASE_URL: str = "http://localhost:8000/v1"
    MODEL_API_KEY: str = ""
    MODEL_NAME: str = "my-model"
    EMBEDDING_API_BASE_URL: str = "http://localhost:8000/v1"
    EMBEDDING_API_KEY: str = ""
    EMBEDDING_MODEL_NAME: str = "my-embedding-model"

    # ---- Storage ----
    STORAGE_BACKEND: str = "local"
    STORAGE_DIR: str = "./data/uploads"
    MAX_UPLOAD_MB: int = 20
    ALLOWED_UPLOAD_EXT: str = ".pdf,.docx,.doc,.txt,.md,.csv,.xlsx,.xls"

    # ---- RAG ----
    RAG_CHUNK_SIZE: int = 500
    RAG_CHUNK_OVERLAP: int = 80
    RAG_TOP_K: int = 5
    # Hybrid retrieval (vector + keyword fusion via RRF). Off preserves the
    # pure-vector behaviour of earlier phases.
    RAG_HYBRID: bool = True
    # RRF fusion constant (standard k=60).
    RAG_RRF_K: int = 60
    # Context compression: drop near-duplicate chunks by token overlap.
    RAG_COMPRESS_DEDUP: bool = True
    # Minimum retrieval score for a chunk to be admitted into the answer
    # context. 0.0 = accept everything (preserve historic behaviour). The most
    # comparable score is the reranker score when RERANKER_KIND != "noop"; with
    # hybrid/RRF the raw ``hit.score`` is a tiny RRF value (~0.0x), so to make
    # this threshold effective in hybrid mode enable a real reranker. Below this
    # score the chunk is dropped; if NO chunk clears it, RAG context + citations
    # are emptied so low-relevance snippets never pollute a normal answer.
    RAG_MIN_SCORE: float = 0.0
    # Skip retrieval entirely for social/capability chit-chat ("你好", "你是谁",
    # "你都能干什么", "谢谢", …) so a bound knowledge base never leaks into
    # casual conversation. Explicit "根据知识库回答" still retrieves (the casual
    # detector honours an explicit KB ask).
    RAG_SKIP_CASUAL: bool = True

    # ---- Background worker ----
    BACKGROUND_WORKER: str = "inprocess"

    # ---- Durable worker (Task 5) ----
    # Redis stream + consumer group names for the durable run queue.
    RUN_QUEUE_STREAM: str = "agent-run-queue"
    RUN_QUEUE_GROUP: str = "workers"
    # Lease TTL: how long a worker may hold a run before recovery can reclaim.
    RUN_LEASE_TTL_SECONDS: int = 120
    # How often the worker renews its lease while a run is in flight.
    RUN_LEASE_RENEW_SECONDS: int = 30
    # Worker poll interval when the queue is empty (InMemoryQueue / fallback).
    WORKER_POLL_INTERVAL_SECONDS: float = 1.0
    # SSE event-tail poll interval while events are actively flowing (a reply
    # is streaming). The idle interval stays WORKER_POLL_INTERVAL_SECONDS.
    SSE_STREAM_POLL_INTERVAL_SECONDS: float = 0.15
    # xreadgroup block timeout for the Redis transport (seconds, 0 = non-blocking).
    WORKER_BLOCK_TIMEOUT_SECONDS: int = 5
    # Recovery scheduler scan interval.
    RECOVERY_SCAN_INTERVAL_SECONDS: int = 30
    # Max recovery retries before a run is terminally failed.
    RUN_MAX_RETRIES: int = 3

    # ---- Agent platform (CrewAI / tool safety) ----
    # Master switch for the CrewAI runtime. Even when True, the runtime is only
    # used when execution_mode="agent" and the `crewai` package is importable;
    # native chat is unaffected. Off => native-only, zero crewai imports.
    CREWAI_ENABLED: bool = False
    # python_exec is NOT a real sandbox (subprocess with process perms). In prod
    # it stays disabled unless one of these opts it in AND a sandbox is configured.
    ALLOW_PYTHON_EXEC: bool = False
    PYTHON_SANDBOX: str = ""  # e.g. "docker" | "e2b" | "gvisor" — reserved for Phase 5
    # Optional JSON file of network-egress allow/forbid rules consulted by the
    # http_get / web_search tools (Codex-style NetworkPolicy, see
    # app.agents.network_policy.NetworkRuleStore). Empty = no policy (allow-all,
    # the historic behaviour). Lets an operator forbid egress to specific hosts
    # without code changes.
    NETWORK_POLICY_FILE: str = ""
    # Agent hard-stop budgets (see app.agents.policies.budget_policy).
    AGENT_MAX_STEPS: int = Field(default=8, gt=0)
    AGENT_MAX_TOOL_CALLS: int = Field(default=12, gt=0)
    AGENT_MAX_REPLAN_COUNT: int = Field(default=2, ge=0)
    AGENT_MAX_RUNTIME_SECONDS: float = Field(default=120.0, gt=0, allow_inf_nan=False)
    AGENT_MAX_TOOL_OUTPUT_CHARS: int = Field(default=8_000, ge=16)
    AGENT_MAX_TOTAL_TOKENS: int = Field(default=40_000, gt=0)
    AGENT_MAX_COST_USD: float = Field(default=5.0, gt=0, allow_inf_nan=False)
    # Intent classifier (runs on the chat hot path before the first token).
    # Master switch: when False, intent recognition is skipped entirely and the
    # keyword router handles routing (zero model calls, zero added latency).
    INTENT_CLASSIFIER_ENABLED: bool = True
    # Per-attempt timeout. A 320-token classification finishes in well under a
    # second on any reasonable endpoint; the old 8s ceiling (x2 with retry) could
    # block the first token for ~16s. Lowered to 2s; tune up only if needed.
    INTENT_TIMEOUT_SECONDS: float = 2.0
    # Retries ON TOP OF the first attempt. 0 = one attempt on the hot path: a
    # transient failure falls back to the keyword router immediately rather than
    # doubling the worst-case latency.
    INTENT_MAX_RETRIES: int = 0
    # Demo mode: the CrewAI multi-agent runtime MAY use a deterministic fake
    # executor (no external LLM) so the full multi-agent panel — real SSE,
    # graph, tool attribution, sequential/parallel lifecycle — can be exercised
    # live without configuring a model endpoint. Default OFF. It is a STRICT
    # OPT-IN, gated TWO ways: this flag must be True AND the request must carry
    # an explicit ``demo=True`` flag (see ChatRequest.demo). A normal
    # /api/chat/stream turn NEVER silently receives canned demo answers — even
    # with this flag on, a plain ``mode=deep_research`` request runs the real
    # executor (or falls back to native with a visible reason). A production
    # deployment MUST leave this False (enforced at startup; the demo emits
    # canned, non-real answers) and enable CREWAI_ENABLED with a real
    # MODEL_API_BASE_URL / MODEL_API_KEY instead.
    AGENT_DEMO_MODE: bool = False
    # Plan-approval gate (B5): when truthy, a multi-agent deep_research run
    # publishes its draft plan and WAITS (bounded by PLAN_CONFIRM_TIMEOUT_S)
    # for the user to confirm/revise via /api/agent-runs/{id}/plan/confirm
    # before executing. When falsy (default) the plan is advisory-only —
    # published for review while the run proceeds immediately.
    PLAN_REQUIRE_CONFIRMATION: bool = False
    PLAN_CONFIRM_TIMEOUT_S: int = 300
    # Auto memory proposal (B7): after each chat turn, extract 0-3 candidate
    # memories (rule-based, no extra model calls) from the user's message and
    # store them INACTIVE for the user to review/enable in settings. Dedup is
    # content-exact; candidates never enter the prompt until manually enabled.
    MEMORY_AUTO_PROPOSE: bool = True
    # Workflow engine routing (Task 6b): when set to a truthy value ("1" / "true")
    # AND the route's agent_profile is "deep_research", that turn runs through the
    # durable WorkflowEngine (plan -> execute -> verify -> bounded replan) instead
    # of the static CrewAI crew walker. OFF by default: the proven CrewAI path is
    # the only deep_research executor. On ANY engine exception the turn falls back
    # to the existing CrewAI path, so enabling this can never make a turn worse.
    AGENT_WORKFLOW_ENGINE: str = ""

    # ---- Office→PDF preview conversion (类飞书在线预览) ----
    # Gotenberg server (docker: gotenberg/gotenberg:8) deployed on a SEPARATE
    # box — this VPS lacks the RAM. Empty string disables the feature and the
    # preview panel falls back to download-only for Office formats.
    GOTENBERG_URL: str = ""
    # Conversion timeout: LibreOffice can take a while on big decks.
    GOTENBERG_TIMEOUT_S: int = 120

    # ---- Chat attachments (Phase 1) ----
    # Broader than KB uploads: includes images for multimodal chat and audio
    # for audio-input models (input_audio parts) / transcription fallback.
    ATTACHMENT_ALLOWED_EXT: str = (
        ".pdf,.docx,.txt,.md,.markdown,.csv,.xlsx,.xls,.json,"
        ".png,.jpg,.jpeg,.webp,.gif,.bmp,.tif,.tiff,"
        ".mp3,.wav,.m4a,.ogg,.webm,.flac,.aac,"
        ".pptx,.ppt,.html,.htm,.epub,.rtf,.doc,"
        ".odt,.ods,.odp"
    )
    ATTACHMENT_MAX_MB: int = 20
    MAX_ATTACHMENTS_PER_MESSAGE: int = 10
    # Background parse timeout for a single attachment (seconds).
    ATTACHMENT_PARSE_TIMEOUT: int = 60

    # ---- File parsing / multimodal (engineering-grade attachments) ----
    # OCR backend for scanned PDFs / image text extraction.
    #   rapidocr (default) — pip-installed, no system binary, cross-platform.
    #   tesseract          — wraps a system Tesseract on PATH.
    OCR_ENGINE: str = "rapidocr"
    # When a PDF yields almost no selectable text, treat it as scanned and run
    # OCR over each rendered page (the fallback behind the vision/OCR strategy).
    OCR_SCANNED_PDF: bool = True
    # Max pages to OCR per scanned PDF (bound the cost on huge scans).
    OCR_SCANNED_PDF_MAX_PAGES: int = 30
    # Inline-injection budget for attachment text, as a fraction of the model's
    # context window. A doc whose text exceeds min(fraction*context, hard cap)
    # is auto-chunked into a per-attachment Qdrant collection and retrieved on
    # demand instead of being spliced wholesale.
    ATTACHMENT_INLINE_FRACTION: float = 0.30
    ATTACHMENT_INLINE_MAX_CHARS: int = 24000
    # Long edge (px) an image is downscaled to before vision injection, to keep
    # base64 / token cost bounded (OpenAI recommends <= 2048px).
    VISION_IMAGE_MAX_EDGE: int = 2048
    # Heuristic: model_names containing any token are assumed vision-capable
    # unless the ModelConfig row explicitly sets supports_vision=False. Lets the
    # feature work out-of-the-box for well-known vision models.
    VISION_MODEL_KEYWORDS: str = "gpt-4o,qwen-vl,qwen2-vl,qwen2.5-vl,glm-4v,glm-4.5v,internvl,llava,minicpm-v,deepseek-vl,vision,vl"

    # ---- SSE ----
    # Heartbeat comment cadence to keep proxies/CDNs from dropping idle streams.
    SSE_HEARTBEAT_SECONDS: int = 20

    # ---- Model HTTP timeouts (provider → upstream model endpoint) ----
    # Connect is short; read is generous so slow / long generations aren't killed
    # mid-stream (the old single 30s read timeout truncated long code answers).
    # The browser↔backend SSE heartbeat above is independent and does NOT keep the
    # backend↔model httpx connection alive.
    MODEL_CONNECT_TIMEOUT_SECONDS: float = 10.0
    MODEL_READ_TIMEOUT_SECONDS: float = 180.0
    MODEL_WRITE_TIMEOUT_SECONDS: float = 30.0
    MODEL_POOL_TIMEOUT_SECONDS: float = 30.0

    # ---- Reranker (Phase 1+ RAG) ----
    # noop | local_bge | remote_api. ``noop`` preserves today's behavior.
    RERANKER_KIND: str = "noop"
    RERANKER_MODEL: str = "BAAI/bge-reranker-base"
    RERANKER_API_BASE_URL: str = ""
    RERANKER_API_KEY: str = ""
    RERANKER_TOP_K: int = 5
    # Over-fetch factor: pull top_k * factor vector hits before reranking.
    RERANKER_OVERFETCH: int = 4

    # ---- Web search (tool: web_search) ----
    # Reachable search backend for the web_search tool. The dependency-free
    # DuckDuckGo HTML scrape is the default no-config fallback, but it is blocked
    # in some networks (e.g. mainland CN), so point this at a JSON-returning
    # search endpoint you can actually reach. Examples:
    #   * self-hosted SearXNG (GET): http://localhost:8080/search   (no key)
    #   * Bing v7 (GET):             https://api.bing.microsoft.com/v7.0/search  (WEB_SEARCH_API_KEY)
    #   * Tavily (POST):             https://api.tavily.com/search   (WEB_SEARCH_API_KEY, WEB_SEARCH_METHOD=post)
    #   * Serper (POST):             https://google.serper.dev/search (WEB_SEARCH_API_KEY, WEB_SEARCH_METHOD=post)
    WEB_SEARCH_ENDPOINT: str = ""
    WEB_SEARCH_API_KEY: str = ""
    # get | post — how to call WEB_SEARCH_ENDPOINT. GET fits SearXNG/Bing;
    # POST fits Tavily/Serper (query + key go in the JSON body / provider header).
    WEB_SEARCH_METHOD: str = "get"

    # ---- Bootstrap admin ----
    ADMIN_EMAIL: str = "admin@example.com"
    ADMIN_USERNAME: str = "admin"
    ADMIN_PASSWORD: str = "changeme123"

    # ---- Token / cost accounting + security policy ----
    # Per-message token usage is persisted (Message.prompt/completion/total_tokens).
    # Cost is computed from this price table: a JSON map of model-substring ->
    # {"prompt": <USD per 1M prompt tokens>, "completion": <USD per 1M completion>}.
    # First matching substring (longest-first) wins. Empty => cost left null.
    # Example: {"gpt-4o": {"prompt": 2.5, "completion": 10}, "gpt-4o-mini": {"prompt": 0.15, "completion": 0.6}}
    MODEL_PRICING_JSON: str = ""
    # Password policy (applied at register/change). min length + complexity toggle.
    PASSWORD_MIN_LENGTH: int = 8
    PASSWORD_REQUIRE_COMPLEXITY: bool = True
    # Idempotency window for chat sends: a client-supplied Idempotency-Key within
    # this TTL dedupes a retried send (avoids double model spend on flaky networks).
    IDEMPOTENCY_TTL_SECONDS: int = 600
    # Backpressure: cap on concurrent in-flight model calls across the process.
    # Bounds DB-pool + provider-connection exhaustion under burst load.
    MAX_CONCURRENT_MODEL_CALLS: int = 16
    # Automatic follow-up calls after an upstream output-token limit. Zero
    # disables; the hard upper bound prevents runaway provider spend.
    AUTO_CONTINUATION_MAX_ROUNDS: int = Field(default=2, ge=0, le=8)
    # Circuit breaker: open a provider's circuit after this many consecutive
    # failures, then fast-fail for the cooldown before a half-open probe.
    MODEL_CIRCUIT_FAILURE_THRESHOLD: int = 5
    MODEL_CIRCUIT_COOLDOWN_SECONDS: float = 30.0
    # Semantic cache (W2): cache exact (model+messages) completions for this TTL
    # so identical prompts skip the model entirely. 0 disables.
    SEMANTIC_CACHE_TTL_SECONDS: int = 0
    SEMANTIC_CACHE_ENABLED: bool = False

    # ---- Workspace tools + sandbox runner (Task 8) ----
    # Master switch for the workspace-confined tool set. OFF by default so the
    # default registry is byte-identical to the pre-Task-8 behaviour; turn on to
    # expose list/read/search/write/apply_patch/shell/git tools confined to
    # WORKSPACE_ROOT. See app.tools.workspace + app.tools.registry_init.
    WORKSPACE_TOOLS_ENABLED: bool = False
    # Root directory the workspace tools confine to. Empty = the caller must pass
    # an explicit root to get_workspace_registry(); the tools refuse to run with
    # no root bound.
    WORKSPACE_ROOT: str = ""
    # Sandbox runner mode: "local" (dev/test subprocess, NOT a real sandbox) or
    # "docker" (enterprise isolation, production-safe). LocalRunner hard-refuses
    # to exec outside dev/test regardless of this setting.
    SANDBOX_MODE: str = "local"
    # Docker isolation knobs (read by app.agents.sandbox.docker.DockerRunnerConfig).
    SANDBOX_DOCKER_IMAGE: str = "python:3.11-slim"
    SANDBOX_CPU_QUOTA: float = Field(default=1.0, gt=0)
    SANDBOX_MEMORY_MB: int = Field(default=512, gt=0)
    SANDBOX_PIDS_LIMIT: int = Field(default=64, gt=0)
    SANDBOX_TIMEOUT_SECONDS: int = Field(default=30, gt=0)
    # Hard per-command output cap (chars of stdout/stderr the runner returns).
    # The ToolGateway's AGENT_MAX_TOOL_OUTPUT_CHARS budget is enforced separately
    # and is NOT duplicated here.
    SANDBOX_OUTPUT_LIMIT: int = Field(default=8192, ge=16)

    # ---- MCP servers (Task 9) ----
    # Statically-configured MCP servers offered to every run (in addition to
    # per-tenant connectors). A JSON array of objects with: name, command (the
    # subprocess command for stdio OR the server URL for http), args, env,
    # transport ("stdio" | "http" | "sse"). Empty => no static MCP servers (the
    # boot guard holds; the app runs unchanged). Example:
    #   [{"name":"echo","command":"python","args":["-m","echo_server"],"transport":"stdio"}]
    MCP_SERVERS: str = ""

    # ---- Observability (Task 11) ----
    # OpenTelemetry-compatible traces + Prometheus metrics are ALWAYS probed via
    # the no-op fallback (app.observability); these knobs only take effect when
    # the respective package is importable. Sampling 1.0 = every span, 0 = none.
    OTEL_ENABLED: bool = False
    OTEL_SERVICE_NAME: str = "mygpt-backend"
    PROMETHEUS_ENABLED: bool = False
    # ---- Quotas (Task 11) ----
    # Multi-axis per-tenant caps. Disabled in test (see QuotaLimits.from_settings)
    # so the suite is never blocked; production opts in via QUOTAS_ENABLED=true.
    QUOTAS_ENABLED: bool = False
    QUOTA_MAX_CONCURRENT_RUNS: int = 8
    QUOTA_MAX_TOKENS: int = 1_000_000
    QUOTA_MAX_COST_USD: float = 50.0
    QUOTA_MAX_STORAGE_BYTES: int = 10 * 1024 * 1024 * 1024  # 10 GiB
    QUOTA_MAX_CONNECTORS: int = 25

    # ---- Derived ----
    @property
    def cors_origins(self) -> List[str]:
        return [o.strip() for o in self.BACKEND_CORS_ORIGINS.split(",") if o.strip()]

    @property
    def allowed_extensions(self) -> set[str]:
        return {e.strip().lower() for e in self.ALLOWED_UPLOAD_EXT.split(",") if e.strip()}

    @property
    def is_dev(self) -> bool:
        return self.ENV == "dev"

    @field_validator("STORAGE_DIR")
    @classmethod
    def _abs_storage(cls, v: str) -> str:
        return str(Path(v))

    @model_validator(mode="after")
    def _guard_default_secrets(self) -> "Settings":
        # Refuse to boot a real deployment with the publicly-known default JWT
        # secret or admin password — either enables trivial takeover. Dev/test
        # keep the defaults so the demo login and the test suite work as-is.
        if self.ENV not in ("dev", "test"):
            if self.JWT_SECRET in ("", "please-change-this"):
                raise ValueError(
                    "JWT_SECRET must be set to a strong random value in non-dev environments"
                )
            if self.ADMIN_PASSWORD in ("", "changeme123"):
                raise ValueError(
                    "ADMIN_PASSWORD must be changed from the default in non-dev environments"
                )
            # Without a stable FERNET_KEY, stored API keys are encrypted with an
            # ephemeral per-process random key and become undecryptable on any
            # restart/redeploy — a silent data-loss/availability bug. Fail startup
            # in real deployments so this can't ship unnoticed.
            if not self.FERNET_KEY:
                raise ValueError(
                    "FERNET_KEY must be set in non-dev environments — otherwise "
                    "stored API keys are encrypted with an ephemeral random key "
                    "and become undecryptable on restart. Generate one with: "
                    "python -c \"from cryptography.fernet import Fernet; "
                    "print(Fernet.generate_key().decode())\""
                )
            # Demo mode emits CANNED, non-real answers from a fake executor.
            # It must never run in production — a real user would receive
            # fabricated content presented as a genuine model answer (this is
            # exactly the regression that leaked "CrewAI supports stateful
            # Flows…" into normal chat). Fail startup loudly, do not warn-and-
            # continue: a warning is too easy to miss in deploy logs.
            if getattr(self, "AGENT_DEMO_MODE", False):
                raise ValueError(
                    "AGENT_DEMO_MODE must be False in non-dev environments "
                    "(demo mode serves canned, non-real answers). Set ENV=dev "
                    "for local demos, or enable CREWAI_ENABLED with a real model."
                )
        return self


@lru_cache
def get_settings() -> Settings:
    return Settings()
