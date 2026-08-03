"""Central configuration. All runtime knobs come from environment via pydantic-settings.

Nothing in the app should read os.environ directly — import `get_settings()` here.
"""
from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import List

from pydantic import field_validator, model_validator
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
    VECTOR_DB: str = "qdrant"
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
    MINIO_ENDPOINT: str = "minio:9000"
    MINIO_ACCESS_KEY: str = "minioadmin"
    MINIO_SECRET_KEY: str = "minioadmin"
    MINIO_BUCKET: str = "ai-chat"

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

    # ---- Agent platform (CrewAI / tool safety) ----
    # Master switch for the CrewAI runtime. Even when True, the runtime is only
    # used when execution_mode="agent" and the `crewai` package is importable;
    # native chat is unaffected. Off => native-only, zero crewai imports.
    CREWAI_ENABLED: bool = False
    # python_exec is NOT a real sandbox (subprocess with process perms). In prod
    # it stays disabled unless one of these opts it in AND a sandbox is configured.
    ALLOW_PYTHON_EXEC: bool = False
    PYTHON_SANDBOX: str = ""  # e.g. "docker" | "e2b" | "gvisor" — reserved for Phase 5
    # Agent hard-stop budgets (see app.agents.policies.budget_policy).
    AGENT_MAX_STEPS: int = 8
    AGENT_MAX_TOOL_CALLS: int = 12
    AGENT_MAX_RUNTIME_SECONDS: int = 120
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

    # ---- Chat attachments (Phase 1) ----
    # Broader than KB uploads: includes images for multimodal chat.
    ATTACHMENT_ALLOWED_EXT: str = (
        ".pdf,.docx,.txt,.md,.csv,.xlsx,.json,.png,.jpg,.jpeg,.webp"
    )
    ATTACHMENT_MAX_MB: int = 20
    MAX_ATTACHMENTS_PER_MESSAGE: int = 10
    # Background parse timeout for a single attachment (seconds).
    ATTACHMENT_PARSE_TIMEOUT: int = 60

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

    # ---- Object storage: S3 (Phase 1 production option; local still default) ----
    S3_ENDPOINT: str = ""           # leave empty for AWS default
    S3_REGION: str = ""
    S3_BUCKET: str = "ai-chat"
    S3_ACCESS_KEY: str = ""
    S3_SECRET_KEY: str = ""
    S3_USE_PATH_STYLE: bool = False

    # ---- Bootstrap admin ----
    ADMIN_EMAIL: str = "admin@example.com"
    ADMIN_USERNAME: str = "admin"
    ADMIN_PASSWORD: str = "changeme123"

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
