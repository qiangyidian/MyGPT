"""Central configuration. All runtime knobs come from environment via pydantic-settings.

Nothing in the app should read os.environ directly — import `get_settings()` here.
"""
from __future__ import annotations

import logging
from functools import lru_cache
from pathlib import Path

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

_logger = logging.getLogger(__name__)

# Extensions already reported as "configured but unparseable". ``allowed_extensions``
# is consulted on every upload, so without this the diagnostic would spam.
_warned_unparseable_exts: set[str] = set()

# Repo root (this file is backend/app/core/config.py → parents[3]). The config
# is loaded from the repo-root .env regardless of the current working directory:
# start.bat runs uvicorn from backend/, docker-compose from /app — both must see
# the SAME source for CREWAI_ENABLED. A CWD-local .env
# (backend/.env) is read too and takes per-key precedence, so a host-run dev DB
# URL (localhost) still overrides the docker service-name URL in the root .env.
_REPO_ROOT = Path(__file__).resolve().parents[3]

# ---- 沙箱 runner 模式（唯一事实来源，见 app.agents.sandbox.factory） ----
# "local"  = 子进程 runner，只在 dev/test 可用（LocalRunner 自己在非 dev/test 硬拒绝）；
# "docker" = 生产用的隔离 runner（独立容器 + 资源/网络限额）。
SANDBOX_MODE_LOCAL = "local"
SANDBOX_MODE_DOCKER = "docker"
SANDBOX_MODES: tuple[str, ...] = (SANDBOX_MODE_LOCAL, SANDBOX_MODE_DOCKER)

# 环境开关取值的唯一解析口径：真值词表 + 假值词表。
# 全仓库解析 settings/env 布尔都必须走 :func:`env_flag`，不要再各处手写
# ``bool(x)`` —— ``bool("false") is True`` 正是 python_exec 曾被任意字符串放行的根因。
_FLAG_TRUE_TOKENS: frozenset[str] = frozenset({"1", "true", "yes", "on", "y", "t", "enabled"})
_FLAG_FALSE_TOKENS: frozenset[str] = frozenset(
    {"0", "false", "no", "off", "n", "f", "disabled", ""}
)


