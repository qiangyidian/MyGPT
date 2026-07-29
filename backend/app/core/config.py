"""Central configuration. All runtime knobs come from environment via pydantic-settings.

Nothing in the app should read os.environ directly — import `get_settings()` here.
"""
from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import List

from pydantic import field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore", case_sensitive=True
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
    # Demo mode: the CrewAI multi-agent runtime uses a deterministic fake
    # executor (no external LLM) so the full multi-agent panel — real SSE,
    # graph, tool attribution, sequential/parallel lifecycle — can be exercised
    # live without configuring an OpenAI-compatible endpoint. Off in prod.
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
        return self


@lru_cache
def get_settings() -> Settings:
    return Settings()