def env_flag(value: object, *, default: bool = False) -> bool:
    """把 settings / 环境变量里的开关值解析成布尔（仓库唯一入口）。

    * ``bool`` 原样返回（pydantic 已经解析过的字段）；
    * 其余按字符串规范化（去空白 + 小写）后查词表：``"1"/"true"/"yes"/"on"`` 为真，
      ``"0"/"false"/"no"/"off"/""`` 为假；
    * 词表外的取值返回 ``default`` —— 认不出来不等于「已开启」（fail closed）。

    注：``app/agents/orchestrator.py`` 的 ``_truthy`` 与
    ``app/agents/intent_service.py`` 的 ``_bool`` 是本函数两份更早的私有副本，
    收敛到这里是为了让 python_exec 之类的安全开关只有一处解析口径。
    """
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    token = str(value).strip().lower()
    if token in _FLAG_TRUE_TOKENS:
        return True
    if token in _FLAG_FALSE_TOKENS:
        return False
    return default


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
    # Empty = unset. Bootstrap seeds a system model only when provider + base
    # URL + key are all real; models are managed in Settings → Models. The
    # defaults must stay empty so a fresh install never starts with a dead
    # "my-model @ localhost" row that re-appears after deletion.
    MODEL_PROVIDER: str = ""
    MODEL_API_BASE_URL: str = ""
    MODEL_API_KEY: str = ""
    MODEL_NAME: str = ""
    EMBEDDING_API_BASE_URL: str = ""
    EMBEDDING_API_KEY: str = ""
    EMBEDDING_MODEL_NAME: str = ""

    # ---- Storage ----
    STORAGE_BACKEND: str = "local"
    STORAGE_DIR: str = "./data/uploads"
    MAX_UPLOAD_MB: int = 20
    # What the KB upload endpoint accepts. This is a *policy* list (operator can
    # narrow it), not a capability list: the final allow-set is intersected with
    # the parser registry in ``allowed_extensions`` below, so an entry nobody can
    # parse back out is dropped instead of turning into an "uploaded, then
    # indexing failed" document. Formats listed here that the parser registry
    # doesn't know are reported as a startup/config warning, not silently kept.
    ALLOWED_UPLOAD_EXT: str = (
        ".pdf,.docx,.doc,.pptx,.ppt,.txt,.md,.markdown,.log,.json,"
        ".csv,.xlsx,.xls,.html,.htm,.epub,.rtf,.odt,.ods,.odp"
    )

    # ---- RAG ----
    RAG_CHUNK_SIZE: int = 500
    RAG_CHUNK_OVERLAP: int = 80
    RAG_TOP_K: int = 5
    # Hard ceiling on the retrieved-context block, in tokens. top_k alone never
    # bounded prompt size: 12 chunks of a long-document KB is an ~18k-token
    # context paid for on every turn. 0 disables the budget (historic shape).
    RAG_CONTEXT_TOKENS: int = 6000
    # Hybrid retrieval (vector + keyword fusion via RRF). Off preserves the
    # pure-vector behaviour of earlier phases.
    RAG_HYBRID: bool = True
    # RRF fusion constant (standard k=60).
    RAG_RRF_K: int = 60
    # Keyword-path candidate ceiling. Scoring the candidates is Python-side, so
    # this is what bounds per-turn CPU on a big KB; 400 was a hard-coded literal.
    RAG_KEYWORD_CANDIDATES: int = 400
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
    # Event batching: streamed run events flush to run_events in batches of
    # RUN_EVENT_BATCH_SIZE or every RUN_EVENT_FLUSH_SECONDS, whichever first
    # (per-token transactions were a 3-orders-of-magnitude write amplification).
    RUN_EVENT_BATCH_SIZE: int = 32
    RUN_EVENT_FLUSH_SECONDS: float = 0.2
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
    # Default True: crewai is a pinned hard dependency (requirements.txt), and
    # the UI advertises 专家模式 = 多Agent协作 — a fresh install must not
    # silently degrade expert mode to a single agent just because no .env set
    # this. Environments without the package still fall back to native with a
    # visible crewai_not_installed reason (orchestrator._crewai_status).
    CREWAI_ENABLED: bool = True
    # python_exec 不是真沙箱（子进程带本进程权限）。在生产里它保持关闭，除非
    # ① 显式打开本开关 **且** ② SANDBOX_MODE=PYTHON_SANDBOX=docker 真的接上了隔离
    # 后端 —— 两个条件缺一都不放行（见 app.agents.policies.tool_policy）。
    ALLOW_PYTHON_EXEC: bool = False
    # 代码执行使用的隔离后端名。目前仓库里真实存在的只有 "docker"
    # （app.agents.sandbox.docker）；"e2b" / "gvisor" 仍是占位，写了也不会放行。
    PYTHON_SANDBOX: str = ""  # e.g. "docker" | "e2b" | "gvisor" — reserved for Phase 5
    # User-configured model endpoints are SSRF-guarded: regular (non-admin)
    # users may only point at hosts that resolve to PUBLIC addresses. Set true
    # only for trusted single-tenant deployments where end users must reach
    # self-hosted in-cluster models (vLLM/Ollama) over private addresses.
    ALLOW_PRIVATE_MODEL_ENDPOINTS: bool = False
    # Proxy networks whose X-Forwarded-For headers are believed for client-IP
    # resolution (rate limiting keys on this). Comma-separated CIDRs; empty =
    # loopback + RFC1918/docker defaults (nginx-on-host and nginx-in-compose).
    # Only set this to the exact proxy networks in front of the app — a client
    # must never be able to choose its own rate-limit identity via the header.
    TRUSTED_PROXIES: str = ""
    # Hard cap for user-uploaded artifacts (MB). The artifacts endpoint
    # previously had NO size/type/frequency limit — any logged-in user could
    # fill the disk unbounded.
    MAX_ARTIFACT_UPLOAD_MB: int = 50
    # When the existing Qdrant collection's dim differs from the configured
    # embedding dim: False (default) raises a clear, actionable error at
    # indexing time; True restores the old behaviour of DELETING and
    # recreating the collection — which silently wiped the whole KB's vectors
    # whenever the embedding model changed without updating the env var.
    QDRANT_AUTO_RECREATE_ON_DIM_MISMATCH: bool = False
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
    # Plan-approval gate (B5): the plan is ALWAYS published first ("计划先行")
    # and the user may confirm/revise it via /api/agent-runs/{id}/plan/confirm.
    # 计划默认不阻塞执行 —— 只有用户主动上闸（RunControl.request_gate）后，
    # run 才会在门禁处等待确认，最多等 PLAN_CONFIRM_TIMEOUT_S 秒后按当前
    # 计划继续（用户没响应不应让任务失败）。
    PLAN_REQUIRE_CONFIRMATION: bool = True
    PLAN_CONFIRM_TIMEOUT_S: int = 90
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
    # 逗号分隔的 profile 名单。总开关为真时，只有名单内的 profile 走引擎。
    # 默认 deep_research（产品决策）：运维一旦把总开关打开，灰度面就**只有**
    # deep_research —— 最成熟、模板/工具/事件都最早验证的那个拓扑。其余 profile
    # （parallel_research / debate / task_decomposition / write_review）必须显式
    # 加进名单才上引擎，所以这份默认值不会放大灰度。
    # 想退回「开总开关也不切任何 profile」：把这里置空即可（每个 profile 单独
    # 摘除 = 独立回滚）。
    AGENT_WORKFLOW_ENGINE_PROFILES: str = "deep_research"
    # LLM 规划器：开启后计划先由模型提议，产出必须通过 validate_plan()，
    # 否则回退模板。模型失败/超时/预算耗尽一律回退 —— LLM 永远不能让引擎
    # 挂掉。默认关：这是额外的模型调用，消耗用户 token 且发生在首 token 之前。
    AGENT_LLM_PLANNER: bool = False
    AGENT_LLM_PLANNER_MAX_STEPS: int = 8
    # 首 token 延迟预算（总预算，不是单次）。超时即回退模板。
    AGENT_LLM_PLANNER_TIMEOUT_S: float = 8.0
    # LLM verifier：用模型验收步骤产出（而非只查 min_chars）。
    # 非法 verdict 回退 RuleBasedVerifier。默认关。
    AGENT_LLM_VERIFIER: bool = False
    AGENT_LLM_VERIFIER_TIMEOUT_S: float = 10.0
    # 丰富 step 事件：stage 完成时透出完整产出（step_output）、运行中发心跳
    # （step_progress）、节点携带 tokens 与成本。纯增量事件与字段，老客户端
    # 忽略即可，故默认开；置假即回到旧行为。
    AGENT_RICH_STEP_EVENTS: bool = True
    # step_progress 心跳间隔（秒）。
    AGENT_STEP_PROGRESS_INTERVAL_S: float = 5.0

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
    # Long edge (px) an image is downscaled to BEFORE OCR. OCR cost scales
    # super-linearly with pixel count — a full-screen screenshot (3000px+) on
    # CPU easily blows the parse timeout. 1568px is rapidocr's native detection
    # resolution, so accuracy is unaffected.
    OCR_MAX_IMAGE_EDGE: int = 1568
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

    # ---- Email verification codes (registration) ----
    # SMTP sender for one-time registration codes. MAIL_ENABLED=false disables
    # real sending — in that mode request_email_code returns the code in the
    # response ONLY when not is_prod (dev convenience), and register
    # falls back to no-code (also non-production only). Production requires a
    # complete SMTP configuration.
    MAIL_ENABLED: bool = False
    MAIL_HOST: str = ""
    MAIL_PORT: int = 465
    MAIL_USERNAME: str = ""
    MAIL_PASSWORD: str = ""          # SMTP auth password / provider auth code
    MAIL_FROM: str = ""              # defaults to MAIL_USERNAME when empty
    MAIL_AUTH: bool = True
    # Verification code lifetime (seconds) and per-email send cadence.
    EMAIL_CODE_TTL_SECONDS: int = 300       # 5 minutes
    EMAIL_CODE_RESEND_INTERVAL: int = 60    # min seconds between sends
    EMAIL_CODE_BURST_LIMIT: int = 5         # max sends per email per hour
    EMAIL_CODE_BURST_WINDOW: int = 3600
    EMAIL_CODE_MAX_ATTEMPTS: int = 5        # failed verifications before the code is invalidated

    # ---- WeChat scan login (via the wechat-auth service) ----
    # MyChat does NOT talk to WeChat: the callback, the reply and the code
    # issuance all live in the standalone wechat-auth service, because WeChat
    # allows exactly one callback URL per Official Account while several
    # products share the account. Here we only redeem a code for an openid.
    # See docs/wechat-login.md.
    WECHAT_AUTH_ENABLED: bool = False
    # Loopback: the service's application-facing endpoints are deliberately
    # unpublished, so this must stay a private address.
    WECHAT_AUTH_BASE_URL: str = "http://127.0.0.1:8020"
    WECHAT_AUTH_APP_ID: str = "mychat"
    # Issued by wechat-auth's registry (/etc/wxauth/apps.json). Must match.
    WECHAT_AUTH_APP_SECRET: str = ""
    # Official Account QR shown when the service runs without AppID/AppSecret
    # (keyword mode). Empty = the login page shows text instructions only.
    WECHAT_AUTH_LOGIN_QR_URL: str = "/images/wechat-account-qrcode.jpg"
    WECHAT_AUTH_DEFAULT_KEYWORD: str = "验证码"
    WECHAT_AUTH_TIMEOUT_SECONDS: float = 5.0

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
    # to exec outside dev/test regardless of this setting. An illegal value is a
    # boot error (see _validate_sandbox_mode) — never a silent fallback to local.
    SANDBOX_MODE: str = "local"
    # Docker isolation knobs (read by app.agents.sandbox.docker.DockerRunnerConfig).
    SANDBOX_DOCKER_IMAGE: str = "python:3.11-slim"
    SANDBOX_CPU_QUOTA: float = Field(default=1.0, gt=0)
    SANDBOX_MEMORY_MB: int = Field(default=512, gt=0)
    SANDBOX_PIDS_LIMIT: int = Field(default=64, gt=0)
    SANDBOX_TIMEOUT_SECONDS: int = Field(default=30, gt=0)
    # local 模式的 CPU 时间上限（RLIMIT_CPU，秒）与内存上限（RLIMIT_AS，MB）。
    # docker 模式用 --cpus/--memory，两者按模式各取所需。
    SANDBOX_CPU_SECONDS: int = Field(default=30, gt=0)
    SANDBOX_LOCAL_MEMORY_MB: int = Field(default=512, gt=0)
    # 单条命令允许写出的文件体积上限（RLIMIT_FSIZE，MB）——防止一条命令把
    # 磁盘打满（docker 侧由只读 rootfs + tmpfs 尺寸承担）。
    SANDBOX_MAX_FSIZE_MB: int = Field(default=64, gt=0)
    # 强制施加硬限额：True（默认）时，local 模式在无法施加 rlimit 的平台上
    # （Windows 没有 resource 模块）**直接拒绝执行**，而不是假装已经限制。
    # 设为 False 只应在明确的开发机上使用，且结果里会标记 limits_enforced=false。
    SANDBOX_REQUIRE_LIMITS: bool = True
    # python_exec / workspace 工具的单次 scratch 目录根。为空时回落到
    # WORKSPACE_ROOT，再为空则回落到系统临时目录（只挂这一个空目录给容器）。
    SANDBOX_SCRATCH_ROOT: str = ""
    # 权限档案（app.agents.permission_profiles）→ 编译成沙箱能力：
    # 决定注册哪些 workspace 工具，以及 docker 容器的
    # 只读 rootfs / 网络 / 工作区挂载读写开关。默认 :workspace-write
    # （可写工作区 + shell，无网络）。
    WORKSPACE_PERMISSION_PROFILE: str = ":workspace-write"
    # 可选档案白名单（逗号分隔）。为空 = 只允许两个安全内置档案
    # （:read-only / :workspace-write）；:danger-full-access 必须显式列出才可选。
    WORKSPACE_PROFILES_ALLOWED: str = ""
    # 命令前缀策略文件（app.agents.exec_policy 的 JSON 形态）：allow/prompt/forbidden。
    # 为空 = 无规则、默认 prompt（未知命令一律走人工确认），fail closed。
    EXEC_POLICY_FILE: str = ""
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
    # Accounting period for token/cost quotas (seconds; default 30 days). The
    # period index is part of the Redis key, so a period rolls over without a
    # reset job.
    QUOTA_PERIOD_SECONDS: int = 2_592_000
    # How long a reserved concurrent-run slot lives before other admissions
    # may reclaim it (protects against a release lost to a Redis outage).
    QUOTA_RUN_TTL_SECONDS: int = 3600

    # ---- Credits / redeem codes ----
    # 预付费积分。CREDITS_ENFORCED 默认关 = 观察模式：扣分照常记账、余额照常
    # 显示，但余额不足不拦截。上线时先发码核对扣分数字，再打开拦截。
    CREDITS_ENFORCED: bool = False
    # 1 美元服务端实测成本 = 多少积分。
    CREDITS_PER_USD: float = 1000.0
    # 未配置定价的模型（usage_cost() 返回 None）按 token 兜底扣分，
    # 每 1000 token 记多少积分。没有这条兜底，这些模型就是免费额度。
    CREDITS_PER_1K_TOKENS_FALLBACK: float = 1.0
    # 注册赠送积分；0 = 不送。
    CREDITS_SIGNUP_BONUS: int = 0
    # 单次管理员调分的绝对值上限（防误操作把余额打成天文数字）。
    CREDITS_MAX_ADJUST: int = 10_000_000
    # 单批兑换码生成上限。
    REDEEM_MAX_CODES_PER_BATCH: int = 5000
    # 兑换码哈希的 HMAC pepper。留空会回落到由 JWT_SECRET 派生的键 ——
    # 任何部署都不会静默退化成无 pepper 的裸哈希。只有数据库 dump、没有
    # 应用层秘密的攻击者无法从 6 字符明文前缀 + 哈希暴力反推全码。
    # 显式配置的收益：轮换 JWT_SECRET 不影响已存的兑换码哈希。
    REDEEM_CODE_PEPPER: str = ""
    # ---- Data retention (app.services.retention) ----
    AUDIT_RETENTION_DAYS: int = 365
    RUN_EVENT_RETENTION_DAYS: int = 90
    ORPHAN_SWEEP_ENABLED: bool = True
    # 删知识库时向量那一步是 best-effort（Qdrant 抖动不该把用户的删除变成 502），
    # 失败会留下名字派生自已删除 KB id、正常路径再也指不回来的孤儿 collection。
    # 这个开关控制周期性回收它们（只认 kb_<32hex> 形状，见 retention.py）。
    ORPHAN_COLLECTION_SWEEP_ENABLED: bool = True

    # ---- Derived ----
    @property
    def cors_origins(self) -> list[str]:
        return [o.strip() for o in self.BACKEND_CORS_ORIGINS.split(",") if o.strip()]

    @property
    def allowed_extensions(self) -> set[str]:
        """Extensions the KB upload path accepts: configured list ∩ parser registry.

        The intersection is the point. A configured-but-unparseable extension is
        worse than a missing one: the file is stored, billed and shown as a row,
        then indexing dies on ``不支持的文件类型`` and the document sits at
        ``failed`` forever with no path to recovery. Dropping it at the door is
        the only outcome the user can act on, so the parser registry
        (:data:`app.rag.parsers.SUPPORTED_EXTS`) holds the veto.
        """
        configured: set[str] = set()
        for raw in self.ALLOWED_UPLOAD_EXT.split(","):
            entry = raw.strip().lower()
            if not entry:
                continue
            configured.add(entry if entry.startswith(".") else f".{entry}")
        try:
            # Imported lazily: ``app.rag.parsers`` depends on this module.
            from app.rag.parsers import SUPPORTED_EXTS
        except Exception:  # pragma: no cover - registry is pure-python, can't fail
            return configured
        parseable = configured & SUPPORTED_EXTS
        refused = sorted(configured - parseable)
        if refused:
            fresh = [e for e in refused if e not in _warned_unparseable_exts]
            if fresh:
                _warned_unparseable_exts.update(fresh)
                _logger.warning(
                    "ALLOWED_UPLOAD_EXT lists %s but no parser can read them; "
                    "uploads of these types will be rejected",
                    ",".join(fresh),
                )
        return parseable

    @property
    def is_dev(self) -> bool:
        return self.ENV == "dev"

    @property
    def is_prod(self) -> bool:
        # Single source of truth for "am I running in production" — code must
        # check this instead of comparing ENV against a hand-typed string (a
        # "production" vs "prod" mismatch once made prod echo email codes).
        return self.ENV == "prod"

    @field_validator("STORAGE_DIR")
    @classmethod
    def _abs_storage(cls, v: str) -> str:
        return str(Path(v))

    @field_validator("SANDBOX_MODE")
    @classmethod
    def _validate_sandbox_mode(cls, v: str) -> str:
        """SANDBOX_MODE 只认 ``local`` / ``docker``，非法值直接启动期报错。

        不做静默回落：把 ``Docker`` 这类手误悄悄当成 local，生产就会用无隔离的
        子进程去执行模型给出的代码；反过来把 local 拼错成别的也会让整套
        workspace 工具看起来「已配置」。拼错必须响。
        """
        mode = (v or "").strip().lower()
        if mode not in SANDBOX_MODES:
            raise ValueError(
                f"SANDBOX_MODE={v!r} 不支持，只能是 {', '.join(SANDBOX_MODES)} 之一"
                "（生产用 docker：local runner 只在 dev/test 允许执行代码）"
            )
        return mode

    @model_validator(mode="after")
    def _guard_default_secrets(self) -> Settings:
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
            # Without the shared secret every scan login fails at runtime with
            # an opaque 503 — and the tempting "fix" is to disable the check.
            # Refuse to boot instead, so the misconfiguration is impossible to
            # miss. The secret itself lives in wechat-auth's registry, not here.
            if self.WECHAT_AUTH_ENABLED and not self.WECHAT_AUTH_APP_SECRET:
                raise ValueError(
                    "WECHAT_AUTH_APP_SECRET must be set when WECHAT_AUTH_ENABLED "
                    "is on in non-dev environments — copy it from wechat-auth's "
                    "/etc/wxauth/apps.json for app "
                    f"{self.WECHAT_AUTH_APP_ID!r}"
                )
        return self


@lru_cache
def get_settings() -> Settings:
    return Settings()
