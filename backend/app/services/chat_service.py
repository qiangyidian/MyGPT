"""Chat orchestration service.

Ties together conversations, model providers, RAG retrieval, and the agent
platform into a single streaming pipeline. The model<->tool loop itself now
lives in :class:`~app.agents.runtime.native_runtime.NativeChatRuntime` (selected
by :class:`~app.agents.orchestrator.ChatOrchestrator`); this service keeps the
app-level concerns: conversation/model resolution, user-message persistence,
RAG, system-prompt assembly, history trimming, the pending assistant
:class:`~app.models.Message`, and final persistence.

The public entry point is ``ChatService.stream(db, user, request)`` — an async
generator yielding SSE event dicts shaped ``{"event": <name>, "data": {...}}``.
A thin router layer translates these into ``text/event-stream`` frames; this
module stays free of FastAPI types so it can be unit-tested in isolation.

Pipeline:
  1. Resolve or create the conversation; resolve the ModelConfig to use.
  2. Persist the user message (skipped on regenerate).
  3. Build the message list: system prompt (+ optional RAG context + citations).
  4. Trim history to fit ``cfg.max_context_tokens`` (tiktoken-based, oldest first).
  5. Create a pending assistant Message row; emit a ``meta`` event.
  6. Build an :class:`AgentTurnContext` and delegate to the orchestrator, which
     runs the chosen runtime and yields unified :class:`AgentEvent`s. Terminal
     ``done``/``error`` events are intercepted here to finalize persistence.
"""
from __future__ import annotations

import asyncio
import logging
import time
import uuid
from dataclasses import replace
from typing import Any, AsyncIterator

import tiktoken
from sqlalchemy import delete, desc, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.agents.db_mutation import (
    commit_with_rollback,
    db_mutation_scope,
)
from app.agents.intent_router import decide_route, decide_route_with_intent
from app.agents.orchestrator import chat_orchestrator
from app.agents.persistence import persist_continuation_checkpoint
from app.agents.planning import (
    extract_goal,
    is_casual_question,
    should_recognize_intent,
    should_summarize,
    summarize_history,
)
from app.agents.schemas import (
    AgentEvent,
    AgentTurnContext,
    ExecutionMode,
)
from app.agents.context_manager import ContextManager
from app.agents.output_spill import production_spill_writer
from app.agents.state_store import load_state, save_summary, upsert_goal
from app.agents.token_budget import (
    PROMPT_TOO_LARGE,
    PromptAdmissionError,
    admit_latest_turn,
    calculate_prompt_budget,
)
from app.core.config import get_settings
from app.core.exceptions import AppException
from app.db import AsyncSessionLocal
from app.quotas import QuotaExceeded, get_quota_service
from app.models import AgentRun, Conversation, KnowledgeBase, Message, ModelConfig, ToolCall, User
from app.model_capabilities import capabilities_from_config
from app.providers.registry import get_provider_for_config
from app.rag.citations import sanitize_unbacked_source_markers
from app.rag.rag_service import rag_service
from app.schemas import ChatRequest, Citation
from app.services.title_service import (
    is_default_title,
    maybe_autotitle,
    maybe_autotitle_after_answer,
)


async def _refine_title_after_answer(
    conversation_id: str,
    cfg: ModelConfig,
    first_user_message: str,
    assistant_prefix: str,
) -> None:
    """Background LLM title refinement (fire-and-forget after the SSE done).

    Owns its DB session (the request session may be closed by the time the
    LLM responds) and reloads the conversation so a user rename that landed
    in the meantime wins. Every failure is swallowed — titling is cosmetic.
    """
    try:
        async with AsyncSessionLocal() as db:
            conversation = await db.get(Conversation, uuid.UUID(conversation_id))
            if conversation is None:
                return
            await maybe_autotitle_after_answer(
                db,
                conversation,
                cfg,
                first_user_message=first_user_message,
                assistant_prefix=assistant_prefix,
            )
    except Exception:  # noqa: BLE001 — cosmetic; never surface
        logger.debug("background title refinement failed", exc_info=True)
from app.services.attachment_service import (
    collect_image_parts,
    resolve_and_bind_attachments,
    smart_attachment_text,
)

logger = logging.getLogger(__name__)

# Fallback context budget when a config has no usable token limit configured.
_DEFAULT_MAX_CONTEXT_TOKENS = 8192

# Rough chars-per-token used for naive fallback counting when tiktoken has no
# encoding for a model (e.g. obscure model names). Keeps trimming conservative.
_CHARS_PER_TOKEN = 4

# Conservative accounting for image inputs. Provider-side image tokenization
# depends on dimensions/detail and cannot be derived from the base64 byte size;
# reserve fixed prompt headroom per image while counting text parts normally.
_IMAGE_INPUT_TOKEN_RESERVE = 1024

# finish_reason → consumer-facing generation status, persisted in message
# metadata so the UI can tell a real completion apart from a truncation,
# timeout, cancel, or failure. The old code collapsed everything non-cancelled
# to "complete", hiding length/timeout truncations.
_FINISH_STATUS: dict[str, str] = {
    "stop": "complete",
    "tool_calls": "complete",
    "length": "truncated",
    "budget": "truncated",
    "cancelled": "cancelled",
    "timeout": "error",
    "content_filter": "error",
    "provider_error": "error",
    "stream_disconnected": "interrupted",
    "error": "error",
}

# ev_error `code` → finish_reason, so a provider timeout is recorded as
# finish_reason="timeout" instead of a generic "error".
_ERROR_FINISH: dict[str, str] = {
    "agent_budget_exceeded": "budget",
    "provider_timeout": "timeout",
    "provider_error": "provider_error",
    "stream_disconnected": "stream_disconnected",
}


def _status_for_finish(finish_reason: str) -> str:
    if not finish_reason:
        return "complete"
    return _FINISH_STATUS.get(finish_reason, "error")


def _finish_for_error_code(code: str | None) -> str:
    if not code:
        return "error"
    return _ERROR_FINISH.get(code, "error")


def _apply_usage_accounting(
    message: Message,
    model_name: str | None,
    usage: dict[str, Any] | None,
) -> None:
    """Persist one already-aggregated turn usage payload and its total cost."""
    from app.core.pricing import normalize_usage, usage_cost

    normalized = normalize_usage(usage)
    if normalized is None:
        return
    message.prompt_tokens = normalized["prompt_tokens"]
    message.completion_tokens = normalized["completion_tokens"]
    message.total_tokens = normalized["total_tokens"]
    message.cost_usd = usage_cost(model_name, usage)
    message.metadata_ = {
        **(message.metadata_ or {}),
        "usage": dict(usage or {}),
    }


async def _charge_quota_if_enabled(tenant_id: str, message: Message) -> None:
    """Charge the tenant's quota counters from the just-persisted message usage.

    Reads the authoritative ``Message`` token/cost fields written by
    :func:`_apply_usage_accounting` (server-computed; never client-supplied) and
    forwards them to the quota service. No-op when quotas are disabled (the
    default + test env), so this call site is inert unless an operator opts in
    via ``QUOTAS_ENABLED=true``.
    """
    svc = get_quota_service()
    if not svc.enabled:
        return
    prompt = int(message.prompt_tokens or 0)
    completion = int(message.completion_tokens or 0)
    if prompt == 0 and completion == 0:
        return  # nothing to charge (e.g. a no-usage mock turn)
    try:
        await svc.charge_usage(
            tenant_id,
            prompt_tokens=prompt,
            completion_tokens=completion,
            cost_usd=float(message.cost_usd or 0.0),
        )
    except QuotaExceeded:
        # Post-usage overage (tenant crossed the cap mid-period). Surface the
        # admin-visible reason via the turn's metadata so the operator sees it;
        # we do not fail the turn that already produced this output.
        logger.warning(
            "quota overage for tenant %s after turn usage charge", tenant_id
        )


def _log_turn_outcome(
    label: str,
    *,
    sel: Any,
    route: Any,
    is_demo: bool,
    rag_requested: bool,
    rag_used: bool,
    rag_skipped_reason: str | None,
    citation_count: int,
    extra: dict[str, Any] | None = None,
) -> None:
    """Emit one structured per-turn log line for production tracing.

    Called from the done / error / cancel terminal paths so EVERY turn —
    including failed and cancelled ones — leaves a complete record of how it was
    routed, whether multi-agent actually executed, whether demo ran, and the RAG
    outcome. No API keys or document content are logged.
    """
    fields: dict[str, Any] = {
        "requested_mode": getattr(sel, "requested_mode", None) or getattr(route, "requested_mode", "auto"),
        "effective_mode": getattr(sel, "effective_mode", None) or getattr(route, "mode", "auto"),
        "requested_runtime": getattr(sel, "requested_runtime", None) or "native",
        "effective_runtime": getattr(sel, "selected_runtime", None) or "native",
        "agent_profile": getattr(sel, "agent_profile", None) or getattr(route, "agent_profile", "general"),
        "multi_agent_requested": bool(getattr(sel, "multi_agent_requested", False)),
        "multi_agent_executed": bool(getattr(sel, "multi_agent_executed", False)),
        "is_demo": is_demo,
        "rag_requested": rag_requested,
        "rag_used": rag_used,
        "rag_skipped_reason": rag_skipped_reason,
        "citation_count": citation_count,
        "fallback_reason": getattr(sel, "fallback_reason", None),
    }
    if extra:
        fields.update(extra)
    logger.info("turn_outcome label=%s %s", label, " ".join(f"{k}={v}" for k, v in fields.items()))

# Default system prompt when a conversation defines none.
_DEFAULT_SYSTEM_PROMPT = "You are a helpful, concise assistant."

# Anti-"fake multi-agent": a single model must not claim to have launched
# multiple agents / sub-models / roles unless the runtime actually did. Without
# this, a native fallback answering "use multiple agents to debate X vs Y" would
# role-play several agents in prose and look like a real multi-agent run.
#
# Important: this only FORBIDS fabricating/role-playing multi-agent work — it
# must NOT make the model prepend a "I didn't really run multiple agents"
# disclaimer. That disclosure is already handled in the UI (the fallback toast
# in useChatStream), so a prose disclaimer is redundant and noisy. The model
# should simply answer as a single assistant without commentary on the runtime.
_MULTI_AGENT_HONESTY = (
    "多 Agent 诚实约束：当你作为单一模型直接作答时（即本轮并非由多 Agent 运行时真实执行），"
    "不要在回答中声称、扮演或模拟“多个 Agent / 子模型 / 角色 / 并行执行”在协作——"
    "直接以单一助手身份给出回答即可，无需就运行时或 Agent 数量做任何声明、解释或免责。"
    "真正由多 Agent 运行时执行的轮次（你会收到其它 Agent 的结构化产出作为上下文）不受此限。"
)

# Prepended when the user enables "agent / tools" mode. Drives iterative tool
# use WITHOUT soliciting a raw chain-of-thought: the model decomposes and acts,
# and the *structured execution trace* (tool calls, steps) is what the UI shows
# — not the model's internal narration. (Replaces the old CoT preamble.)
_AGENT_TASK_PREAMBLE = (
    "你具有工具调用能力（web_search、http_get 等），可以多次调用以获取所需信息。\n"
    "工作方式：\n"
    "1) 如需最新或外部信息，调用工具获取，不要凭空臆造；\n"
    "2) 工具返回不足时可继续调用，但避免重复相同的查询；\n"
    "3) 证据充分后给出简洁、有条理的最终回答，并用 [source N] 标注来源。\n"
    "不要在回答中暴露你的内部推理过程，直接给出结论与依据。"
)


# --------------------------------------------------------------------------- #
# Task 7: the ONE ContextManager. Invocation sites (precise):
#   * ASSEMBLY — here, on BOTH the inline path (``ChatService._run``) and the
#     durable path (``run_durable_turn``): ``assemble_system_prompt`` builds the
#     complete effective system prompt from persisted fragments.
#   * MID-RUN COMPACTION — the NATIVE runtime
#     (``app.agents.runtime.native_runtime``) calls ``compact_async`` (gated by
#     ``should_compact_midrun``) in its per-round loop, with an LLM-backed
#     ``summarize_fn_async`` that reuses ``planning.summarize_prefix``. The
#     heuristic ``_default_summarize_for_compaction`` below is ONLY a unit-test
#     double for the sync ``compact`` path; it is NOT load-bearing in
#     production (the runtime uses the async LLM-backed path).
#   * The workflow engine (Task 6) does NOT yet consume the ContextManager;
#     that wiring is deferred to the workflow-engine task.
# The effective system prompt assembled here is a PURE function of persisted
# fragments (conversation, flow state, active user memories, RAG) — no
# process-local mutable world state — so any worker produces the same prompt.
# --------------------------------------------------------------------------- #
def _default_summarize_for_compaction(older: list[dict]) -> str:
    """Compact, dependency-free summarizer used only if the ContextManager's
    compaction path is invoked without a real LLM summarizer wired. Returns a
    faithful prose roll-up of the older messages so tool-pair-aware compaction
    never silently drops context."""
    parts: list[str] = []
    for m in older:
        role = m.get("role", "msg")
        content = m.get("content") or ""
        if isinstance(content, list):
            content = " ".join(
                p.get("text", "") for p in content if isinstance(p, dict)
            )
        if content:
            parts.append(f"[{role}] {content[:280]}")
    return "Earlier turns summarized:\n" + "\n".join(parts)


_CONTEXT_MANAGER = ContextManager(
    summarize_fn=_default_summarize_for_compaction,
    # Production spill writer: a spilled tool result becomes a first-class,
    # tenant-scoped Artifact (source="spill") whose opaque `artifact:<id>`
    # handle resolves to a downloadable row. The owner_id/run_id are resolved
    # per-turn from the artifact auth context (bound in ChatService._run).
    # Falls through to the temp-file default when no turn is bound (unit tests).
    spill_writer=production_spill_writer,
)


async def _load_active_user_memories(
    db: AsyncSession, user_id: uuid.UUID
) -> list[str]:
    """Return the user's active, non-expired USER-level memory contents.

    These are the opt-in semantic memories (Task 7) folded into the effective
    system prompt each turn. Pure read — no embedding round-trip. Tenant-scoped
    by user_id; never crosses users.
    """
    from datetime import datetime, timezone

    from app.models import UserMemory

    rows = (
        await db.execute(
            select(UserMemory)
            .where(
                UserMemory.user_id == user_id,
                UserMemory.active.is_(True),
            )
            .order_by(UserMemory.updated_at.desc())
            .limit(50)
        )
    ).scalars().all()
    now = datetime.now(timezone.utc)
    from app.core.datetime_utils import is_expired as _is_expired

    return [r.content for r in rows if not _is_expired(r.expires_at, now)]


def _event(name: str, data: dict[str, Any] | None = None) -> dict[str, Any]:
    """Build an SSE envelope dict."""
    return {"event": name, "data": data or {}}


def _safe_int(value: Any, default: int) -> int:
    try:
        v = int(value)
        return v if v > 0 else default
    except (TypeError, ValueError):
        return default


async def _resolve_model_config(
    db: AsyncSession, request: ChatRequest, conversation: Conversation | None
) -> ModelConfig:
    """Pick the ModelConfig to run this turn against.

    Priority: explicit request.model_id > conversation.model_id > any available
    non-embedding config. System-wide (user_id IS NULL) configs count as
    available to everyone.
    """
    cfg_id = request.model_id or (conversation.model_id if conversation else None)
    if cfg_id is not None:
        cfg = await db.get(ModelConfig, cfg_id)
        if cfg is None:
            raise AppException(404, "model_not_found", "Model config not found")
        return cfg

    # Fall back to the first available chat config so a freshly registered user
    # with no personal config can still chat.
    result = await db.execute(
        select(ModelConfig)
        .where(ModelConfig.is_embedding.is_(False))
        .order_by(ModelConfig.created_at.asc())
        .limit(1)
    )
    cfg = result.scalar_one_or_none()
    if cfg is None:
        raise AppException(400, "no_model_configured", "No model is configured yet")
    return cfg


async def _get_or_create_conversation(
    db: AsyncSession, user: User, request: ChatRequest
) -> Conversation:
    """Resolve the conversation for this turn, creating one if needed."""
    if request.conversation_id is not None:
        result = await db.execute(
            select(Conversation).where(Conversation.id == request.conversation_id)
        )
        conv = result.scalar_one_or_none()
        if conv is None:
            raise AppException(404, "conversation_not_found", "Conversation not found")
        if conv.user_id != user.id:
            # Ownership check — never leak another user's conversation.
            raise AppException(403, "forbidden", "Not your conversation")
        return conv

    title = request.content.strip()[:60] if request.content.strip() else "新对话"
    conv = Conversation(
        user_id=user.id,
        title=title,
        knowledge_base_id=request.knowledge_base_id,
    )
    db.add(conv)
    await db.flush()  # populate conv.id without committing
    return conv


async def _load_history(
    db: AsyncSession, conversation_id: uuid.UUID, *, limit: int = 500
) -> list[Message]:
    """Return the most recent ``limit`` conversation messages, oldest-first.

    Capped so a very long conversation doesn't load every message row (with full
    content/metadata) into memory on every turn — trimming + summarization then
    operate within this recent window. ``limit`` defaults high enough that normal
    conversations are unaffected; it's a backstop against pathological histories.
    """
    result = await db.execute(
        select(Message)
        .where(Message.conversation_id == conversation_id)
        .order_by(Message.created_at.desc(), Message.id.desc())
        .limit(limit)
    )
    rows = list(result.scalars().all())
    rows.reverse()  # restore oldest-first for callers
    return rows


def _estimate_tokens(text: str, model_name: str) -> int:
    """Best-effort token count.

    Tries the tiktoken encoding matching ``model_name``; on any failure falls
    back to cl100k_base, then to a character heuristic — we never hard-fail on
    trimming, we just get less precise.
    """
    if not text:
        return 0
    try:
        enc = tiktoken.encoding_for_model(model_name)
        return len(enc.encode(text))
    except Exception:
        pass
    try:
        enc = tiktoken.get_encoding("cl100k_base")
        return len(enc.encode(text))
    except Exception:
        return max(1, len(text) // _CHARS_PER_TOKEN)


def _trim_history(
    messages: list[dict[str, Any]], max_tokens: int, model_name: str
) -> list[dict[str, Any]]:
    """Drop oldest messages until the whole list fits the token budget.

    The very first message (the system prompt) is always preserved. Trimming
    starts from the oldest non-system entry and walks forward — we keep the
    most recent context, mirroring how a human would summarize.
    """
    if not messages or len(messages) <= 1:
        return messages

    # Precompute each message's token cost ONCE. The old loop recomputed the
    # whole-list total on every deletion (re-serializing every message each
    # time) — O(n^2). Here we keep a running total and subtract in O(1).
    costs = [_estimate_message_tokens(message, model_name) for message in messages]
    total = sum(costs)
    if total <= max_tokens:
        return messages

    latest_user_index = next(
        (
            index
            for index in range(len(messages) - 1, 0, -1)
            if messages[index].get("role") == "user"
        ),
        1,
    )
    while latest_user_index > 1 and total > max_tokens:
        # Remove a complete oldest turn where possible, rather than leaving an
        # orphaned assistant/tool response after its user prompt was trimmed.
        cutoff = 2
        if messages[1].get("role") == "user":
            while (
                cutoff < latest_user_index
                and messages[cutoff].get("role") != "user"
            ):
                cutoff += 1
        total -= sum(costs[1:cutoff])
        del messages[1:cutoff]
        del costs[1:cutoff]
        latest_user_index -= cutoff - 1
    return messages


def _estimate_message_tokens(message: dict[str, Any], model_name: str) -> int:
    """Count serialized message text plus conservative multimodal reserves."""
    import json

    content = message.get("content")
    if not isinstance(content, list):
        return _estimate_tokens(
            json.dumps(message, ensure_ascii=False, default=str), model_name
        )

    image_count = 0
    serialized_parts: list[dict[str, Any]] = []
    for part in content:
        if isinstance(part, dict) and part.get("type") == "image_url":
            image_count += 1
            serialized_parts.append({"type": "image_url"})
        elif isinstance(part, dict):
            serialized_parts.append(part)
    text_message = {**message, "content": serialized_parts}
    return _estimate_tokens(
        json.dumps(text_message, ensure_ascii=False, default=str), model_name
    ) + image_count * _IMAGE_INPUT_TOKEN_RESERVE


def _latest_user_turn_tokens(messages: list[dict[str, Any]], model_name: str) -> int:
    """Return the serialized cost of the newest user turn only."""
    for message in reversed(messages):
        if message.get("role") == "user":
            return _estimate_message_tokens(message, model_name)
    return 0


def _admit_and_trim_history(
    messages: list[dict[str, Any]],
    cfg: ModelConfig,
    *,
    model_name: str | None = None,
    tool_schema_tokens: int = 0,
) -> list[dict[str, Any]]:
    """Apply capability-aware admission, then trim only older history."""

    caps = capabilities_from_config(cfg)
    budget = calculate_prompt_budget(
        caps,
        requested_output=caps.max_output_tokens,
        tool_schema_tokens=tool_schema_tokens,
    )
    effective_model = model_name or getattr(cfg, "model_name", "")
    admit_latest_turn(
        _latest_user_turn_tokens(messages, effective_model), budget.input_tokens
    )
    admitted = _trim_history(list(messages), budget.input_tokens, effective_model)
    admitted_total = sum(
        _estimate_message_tokens(message, effective_model) for message in admitted
    )
    if admitted_total > budget.input_tokens:
        raise PromptAdmissionError(
            PROMPT_TOO_LARGE,
            "The protected system prompt and latest message exceed the prompt budget",
        )
    return admitted


def _estimate_available_tool_schema_tokens(
    cfg: ModelConfig, *, enable_tools: bool, route: Any, model_name: str
) -> int:
    """Estimate advertised tool schemas when this turn can expose them."""
    caps = capabilities_from_config(cfg)
    if not enable_tools or not (
        caps.supports_tools or (getattr(cfg, "provider", "") or "") == "mock"
    ):
        return 0
    try:
        import json

        from app.agents.intent_router import filter_tool_names
        from app.tools.registry_init import get_default_registry

        registry = get_default_registry()
        names = [tool.name for tool in registry.list()]
        names = list(filter_tool_names(names, route))
        schemas = registry.openai_schemas(only=names)
        return _estimate_tokens(
            json.dumps(schemas, ensure_ascii=False, default=str), model_name
        )
    except Exception:  # noqa: BLE001 - estimation is best-effort
        logger.warning("tool schema token estimation failed", exc_info=True)
        return 0


def _finalize_prompt_messages(
    messages: list[dict[str, Any]],
    cfg: ModelConfig,
    *,
    enable_tools: bool,
    route: Any,
    image_parts: list[dict[str, Any]],
    audio_parts: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """Authoritatively admit the complete prompt immediately before dispatch."""

    if image_parts or audio_parts:
        _attach_multimodal_parts(messages, image_parts, audio_parts or [])
    tool_schema_tokens = _estimate_available_tool_schema_tokens(
        cfg,
        enable_tools=enable_tools,
        route=route,
        model_name=cfg.model_name,
    )
    return _admit_and_trim_history(
        messages,
        cfg,
        model_name=cfg.model_name,
        tool_schema_tokens=tool_schema_tokens,
    )


def _messages_to_dicts(
    system: str | None, history: list[Message], extra: list[dict[str, Any]] | None = None
) -> list[dict[str, Any]]:
    """Flatten persisted Message rows + a system prompt into provider message dicts.

    ``extra`` carries already-shaped dicts (e.g. tool turns appended during the
    current turn) that are appended verbatim after the persisted history.
    """
    out: list[dict[str, Any]] = []
    if system:
        out.append({"role": "system", "content": system})

    for msg in history:
        role = msg.role
        # Skip persisted system rows — we synthesized the system prompt above
        # (possibly with RAG context). Avoids a duplicate system turn.
        if role == "system":
            continue
        entry: dict[str, Any] = {"role": role, "content": msg.content or ""}
        meta = msg.metadata_ or {}
        # Preserve tool-call linkage so the model sees a coherent transcript.
        if role == "assistant" and meta.get("tool_calls"):
            entry["tool_calls"] = meta["tool_calls"]
        if role == "tool" and meta.get("tool_call_id"):
            entry["tool_call_id"] = meta["tool_call_id"]
            entry["name"] = meta.get("tool_name") or meta.get("name") or ""
        out.append(entry)

    if extra:
        out.extend(extra)
    return out


def _build_system_prompt(conversation: Conversation | None, rag_context: str) -> str:
    """Compose the effective system prompt, prepending any RAG context."""
    base = (conversation.system_prompt if conversation else None) or _DEFAULT_SYSTEM_PROMPT
    if rag_context:
        return (
            "Use the following retrieved context to answer the user's question. "
            "If the context is insufficient, say so. Cite sources by their "
            "[source N] marker when relevant.\n\n"
            f"Context:\n{rag_context}\n\n"
            f"{base}"
        )
    return base


def _inline_attachment_budget(cfg: ModelConfig) -> int:
    """Model-aware char budget for inline attachment text injection.

    A fraction of the model's context window (chars ≈ tokens × 4), capped by a
    hard ceiling so a 128k-context model doesn't try to inline a whole book.
    Docs exceeding this go through per-attachment RAG retrieval instead.
    """
    s = get_settings()
    ctx_tokens = _safe_int(cfg.max_context_tokens, _DEFAULT_MAX_CONTEXT_TOKENS)
    from_fraction = int(ctx_tokens * s.ATTACHMENT_INLINE_FRACTION * _CHARS_PER_TOKEN)
    return max(2000, min(from_fraction, s.ATTACHMENT_INLINE_MAX_CHARS))


async def propose_user_memory(
    db: AsyncSession,
    user: Any,
    content: str,
    *,
    memory_type: str = "fact",
    confidence: float = 0.5,
    source_message_id: uuid.UUID | None = None,
    source_conversation_id: uuid.UUID | None = None,
) -> None:
    """Insert an INACTIVE candidate user memory (B7 auto-proposal).

    Thin direct-DB twin of the /api/memories propose endpoint so the chat turn
    doesn't need a MemoryService (embedder/Qdrant): candidates are inert rows
    until the user activates them (activation embeds them via the endpoint).
    Exact-content dedup keeps repeat mentions from piling up.
    """
    from app.models import UserMemory

    user_id = getattr(user, "id", user)
    content = (content or "").strip()
    if not content:
        return
    existing = (
        await db.execute(
            select(UserMemory).where(
                UserMemory.user_id == user_id,
                UserMemory.content == content,
            )
        )
    ).scalar_one_or_none()
    if existing is not None:
        return
    db.add(
        UserMemory(
            user_id=user_id,
            memory_type=memory_type,
            content=content,
            confidence=confidence,
            source_message_id=source_message_id,
            source_conversation_id=source_conversation_id,
            active=False,  # opt-in: never active on creation
        )
    )
    await db.flush()


def _augment_with_attachments(
    user_content: str, attachment_text: str, max_chars: int = 8000
) -> str:
    """Append parsed attachment text to the user content for this turn.

    The attachment bytes live in storage; only the extracted text is spliced in
    so a text-only model can reason over the file. Larger files / structured
    data are meant to go through file tools in data_analysis mode; here we keep
    a bounded inline snippet so it never blows the context budget.
    """
    snippet = (attachment_text or "").strip()
    if not snippet:
        return user_content
    # Bound the inline injection; full text stays on the attachment row.
    if len(snippet) > max_chars:
        snippet = snippet[:max_chars] + "\n…（内容已截断，完整内容见附件）"
    return f"{user_content}\n\n[附件内容]\n{snippet}"


def _attach_image_parts(messages: list[dict[str, Any]], image_parts: list[dict[str, Any]]) -> None:
    """Back-compat wrapper: attach image parts only."""
    _attach_multimodal_parts(messages, image_parts, [])


def _attach_multimodal_parts(
    messages: list[dict[str, Any]],
    image_parts: list[dict[str, Any]],
    audio_parts: list[dict[str, Any]],
) -> None:
    """Convert the latest user message to multimodal content with image/audio parts.

    OpenAI multimodal format: ``content`` becomes a list of parts —
    ``{type: text}``, ``{type: image_url}`` (base64 data URL), and
    ``{type: input_audio}`` (base64 + format). The text part preserves
    whatever the user typed + inline attachment text. We mutate the LAST user
    message in-place (the current turn). History turns keep plain-string
    content — old media turns are reconstructed from extracted text only,
    matching the common pattern of not re-sending media from prior turns.
    """
    for msg in reversed(messages):
        if msg.get("role") != "user":
            continue
        existing = msg.get("content")
        text = existing if isinstance(existing, str) else ""
        parts: list[dict[str, Any]] = [{"type": "text", "text": text}] if text else []
        for ip in image_parts:
            parts.append({"type": "image_url", "image_url": {"url": ip["data_url"]}})
        for ap in audio_parts:
            parts.append(
                {
                    "type": "input_audio",
                    "input_audio": {"data": ap["b64"], "format": ap["format"]},
                }
            )
        msg["content"] = parts
        return


async def _delete_last_assistant_message(
    db: AsyncSession, conversation_id: uuid.UUID
) -> str:
    """Regenerate helper: drop the trailing assistant turn and return the
    preceding user content so the turn can be replayed."""
    result = await db.execute(
        select(Message)
        .where(Message.conversation_id == conversation_id)
        .order_by(desc(Message.created_at))
        .limit(2)
        .options(selectinload(Message.tool_calls))
    )
    tail = list(result.scalars().all())
    if not tail:
        raise AppException(400, "nothing_to_regenerate", "Nothing to regenerate")

    last = tail[0]
    if last.role == "assistant":
        # Cascade should handle ToolCall rows, but be explicit to be safe.
        await db.execute(delete(ToolCall).where(ToolCall.message_id == last.id))
        await db.delete(last)
        await db.flush()
        if len(tail) >= 2 and tail[1].role == "user":
            return tail[1].content or ""
        raise AppException(400, "nothing_to_regenerate", "Nothing to regenerate")
    # Last message is already a user message (e.g. previous generation failed).
    return last.content or ""


async def _persist_partial(db: AsyncSession, assistant_msg: Message) -> None:
    """Best-effort flush of the assistant message's current content/metadata.

    Used when the client disconnects mid-stream so a partial reply is still
    recoverable from the conversation history.
    """
    try:
        if not assistant_msg.content:
            assistant_msg.content = ""
        await commit_with_rollback(db)
    except asyncio.CancelledError:
        raise
    except Exception:  # pragma: no cover - best effort only
        pass


async def _persist_continuation_checkpoint(
    session_factory: Any,
    assistant_msg: Message,
    run_id: uuid.UUID | str,
    checkpoint: dict[str, Any],
) -> None:
    """Durably checkpoint without attaching request-session ORM objects."""
    try:
        run_uuid = run_id if isinstance(run_id, uuid.UUID) else uuid.UUID(str(run_id))
    except (TypeError, ValueError):
        run_uuid = None
    await persist_continuation_checkpoint(
        session_factory,
        message_id=assistant_msg.id,
        run_id=run_uuid,
        content=assistant_msg.content or "",
        metadata=dict(assistant_msg.metadata_ or {}),
        checkpoint=checkpoint,
    )


class ChatService:
    """Orchestrates a single chat turn end to end."""

    def __init__(self, persistence_session_factory: Any = AsyncSessionLocal) -> None:
        self._persistence_session_factory = persistence_session_factory

    async def stream(
        self, db: AsyncSession, user: User, request: ChatRequest
    ) -> AsyncIterator[dict[str, Any]]:
        """Yield SSE event dicts for one chat turn.

        Always yields at least one event. On any unrecoverable failure inside
        the pipeline an ``error`` event is emitted and the generator stops, so a
        router can rely on the stream terminating cleanly.

        Quota admission: when ``QUOTAS_ENABLED`` is on, a run slot is reserved
        here (admit_run) and returned in a ``finally`` (release_run). A tenant
        over the concurrent/token/cost cap receives a ``quota_exceeded`` error
        event carrying the admin-visible reason + the quota details. Disabled by
        default + in test, so this is inert unless an operator opts in.
        """
        tenant = str(user.id)
        quota_svc = get_quota_service()
        quota_ticket = None
        try:
            if quota_svc.enabled:
                try:
                    quota_ticket = await quota_svc.admit_run(tenant)
                except QuotaExceeded as exc:
                    yield _event(
                        "error",
                        {
                            "code": "quota_exceeded",
                            "message": exc.reason,
                            "quota": exc.to_dict(),
                        },
                    )
                    return
            async for evt in self._run(db, user, request):
                yield evt
        except asyncio.CancelledError:
            # Client gone — nothing more to emit; partial state already saved
            # inside _run's finally handling. Re-raise so the ASGI server sees it.
            raise
        except AppException as exc:
            yield _event("error", {"code": exc.code, "message": exc.message})
        except Exception as exc:  # pragma: no cover - defensive last resort
            logger.exception("unexpected error during chat: %s", exc)
            yield _event("error", {"code": "internal", "message": "Internal error"})
        finally:
            # release_run is idempotent (set-remove), so a retry/cancellation
            # double-release here is a safe no-op.
            if quota_ticket is not None:
                try:
                    await quota_svc.release_run(tenant, quota_ticket)
                except Exception:  # noqa: BLE001 — release must never break shutdown
                    pass

    async def _run(
        self, db: AsyncSession, user: User, request: ChatRequest
    ) -> AsyncIterator[dict[str, Any]]:
        turn_started = time.monotonic()  # for per-message latency accounting
        # Bind the artifact auth context for this turn so any spill (oversized
        # tool results spilled by the ToolGateway, Task-10 wiring) persists the
        # blob as a real, tenant-scoped Artifact owned by this user, attributed
        # to the run. Reset in finally so the context never leaks across turns.
        from app.artifacts.context import (
            reset_artifact_spill_context,
            set_artifact_spill_context,
        )

        _art_ctx_token = set_artifact_spill_context(
            owner_id=user.id,
            db_factory=self._persistence_session_factory,
            run_id=None,
        )
        try:
            async for evt in self._run_with_artifact_ctx(db, user, request):
                yield evt
        finally:
            reset_artifact_spill_context(_art_ctx_token)

    async def _run_with_artifact_ctx(
        self, db: AsyncSession, user: User, request: ChatRequest
    ) -> AsyncIterator[dict[str, Any]]:
        turn_started = time.monotonic()  # for per-message latency accounting
        # 1. Resolve conversation + model.
        conversation = await _get_or_create_conversation(db, user, request)
        cfg = await _resolve_model_config(db, request, conversation)

        # 1b. Resolve the execution route from the user-facing mode. The UI sends
        # ``mode`` (auto | search | deep_research | create | data_analysis); the
        # intent router turns it into execution_mode / profile / tools. Legacy
        # explicit execution_mode='agent' (with default mode) still forces the
        # multi-agent runtime so existing clients/tests keep working.
        # Effective KB set: explicit per-turn multi-select wins, else the legacy
        # single id, else the conversation's stored KB. ``kb_explicit`` records
        # whether the user selected a KB *this turn* (vs. inheriting the
        # conversation's bound KB) — logged for observability so we can tell a
        # deliberate KB query apart from an inherited one.
        kb_ids: list[uuid.UUID] = list(request.knowledge_base_ids or [])
        kb_explicit = bool(kb_ids)
        if not kb_ids and request.knowledge_base_id is not None:
            kb_ids = [request.knowledge_base_id]
            kb_explicit = True
        elif not kb_ids and conversation.knowledge_base_id is not None:
            kb_ids = [conversation.knowledge_base_id]
            kb_explicit = False
        # Ownership: a user may only run against their own (or system-wide) model
        # config and their own knowledge bases — never another user's.
        if cfg.user_id is not None and cfg.user_id != user.id:
            raise AppException(404, "model_not_found", "Model config not found")
        kb_names: list[str] = []
        if kb_ids:
            # Batch-load KBs in one query instead of one db.get per id (N+1).
            kb_rows = (
                await db.execute(select(KnowledgeBase).where(KnowledgeBase.id.in_(kb_ids)))
            ).scalars().all()
            kb_by_id = {kb.id: kb for kb in kb_rows}
            for _kb_id in kb_ids:
                kb_row = kb_by_id.get(_kb_id)
                # Same rule as the KB API: admins may use any KB; users only
                # their own. (Admins SEE every KB in the picker — dropping
                # their selection here silently produced "no knowledge base"
                # answers.)
                if kb_row is None or (kb_row.user_id != user.id and user.role != "admin"):
                    raise AppException(404, "knowledge_base_not_found", "Knowledge base not found")
                kb_names.append(kb_row.name or str(_kb_id))
            # Bind the selection to the conversation so it survives refreshes
            # and the detail refetch restores the picker state.
            if conversation.knowledge_base_id != kb_ids[0]:
                conversation.knowledge_base_id = kb_ids[0]
        route = decide_route(
            request.mode,
            has_knowledge_base=bool(kb_ids),
            has_attachment=bool(request.attachment_ids),
            user_content=request.content or "",
        )
        enable_tools = route.enable_tools or request.enable_tools
        execution_mode = route.execution_mode
        agent_profile = route.agent_profile
        if (request.execution_mode or "auto").lower() == "agent" and request.mode == "auto":
            execution_mode = ExecutionMode.agent
            agent_profile = request.agent_profile or "deep_research"
            enable_tools = True
            route = replace(
                route,
                execution_mode=execution_mode,
                agent_profile=agent_profile,
                enable_tools=True,
                use_multi_agent=True,
            )

        # 2. Persist the user message (unless regenerating).
        user_content = request.content or ""
        # Raw user input BEFORE attachment augmentation — used for the
        # conversation auto-title (augmented text would leak chunk headers
        # into the sidebar).
        user_content_original = (request.content or "").strip()
        if request.regenerate:
            user_content = await _delete_last_assistant_message(db, conversation.id)
        else:
            if user_content.strip():
                # Persist the send parameters on the user message so the client
                # can rebuild its "last send" state after a page refresh
                # (regenerate / continue-generation replay). Without this the
                # frontend's in-memory lastSendRef is lost and those buttons
                # silently no-op after a reload.
                user_msg = Message(
                    conversation_id=conversation.id,
                    role="user",
                    content=user_content,
                    metadata_={
                        "send_params": {
                            "mode": request.mode or "speed",
                            "model_id": str(request.model_id) if request.model_id else None,
                            "knowledge_base_ids": [str(k) for k in (kb_ids or [])],
                            "attachment_ids": [str(a) for a in (request.attachment_ids or [])],
                        }
                    },
                )
                db.add(user_msg)
                await db.flush()
                # Bind chat attachments to this user message (ownership-checked).
                if request.attachment_ids:
                    try:
                        summaries, _full_attachment_text = await resolve_and_bind_attachments(
                            db, user.id, conversation.id, user_msg.id, request.attachment_ids
                        )
                        user_msg.metadata_ = {**(user_msg.metadata_ or {}), "attachments": summaries}
                        # Smart-hybrid attachment text: small docs inlined verbatim,
                        # oversized ones replaced by query-relevant chunks (RAG).
                        attachment_text = await smart_attachment_text(
                            db, user.id, conversation.id, request.attachment_ids,
                            user_content, _inline_attachment_budget(cfg),
                        )
                        if attachment_text:
                            user_content = _augment_with_attachments(
                                user_content, attachment_text, _inline_attachment_budget(cfg)
                            )
                    except AppException:
                        raise
                    except Exception as exc:  # noqa: BLE001 — attachments are best-effort
                        logger.warning("attachment binding failed: %s", exc)
                # Cheap sidebar preview (last user message text).
                conversation.last_message_preview = (user_content or "").strip()[:280]

                # Auto-title (ChatGPT-style): an untitled conversation gets an
                # immediate truncated-title from the first user message; the
                # LLM refinement runs after the answer completes (below).
                # User-renamed conversations are never touched.
                if user_msg and conversation and is_default_title(conversation.title):
                    try:
                        await maybe_autotitle(
                            db,
                            conversation,
                            cfg,
                            first_user_message=request.content or "",
                        )
                    except Exception:  # noqa: BLE001 — titling never blocks
                        logger.debug("auto-title (fallback) failed", exc_info=True)

                # Auto memory proposal (B7): extract 0-3 candidate memories
                # (rule-based, no model calls) and persist them INACTIVE for
                # review in settings. Best-effort — never blocks the turn.
                if get_settings().MEMORY_AUTO_PROPOSE:
                    try:
                        from app.agents.memory_auto_propose import extract_memory_candidates

                        for cand in extract_memory_candidates(request.content or ""):
                            try:
                                await propose_user_memory(
                                    db,
                                    user,
                                    cand.content,
                                    memory_type=cand.memory_type,
                                    confidence=cand.confidence,
                                    source_message_id=user_msg.id,
                                    source_conversation_id=conversation.id,
                                )
                            except Exception:  # noqa: BLE001 — per-candidate
                                logger.debug("memory candidate rejected", exc_info=True)
                    except Exception:  # noqa: BLE001 — feature is optional
                        logger.debug("auto memory proposal skipped", exc_info=True)

        # 3. System prompt + optional RAG retrieval.
        # RAG is SKIPPED for social/capability chit-chat ("你好", "你是谁",
        # "你都能干什么", "谢谢", …) so a knowledge base bound to the
        # conversation never leaks into casual answers. An explicit per-turn KB
        # selection still retrieves for genuine questions. rag_skipped_reason
        # records WHY retrieval did not run (no KB / casual / retrieval error)
        # for observability.
        citations: list[Citation] = []
        rag_context = ""
        rag_requested = False
        rag_skipped_reason: str | None = None
        if not kb_ids:
            rag_skipped_reason = "no_knowledge_base"
        elif get_settings().RAG_SKIP_CASUAL and is_casual_question(user_content):
            rag_skipped_reason = "casual_question"
        else:
            rag_requested = True
            try:
                rag_context, citations = await rag_service.retrieve(
                    db, user_content, kb_ids, top_k=5
                )
            except Exception as exc:
                # RAG is best-effort: a retrieval failure must not kill the chat.
                logger.warning("RAG retrieval failed, continuing without context: %s", exc)
                rag_context, citations = "", []
                rag_skipped_reason = "retrieval_error"
        rag_used = bool(rag_context)
        logger.info(
            "rag_decision rag_requested=%s rag_used=%s rag_skipped_reason=%s "
            "kb_explicit=%s citation_count=%d",
            rag_requested, rag_used, rag_skipped_reason, kb_explicit, len(citations),
        )

        if citations:
            yield _event(
                "citations",
                {"citations": [c.model_dump(mode="json") for c in citations]},
            )

        # 3b. Load cross-turn state (goal + rolling summary + facts).
        flow_state = await load_state(db, conversation.id, user.id)

        system_prompt = _build_system_prompt(conversation, rag_context)
        # Task 7: assemble the COMPLETE effective system prompt via the ONE
        # ContextManager — a pure function of persisted fragments (no
        # process-local mutable world state). The tool-use preamble, rolling
        # summary, ongoing goal, and the user's opt-in semantic memories are
        # all folded in here; the single-model honesty backstop closes it.
        active_user_memories = await _load_active_user_memories(db, user.id)
        behavior_blocks: list[str] = []
        if enable_tools:
            behavior_blocks.append(_AGENT_TASK_PREAMBLE)
        behavior_blocks.append(_MULTI_AGENT_HONESTY)
        system_prompt = _CONTEXT_MANAGER.assemble_system_prompt(
            base=system_prompt,
            rag_context="",  # already folded into base by _build_system_prompt
            summary=flow_state.conversation_summary or "",
            goal=flow_state.user_goal or "",
            memories=active_user_memories,
            intent_block=None,
            behavior_blocks=behavior_blocks,
        )

        # Remember the user's goal for this conversation (single 'task' memory).
        if user_content.strip():
            await upsert_goal(
                db, conversation.id, user.id, extract_goal(user_content),
                source_message_id=None,
            )

        # 4. Early admission uses the largest possible input budget (no tool
        # schema reserve), so it can fast-fail impossible latest turns without
        # rejecting a turn whose intent routing later disables tools. A final,
        # authoritative admission runs after routing/enrichment/images below.
        history = await _load_history(db, conversation.id)
        messages = _messages_to_dicts(system_prompt, history)
        messages = _admit_and_trim_history(
            messages,
            cfg,
            model_name=cfg.model_name,
            tool_schema_tokens=0,
        )

        # 4b. Gather multimodal image parts (data only — do NOT mutate messages
        # yet). The actual content-parts injection happens after intent
        # recognition / context enrichment (those read message content as a
        # plain string and would otherwise skip the current user turn).
        _image_parts: list[dict[str, Any]] = []
        if cfg.supports_vision and request.attachment_ids:
            try:
                _image_parts = await collect_image_parts(
                    db, user.id, conversation.id, request.attachment_ids
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning("collect_image_parts failed: %s", exc)
                _image_parts = []

        # 4b'. Gather audio parts for audio-input models (OpenAI input_audio
        # format). Text-only / non-audio models keep the transcription path
        # (audio parsing extracts text when a transcript is available).
        _audio_parts: list[dict[str, Any]] = []
        if getattr(cfg, "supports_audio_input", False) and request.attachment_ids:
            try:
                from app.services.attachment_service import collect_audio_parts

                _audio_parts = await collect_audio_parts(
                    db, user.id, conversation.id, request.attachment_ids
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning("collect_audio_parts failed: %s", exc)
                _audio_parts = []

        # 6a. Intent recognition (engineering-grade, model-driven). Assemble typed
        # context fragments, run the classifier, and let a trusted judgment steer
        # routing — replacing the silent keyword guessing that mis-routed code
        # requests into the research crew. Skipped for casual/short turns and
        # when the user explicitly forced agent mode (explicit intent wins).
        intent_decision = None
        intent_fragment_names: list[str] = []
        agent_forced = (request.execution_mode or "auto").lower() == "agent" and request.mode == "auto"
        # 极速 / 专家 两种模式都跳过意图分类：极速要首字最快、专家本就固定走多 Agent。
        _mode_skips_intent = (request.mode or "speed").strip().lower() in ("speed", "expert")
        if not agent_forced and not _mode_skips_intent and should_recognize_intent(user_content):
            from app.agents.context_fragments import (
                IntentContextInput,
                assemble_context_fragments,
                fragment_names,
                recognized_intent_fragment,
            )
            from app.agents.intent_service import intent_service
            from app.providers.registry import get_provider_for_config

            _ctx_fragments = assemble_context_fragments(
                IntentContextInput(
                    mode=request.mode,
                    user_content=user_content,
                    kb_names=tuple(kb_names),
                    attachment_descriptors=(
                        (f"{len(request.attachment_ids)} 个附件",) if request.attachment_ids else ()
                    ),
                    messages=messages,
                )
            )
            try:
                intent_decision = await intent_service.judge(
                    user_content=user_content,
                    fragments=_ctx_fragments,
                    provider=get_provider_for_config(cfg),
                )
            except Exception:  # noqa: BLE001 — intent is an enhancement, never fatal
                logger.warning("intent recognition raised; using keyword route", exc_info=True)
                intent_decision = None

            if intent_decision is not None:
                intent_fragment_names = fragment_names(_ctx_fragments)
                route = decide_route_with_intent(
                    request.mode,
                    user_content=user_content,
                    intent=intent_decision,
                    has_knowledge_base=bool(kb_ids),
                    has_attachment=bool(request.attachment_ids),
                )
                enable_tools = route.enable_tools or request.enable_tools
                execution_mode = route.execution_mode
                agent_profile = route.agent_profile
                # Surface the recognized intent to the answering model (the native
                # runtime sends ctx.messages[0] as the system message) so it
                # self-judges intent instead of relying on a hidden router.
                _intent_block = recognized_intent_fragment(intent_decision).render()
                if _intent_block:
                    system_prompt = system_prompt + "\n\n" + _intent_block
                    if messages and messages[0].get("role") == "system":
                        messages[0]["content"] = (
                            messages[0].get("content") or ""
                        ) + "\n\n" + _intent_block

        # 6b. Context enrichment (Codex incremental-context pattern). Assemble the
        # stable behavioral/environment fragments + resolved $skill mentions, and
        # inject only the ones that CHANGED since last turn (world-state diffing)
        # so we don't re-pay their token cost every turn. Best-effort: a failure
        # here never breaks the turn.
        try:
            import os as _os
            from pathlib import Path as _Path

            from app.agents.answer_format import answer_format_fragment
            from app.agents.behavior_fragments import (
                mode_behavior_fragment,
                multi_agent_mode_fragment,
                personality_fragment,
            )
            from app.agents.context_fragments import render_fragments
            from app.agents.project_instructions import (
                load_project_instructions,
                project_instructions_fragment,
            )
            from app.agents.skills.loader import (
                load_skills,
                resolve_mentions,
                skill_fragment,
            )
            from app.agents.world_state import differ_for

            _settings = get_settings()
            _cwd = getattr(_settings, "PROJECT_ROOT", None) or _os.getcwd()
            _skill_roots = [_Path(p) for p in (getattr(_settings, "SKILLS_ROOTS", None) or [])]

            # Stable fragments (diffed across turns — unchanged ones emit nothing).
            _stable = [
                mode_behavior_fragment(route.mode),
                multi_agent_mode_fragment("explicit"),
                project_instructions_fragment(load_project_instructions(_cwd)),
                answer_format_fragment(),
            ]
            _persona = getattr(request, "personality", None)
            if _persona:
                _stable.append(personality_fragment(str(_persona)))

            # Per-turn skill fragments (mention-based → always injected when present).
            _mentioned = resolve_mentions(user_content, load_skills(_skill_roots))
            _skill_block = render_fragments([skill_fragment(s) for s in _mentioned])

            _changed = differ_for(str(conversation.id)).diff(_stable)
            _stable_block = render_fragments(_changed)
            _enrich = "\n\n".join(b for b in (_stable_block, _skill_block) if b)
            if _enrich:
                system_prompt = system_prompt + "\n\n" + _enrich
                if messages and messages[0].get("role") == "system":
                    messages[0]["content"] = (messages[0].get("content") or "") + "\n\n" + _enrich
        except Exception:  # noqa: BLE001 — enrichment is best-effort, never fatal
            logger.warning("context enrichment failed; continuing without it", exc_info=True)

        # 6c. Final authoritative admission sees the final intent route, all
        # system/enrichment text, advertised tool schemas, and multimodal parts.
        # It is intentionally the last prompt mutation before provider dispatch.
        messages = _finalize_prompt_messages(
            messages,
            cfg,
            enable_tools=enable_tools,
            route=route,
            image_parts=_image_parts,
            audio_parts=_audio_parts,
        )

        # 7. Create the pending assistant only after admission succeeds, so a
        # rejected oversized prompt never leaves a ghost pending response.
        assistant_msg = Message(
            conversation_id=conversation.id,
            role="assistant",
            content="",
            model_name=cfg.model_name,
            metadata_={"status": "pending"},
        )
        db.add(assistant_msg)
        await db.flush()
        await commit_with_rollback(db)  # durable IDs + history before streaming starts

        yield _event(
            "meta",
            {
                "message_id": str(assistant_msg.id),
                "conversation_id": str(conversation.id),
            },
        )

        # 8. Build turn context and delegate to the orchestrator/runtime.
        db_mutation_lock = asyncio.Lock()
        persistence_lock = asyncio.Lock()
        ctx = AgentTurnContext(
            db=db,
            user=user,
            conversation=conversation,
            model_config=cfg,
            request=request,
            user_content=user_content,
            system_prompt=system_prompt,
            messages=messages,
            rag_context=rag_context,
            citations=citations,
            assistant_msg=assistant_msg,
            run_id=uuid.uuid4(),  # placeholder; orchestrator overwrites with the real run id
            execution_mode=execution_mode,
            agent_profile=agent_profile,
            enable_tools=enable_tools,
            knowledge_base_id=kb_ids[0] if kb_ids else None,
            mode=route.mode,
            extra={
                "state": flow_state,
                "route": route,
                "intent_decision": intent_decision,
                "intent_fragments": intent_fragment_names,
                "db_mutation_lock": db_mutation_lock,
                "persistence_session_factory": self._persistence_session_factory,
                "persistence_lock": persistence_lock,
                # Reasoning-effort hint (B6): validated, passed through to the
                # native runtime's provider params when the model supports it.
                "reasoning_effort": (
                    request.reasoning_effort
                    if (request.reasoning_effort or "").lower()
                    in ("low", "medium", "high")
                    else None
                ),
                # Hermes 模式：工具全部在 Hermes 服务端执行（平台只透传流），
                # 平台侧预算（时长/token）会把长任务掐断——单轮真实搜索就能
                # 烧 35k+ tokens。这里放宽到适合 Agent 长任务的量级并授权
                # 提升，使 web 端体验对齐 IM 平台直连。
                **(
                    {
                        "budget_overrides": {
                            "max_runtime_seconds": 900.0,   # 15 分钟
                            "max_total_tokens": 1_000_000,
                            "max_tool_output_chars": 200_000,
                            "max_agent_steps": 64,
                        },
                        "budget_policy_authorized": True,
                    }
                    if route.mode == "hermes"
                    else {}
                ),
            },
        )

        async def persist_continuation(checkpoint: dict[str, Any]) -> None:
            async with db_mutation_scope(persistence_lock):
                await _persist_continuation_checkpoint(
                    self._persistence_session_factory,
                    assistant_msg,
                    ctx.run_id,
                    checkpoint,
                )

        ctx.extra["persist_continuation_checkpoint"] = persist_continuation

        try:
            async for evt in chat_orchestrator.stream(ctx):
                if evt.kind == "done":
                    finish = evt.data.get("finish_reason", "stop")
                    assistant_msg.metadata_ = self._meta(
                        cfg, citations, finish, assistant_msg.metadata_
                    )
                    # Citation integrity: strip any [source N] in the final
                    # answer that has no real backing citation (a model may
                    # hallucinate a marker; the deterministic demo writer emits
                    # them unconditionally). The persisted text is cleaned so a
                    # reload/regenerate shows honest markers; the frontend also
                    # cleans the live stream on render. Flag the turn when any
                    # marker had to be stripped.
                    if assistant_msg.content:
                        _cleaned, _cited_changed = sanitize_unbacked_source_markers(
                            assistant_msg.content, len(citations)
                        )
                        if _cited_changed:
                            assistant_msg.content = _cleaned
                            assistant_msg.metadata_["citation_validation_failed"] = True
                    # Drop the live tool_calls trace from the persisted metadata:
                    # the UI reads the execution trace from ToolCall/AgentStep rows,
                    # and a dangling tool_calls here would make the NEXT turn's
                    # transcript invalid (assistant tool_calls with no matching
                    # role:tool rows → provider HTTP 400).
                    assistant_msg.metadata_.pop("tool_calls", None)
                    if ctx.extra.get("budget"):
                        assistant_msg.metadata_["budget"] = ctx.extra["budget"]
                    if ctx.extra.get("intent"):
                        assistant_msg.metadata_["intent"] = ctx.extra["intent"]
                    # Spilled artifact handles (oversized tool outputs persisted
                    # as downloadable Artifacts this turn) — the UI renders
                    # inline download chips from this list.
                    if ctx.extra.get("spilled_artifacts"):
                        assistant_msg.metadata_["artifacts"] = ctx.extra[
                            "spilled_artifacts"
                        ]
                    if ctx.extra.get("multi_agent"):
                        # Mark multi-agent runs so the UI shows the compact
                        # "查看执行过程" entry (single-agent runs keep ResearchSteps).
                        assistant_msg.metadata_["multi_agent"] = True
                    # Observability: record the runtime selection so the UI can
                    # tell a REAL multi-agent run from a native fallback (and
                    # never mistake a single-model fallback for multi-agent).
                    sel = ctx.extra.get("runtime_selection")
                    if sel is not None:
                        md = assistant_msg.metadata_
                        md["requested_mode"] = sel.requested_mode
                        md["effective_mode"] = sel.effective_mode
                        md["requested_runtime"] = sel.requested_runtime
                        md["effective_runtime"] = sel.selected_runtime
                        md["agent_profile"] = sel.agent_profile
                        md["multi_agent_requested"] = sel.multi_agent_requested
                        md["multi_agent_executed"] = sel.multi_agent_executed
                        if sel.fallback_reason:
                            md["fallback_reason"] = sel.fallback_reason
                    elif route.requested_mode and route.requested_mode != route.mode:
                        assistant_msg.metadata_["requested_mode"] = route.requested_mode
                        assistant_msg.metadata_["effective_mode"] = route.mode
                    # is_demo: True only when the answer came from the
                    # deterministic demo executor (canned, non-real content).
                    # Drives the persistent UI warning; always False on the
                    # public chat path (demo needs an explicit request opt-in).
                    # ``sel`` (runtime_selection) was resolved just above.
                    _is_demo = bool(ctx.extra.get("is_demo")) or bool(
                        getattr(sel, "is_demo", False)
                    )
                    assistant_msg.metadata_["is_demo"] = _is_demo
                    # RAG + observability fields: persisted on the message (so
                    # the debug panel / future turns can read them) AND emitted
                    # as one structured log line per turn for production tracing.
                    assistant_msg.metadata_["rag_requested"] = rag_requested
                    assistant_msg.metadata_["rag_used"] = rag_used
                    if rag_skipped_reason:
                        assistant_msg.metadata_["rag_skipped_reason"] = rag_skipped_reason
                    assistant_msg.metadata_["citation_count"] = len(citations)
                    # Token / cost accounting: persist usage from the provider
                    # (propagated through the runtime's done event). The provider
                    # parses usage; it used to be discarded — now it answers
                    # "who spent what" and enables per-user budgets.
                    _apply_usage_accounting(
                        assistant_msg, cfg.model_name, evt.data.get("usage")
                    )
                    # Quota charge (Task 11): forward the SERVER-computed usage
                    # to the tenant's quota counters. No-op unless QUOTAS_ENABLED.
                    await _charge_quota_if_enabled(str(user.id), assistant_msg)
                    assistant_msg.latency_ms = int((time.monotonic() - turn_started) * 1000)
                    _log_turn_outcome(
                        "complete",
                        sel=sel,
                        route=route,
                        is_demo=_is_demo,
                        rag_requested=rag_requested,
                        rag_used=rag_used,
                        rag_skipped_reason=rag_skipped_reason,
                        citation_count=len(citations),
                        extra={"finish_reason": finish},
                    )
                    # Refresh the sidebar preview with the final assistant text.
                    conversation.last_message_preview = (assistant_msg.content or "")[:280]
                    async with db_mutation_scope(db_mutation_lock):
                        await commit_with_rollback(db)
                    # Auto-title refinement: now that the answer exists, try
                    # an LLM-generated concise title (replaces the truncated
                    # fallback set at turn start). Fire-and-forget on a fresh
                    # session — the LLM call (1-3s) must not delay the SSE
                    # close; it uses its own DB session and skips
                    # conversations the user renamed in the meantime.
                    asyncio.create_task(
                        _refine_title_after_answer(
                            str(conversation.id),
                            cfg,
                            user_content_original,
                            (assistant_msg.content or "")[:400],
                        )
                    )
                    # Rolling summary: if history grew past the budget, roll the
                    # older messages into a summary memory for future turns.
                    try:
                        await self._maybe_summarize(db, conversation, cfg, user.id)
                    except Exception:  # pragma: no cover - never block done on summary
                        logger.warning("post-turn summary failed", exc_info=True)
                    yield evt.to_sse_envelope()
                    return
                if evt.kind == "error":
                    err_code = evt.data.get("code")
                    err_msg = evt.data.get("message", "error")
                    err_finish = evt.data.get("finish_reason") or _finish_for_error_code(
                        err_code
                    )
                    async with db_mutation_scope(db_mutation_lock):
                        await self._finalize_error(
                            db, assistant_msg, err_msg,
                            finish_reason=err_finish, code=err_code,
                            usage=evt.data.get("usage"),
                            model_name=cfg.model_name,
                            budget=evt.data.get("budget"),
                        )
                    # Structured record for a FAILED turn (same field set as a
                    # completed one) so failed runs are queryable too.
                    sel = ctx.extra.get("runtime_selection")
                    _log_turn_outcome(
                        "failed",
                        sel=sel,
                        route=route,
                        is_demo=bool(ctx.extra.get("is_demo")) or bool(getattr(sel, "is_demo", False)),
                        rag_requested=rag_requested,
                        rag_used=rag_used,
                        rag_skipped_reason=rag_skipped_reason,
                        citation_count=len(citations),
                        extra={"error_code": err_code, "finish_reason": err_finish},
                    )
                    yield evt.to_sse_envelope()
                    return
                yield evt.to_sse_envelope()
        except asyncio.CancelledError:
            # The connection was cancelled. Distinguish a user-initiated stop
            # (the cancel API sets ctl.cancel) from an ungraceful network drop
            # so the persisted status matches what actually happened.
            from app.agents.run_controls import get as get_run_control

            ctl = get_run_control(ctx.run_id)
            reason = (
                "cancelled"
                if (ctl is not None and ctl.cancel.is_set())
                else "stream_disconnected"
            )
            logger.info("chat stream cancelled (%s); saving partial output", reason)
            assistant_msg.metadata_ = self._meta(
                cfg, citations, reason, assistant_msg.metadata_
            )
            # Structured record for a cancelled/disconnected turn too.
            sel = ctx.extra.get("runtime_selection")
            _log_turn_outcome(
                reason,
                sel=sel,
                route=route,
                is_demo=bool(ctx.extra.get("is_demo")) or bool(getattr(sel, "is_demo", False)),
                rag_requested=rag_requested,
                rag_used=rag_used,
                rag_skipped_reason=rag_skipped_reason,
                citation_count=len(citations),
            )
            async with db_mutation_scope(db_mutation_lock):
                await self._finalize_interrupted(
                    db,
                    assistant_msg,
                    finish_reason=reason,
                    usage=ctx.extra.get("usage"),
                    model_name=cfg.model_name,
                )
            raise

    @staticmethod
    def _meta(
        cfg: ModelConfig,
        citations: list[Citation],
        finish_reason: str,
        prev: dict[str, Any] | None,
    ) -> dict[str, Any]:
        metadata: dict[str, Any] = {
            **(prev or {}),
            "model": cfg.model_name,
            "finish_reason": finish_reason,
            "status": _status_for_finish(finish_reason),
        }
        if citations:
            metadata["citations"] = [c.model_dump(mode="json") for c in citations]
        return metadata

    async def _finalize_error(
        self,
        db: AsyncSession,
        assistant_msg: Message,
        message: str,
        *,
        finish_reason: str = "error",
        code: str | None = None,
        usage: dict[str, Any] | None = None,
        model_name: str | None = None,
        budget: dict[str, Any] | None = None,
    ) -> None:
        # Preserve partial content (assistant_msg.content is mutated by the
        # runtime as tokens stream) — only record why it stopped.
        md: dict[str, Any] = {
            **(assistant_msg.metadata_ or {}),
            "finish_reason": finish_reason,
            "status": _status_for_finish(finish_reason),
            "error": message,
        }
        if code:
            md["provider_error_code"] = code
        if budget is not None:
            md["budget"] = dict(budget)
        assistant_msg.metadata_ = md
        _apply_usage_accounting(assistant_msg, model_name, usage)
        await commit_with_rollback(db)

    async def _finalize_interrupted(
        self,
        db: AsyncSession,
        assistant_msg: Message,
        *,
        finish_reason: str,
        usage: dict[str, Any] | None,
        model_name: str | None,
    ) -> None:
        assistant_msg.metadata_ = {
            **(assistant_msg.metadata_ or {}),
            "finish_reason": finish_reason,
            "status": _status_for_finish(finish_reason),
        }
        _apply_usage_accounting(assistant_msg, model_name, usage)
        await _persist_partial(db, assistant_msg)

    async def _maybe_summarize(
        self,
        db: AsyncSession,
        conversation: Conversation,
        cfg: ModelConfig,
        user_id: uuid.UUID,
    ) -> None:
        """Roll older history into a summary memory when the prompt is large.

        Best-effort: a summarization failure is logged and swallowed so it can
        never block the turn. The summary is picked up by ``load_state`` on the
        next turn and injected into the system prompt.
        """
        history = await _load_history(db, conversation.id)
        messages = _messages_to_dicts(None, history)  # no system prompt
        if not messages:
            return
        total = sum(
            _estimate_tokens(str(m.get("content") or ""), cfg.model_name)
            for m in messages
        )
        max_ctx = _safe_int(cfg.max_context_tokens, _DEFAULT_MAX_CONTEXT_TOKENS)
        if not should_summarize(total, max_ctx):
            return
        provider = get_provider_for_config(cfg)
        summary = await summarize_history(provider, messages, keep_recent=6)
        if summary:
            await save_summary(db, conversation.id, user_id, summary)
            await db.commit()

    # ------------------------------------------------------------------ #
    # Task 5: durable dispatch (additive — inline path unchanged)
    # ------------------------------------------------------------------ #
    async def create_and_enqueue_durable_run(
        self, db: AsyncSession, user: User, request: ChatRequest
    ) -> uuid.UUID:
        """Create the turn records and enqueue for background execution.

        Reuses the SAME lower-level helpers as :meth:`_run`
        (:func:`_get_or_create_conversation`, :func:`_resolve_model_config`) so
        conversation/model resolution is identical to the inline path. Creates:

          * the user Message (unless regenerating),
          * a pending assistant Message,
          * a ``pending`` AgentRun (the worker flips it to ``running``).

        Then enqueues ``run.id`` on the run queue and returns it. The caller
        (the chat API) opens an SSE stream tailing the run's durable events so
        the client consumes the result identically to the inline path.

        The heavy context-building (system prompt, RAG, history trimming, intent
        recognition, enrichment) is deferred to :func:`run_durable_turn` which
        the worker invokes — it is NOT duplicated here.
        """
        from datetime import datetime, timezone

        # 1. Resolve conversation + model (same helpers as _run).
        conversation = await _get_or_create_conversation(db, user, request)
        cfg = await _resolve_model_config(db, request, conversation)
        # Ownership (same guard as _run).
        if cfg.user_id is not None and cfg.user_id != user.id:
            raise AppException(404, "model_not_found", "Model config not found")

        # 2. Persist the user message (same shape as _run; no regenerate support
        #    in durable dispatch for now — regenerate is an inline-only path).
        user_content = request.content or ""
        if user_content.strip():
            user_msg = Message(
                conversation_id=conversation.id,
                role="user",
                content=user_content,
            )
            db.add(user_msg)
            await db.flush()

        # 3. Create the pending assistant Message (same shape as _run).
        assistant_msg = Message(
            conversation_id=conversation.id,
            role="assistant",
            content="",
            model_name=cfg.model_name,
            metadata_={"status": "pending"},
        )
        db.add(assistant_msg)
        await db.flush()

        # 4. Create the AgentRun (status=pending; the worker flips it to running).
        snapshot = {
            "provider": cfg.provider,
            "model_name": cfg.model_name,
            "api_base_url": cfg.api_base_url,
            "temperature": cfg.temperature,
            "top_p": cfg.top_p,
            "max_tokens": cfg.max_tokens,
            "supports_tools": getattr(cfg, "supports_tools", False),
        }
        run = AgentRun(
            conversation_id=conversation.id,
            message_id=assistant_msg.id,
            user_id=user.id,
            runtime="native",
            flow_name="native_chat",
            status="pending",
            current_step="",
            input={
                "content": user_content,
                "enable_tools": bool(request.enable_tools),
                "execution_mode": (request.execution_mode or "auto"),
                "agent_profile": request.agent_profile or "general",
                "knowledge_base_id": (
                    str(request.knowledge_base_id) if request.knowledge_base_id else None
                ),
                # Per-turn KB selection (multi-KB) so the worker-side turn can
                # run RAG — without this the durable path always saw no KB.
                "knowledge_base_ids": [str(k) for k in (request.knowledge_base_ids or [])],
                "mode": request.mode or "speed",
            },
            model_config_snapshot=snapshot,
        )
        db.add(run)
        await db.flush()
        conversation.last_message_preview = (user_content or "").strip()[:280]
        # Auto-title fallback for the durable path too (LLM refinement happens
        # in the worker's post-turn hook if configured there).
        if is_default_title(conversation.title) and (user_content or "").strip():
            try:
                await maybe_autotitle(
                    db, conversation, None, first_user_message=user_content, commit=False
                )
            except Exception:  # noqa: BLE001
                logger.debug("durable auto-title failed", exc_info=True)
        await commit_with_rollback(db)

        # 5. Enqueue for the background worker.
        from app.agents.workflow.queue import get_run_queue

        queue = await get_run_queue()
        await queue.enqueue(run.id, db_session_factory=self._persistence_session_factory)
        logger.info(
            "durable dispatch: enqueued run %s for conversation %s (user %s)",
            run.id, conversation.id, user.id,
        )
        return run.id


# --------------------------------------------------------------------------- #
# Task 5: durable execution seam (additive, gated behind BACKGROUND_WORKER)
# --------------------------------------------------------------------------- #
async def maybe_enqueue_durable_run(run_id: uuid.UUID | str) -> bool:
    """When ``BACKGROUND_WORKER`` is the durable mode, enqueue ``run_id`` for the
    background worker. In the default ``inprocess`` mode this is a no-op and the
    existing inline stream handles execution.

    Returns True if the run was enqueued (durable mode), False otherwise.

    This is the single integration point the chat API calls after persisting an
    AgentRun. It does NOT modify the existing ``stream`` path — the inline
    generator runs unchanged when ``BACKGROUND_WORKER=inprocess`` (the default).
    """
    settings = get_settings()
    if settings.BACKGROUND_WORKER == "inprocess":
        return False
    try:
        from app.agents.workflow.queue import get_run_queue
        from app.db import AsyncSessionLocal

        queue = await get_run_queue()
        await queue.enqueue(run_id, db_session_factory=AsyncSessionLocal)
        return True
    except Exception:  # noqa: BLE001 — never crash the request on enqueue failure
        logger.warning("durable enqueue failed for run %s (inline fallback)", run_id, exc_info=True)
        return False


async def _resolve_model_for_durable_run(
    db: AsyncSession, conversation: Conversation
) -> ModelConfig | None:
    """Resolve the ModelConfig for a durable run.

    Prefers the conversation's bound model; falls back to the first available
    chat config so a durable run never dead-ends on model resolution.
    """
    cfg_id = getattr(conversation, "model_id", None)
    if cfg_id is not None:
        cfg = await db.get(ModelConfig, cfg_id)
        if cfg is not None:
            return cfg
    result = await db.execute(
        select(ModelConfig)
        .where(ModelConfig.is_embedding.is_(False))
        .order_by(ModelConfig.created_at.asc())
        .limit(1)
    )
    return result.scalar_one_or_none()


async def run_durable_turn(
    run_id: uuid.UUID | str, session: AsyncSession
) -> AsyncIterator[AgentEvent]:
    """Reconstruct and execute a persisted run's turn; yield each AgentEvent.

    This is the production executor body the durable worker drives. It loads the
    persisted run (+ conversation / user / assistant message / model config),
    rebuilds the turn context using the SAME module-level helpers as
    :meth:`ChatService._run` (system prompt, history load+trim, message list),
    and delegates to :meth:`chat_orchestrator.stream`. Terminal ``done`` /
    ``error`` events are intercepted to finalize the assistant message exactly
    as the inline path does (metadata + usage accounting + sidebar preview).

    Intentionally deferred vs. the inline ``stream`` (these are app-level
    enhancements that require a live request / per-turn inputs the durable path
    does not re-hydrate; a native single-agent turn still executes end-to-end):

      * Intent recognition (extra model call; the route is derived from the
        run's stored ``execution_mode`` instead).
      * Context-enrichment fragments (behavior / project instructions / skills).
      * RAG retrieval + citations (no per-turn KB binding on the durable path).
      * Attachment binding / inline image parts.
      * Cross-turn rolling-summary write-back (``_maybe_summarize``).
      * Goal upsert.

    The orchestrator reuses the EXISTING AgentRun (the durable worker created +
    leased it before calling us) via ``ctx.extra["durable_run_id"]``.
    """
    from app.agents.schemas import ConversationFlowState

    db = session
    run = await db.get(AgentRun, run_id)
    if run is None:
        yield AgentEvent(kind="error", data={"code": "run_not_found", "run_id": str(run_id)})
        return

    conversation = await db.get(Conversation, run.conversation_id)
    if conversation is None:
        yield AgentEvent(
            kind="error",
            data={"code": "conversation_not_found", "run_id": str(run_id)},
        )
        return

    user = await db.get(User, run.user_id) if run.user_id else None
    if user is None:
        yield AgentEvent(
            kind="error", data={"code": "user_not_found", "run_id": str(run_id)}
        )
        return

    assistant_msg = await db.get(Message, run.message_id) if run.message_id else None
    if assistant_msg is None:
        yield AgentEvent(
            kind="error",
            data={"code": "assistant_message_not_found", "run_id": str(run_id)},
        )
        return

    cfg = await _resolve_model_for_durable_run(db, conversation)
    if cfg is None:
        yield AgentEvent(
            kind="error", data={"code": "model_config_not_found", "run_id": str(run_id)}
        )
        return

    # Reconstruct the turn inputs from the persisted run input + route helper.
    run_input = run.input or {}
    user_content = run_input.get("content") or ""
    enable_tools = bool(run_input.get("enable_tools", False))
    try:
        execution_mode = ExecutionMode(run_input.get("execution_mode", "auto"))
    except ValueError:
        execution_mode = ExecutionMode.auto
    agent_profile = run_input.get("agent_profile") or "general"

    # ---- Knowledge bases (per-turn selection persisted on the run) ----
    # Same precedence as the inline ``_run``: explicit multi-select, else the
    # legacy single id, else the conversation's bound KB. Ownership-checked
    # against the requesting user so a forged run.input can't read another
    # user's knowledge bases.
    kb_ids: list[uuid.UUID] = []
    for raw in run_input.get("knowledge_base_ids") or []:
        try:
            kb_ids.append(uuid.UUID(str(raw)))
        except (TypeError, ValueError):
            continue
    if not kb_ids and run_input.get("knowledge_base_id"):
        try:
            kb_ids.append(uuid.UUID(str(run_input["knowledge_base_id"])))
        except (TypeError, ValueError):
            pass
    if not kb_ids and conversation.knowledge_base_id is not None:
        kb_ids = [conversation.knowledge_base_id]
    kb_ids = [
        kb_id
        for kb_id in kb_ids
        if (_kb := await db.get(KnowledgeBase, kb_id)) is not None
        and (_kb.user_id == user.id or user.role == "admin")
    ]
    # Bind the selection to the conversation (single-KB semantics) so the
    # client's detail refetch restores the picker state after the turn.
    if kb_ids and conversation.knowledge_base_id != kb_ids[0]:
        conversation.knowledge_base_id = kb_ids[0]

    # Derive a native single-agent route. Durable execution is additive and
    # currently scoped to native turns; the route's ``use_multi_agent=False``
    # guarantees the orchestrator selects the native runtime.
    route = decide_route(
        run_input.get("mode") or "speed",
        has_knowledge_base=bool(kb_ids),
        has_attachment=False,
        user_content=user_content,
    )
    route = replace(route, use_multi_agent=False)

    # ---- RAG retrieval (mirrors the inline path) ----
    # Best-effort: a retrieval failure degrades to a normal answer, never
    # kills the turn. Casual chit-chat skips retrieval so a KB bound to the
    # conversation doesn't leak into greetings.
    citations: list[Citation] = []
    rag_context = ""
    if not kb_ids:
        rag_skipped_reason = "no_knowledge_base"
    elif get_settings().RAG_SKIP_CASUAL and is_casual_question(user_content):
        rag_skipped_reason = "casual_question"
    else:
        rag_skipped_reason = None
        try:
            rag_context, citations = await rag_service.retrieve(
                db, user_content, kb_ids, top_k=5
            )
        except Exception as exc:  # noqa: BLE001 — RAG is best-effort
            logger.warning("durable RAG retrieval failed, continuing without context: %s", exc)
            rag_context, citations = "", []
            rag_skipped_reason = "retrieval_error"
    logger.info(
        "rag_decision(durable) rag_used=%s rag_skipped_reason=%s kb_count=%d citation_count=%d",
        bool(rag_context), rag_skipped_reason, len(kb_ids), len(citations),
    )

    # Rebuild the system prompt + trimmed history using the shared helpers.
    # Task 7: route the durable path through the SAME ContextManager assembly
    # as the inline path so active user memories are folded in consistently
    # (M-2). Summary / goal remain deferred on the durable path (no flow-state
    # re-hydration), matching the prior behavior.
    history = await _load_history(db, conversation.id)
    _active_user_memories = await _load_active_user_memories(db, user.id)
    system_prompt = _build_system_prompt(conversation, rag_context=rag_context)
    system_prompt = _CONTEXT_MANAGER.assemble_system_prompt(
        base=system_prompt,
        rag_context="",  # already folded into base by _build_system_prompt
        summary="",
        goal="",
        memories=_active_user_memories,
        intent_block=None,
        behavior_blocks=[_MULTI_AGENT_HONESTY],
    )
    messages = _messages_to_dicts(system_prompt, history)
    messages = _admit_and_trim_history(
        messages,
        cfg,
        model_name=cfg.model_name,
        tool_schema_tokens=0,
    )

    db_mutation_lock = asyncio.Lock()
    persistence_lock = asyncio.Lock()
    request = ChatRequest(content=user_content, model_id=cfg.id)
    ctx = AgentTurnContext(
        db=db,
        user=user,
        conversation=conversation,
        model_config=cfg,
        request=request,
        user_content=user_content,
        system_prompt=system_prompt,
        messages=messages,
        rag_context=rag_context,
        citations=citations,
        assistant_msg=assistant_msg,
        run_id=run.id,
        execution_mode=execution_mode,
        agent_profile=agent_profile,
        enable_tools=enable_tools,
        knowledge_base_id=kb_ids[0] if kb_ids else None,
        mode=route.mode,
        extra={
            "state": ConversationFlowState(
                conversation_id=str(conversation.id), user_id=str(user.id)
            ),
            "route": route,
            "db_mutation_lock": db_mutation_lock,
            "persistence_session_factory": chat_service._persistence_session_factory,
            "persistence_lock": persistence_lock,
            # Tell the orchestrator to reuse this run instead of creating one.
            "durable_run_id": run.id,
        },
    )

    turn_started = time.monotonic()
    try:
        # Surface retrieval citations BEFORE the first token (same as the
        # inline path) so the client can render source chips with the answer.
        if citations:
            yield AgentEvent(
                kind="citations",
                data={"citations": [c.model_dump(mode="json") for c in citations]},
            )
        async for evt in chat_orchestrator.stream(ctx):
            # M5: the durable worker already appended ``run.started`` (dotted,
            # Task-4 scheme) when it acquired the lease. Suppress the
            # orchestrator's ``run_started`` (underscore) AgentEvent on the
            # durable path so ``EventStore.replay`` shows a single start event.
            if evt.kind == "run_started":
                continue
            if evt.kind == "done":
                finish = evt.data.get("finish_reason", "stop")
                assistant_msg.metadata_ = ChatService._meta(
                    cfg, citations, finish, assistant_msg.metadata_
                )
                # Drop live tool_calls trace (same rationale as the inline path).
                assistant_msg.metadata_.pop("tool_calls", None)
                sel = ctx.extra.get("runtime_selection")
                if sel is not None:
                    assistant_msg.metadata_["requested_mode"] = sel.requested_mode
                    assistant_msg.metadata_["effective_mode"] = sel.effective_mode
                    assistant_msg.metadata_["requested_runtime"] = sel.requested_runtime
                    assistant_msg.metadata_["effective_runtime"] = sel.selected_runtime
                    assistant_msg.metadata_["multi_agent_executed"] = (
                        sel.multi_agent_executed
                    )
                assistant_msg.metadata_["is_demo"] = bool(
                    ctx.extra.get("is_demo")
                ) or bool(getattr(sel, "is_demo", False))
                _apply_usage_accounting(
                    assistant_msg, cfg.model_name, evt.data.get("usage")
                )
                # Quota charge (Task 11): forward SERVER-computed usage to the
                # tenant's counters. No-op unless QUOTAS_ENABLED.
                await _charge_quota_if_enabled(str(user.id), assistant_msg)
                assistant_msg.latency_ms = int((time.monotonic() - turn_started) * 1000)
                conversation.last_message_preview = (assistant_msg.content or "")[:280]
                await commit_with_rollback(db)
                yield evt
                return
            if evt.kind == "error":
                err_code = evt.data.get("code")
                err_finish = evt.data.get("finish_reason") or _finish_for_error_code(
                    err_code
                )
                await ChatService()._finalize_error(
                    db,
                    assistant_msg,
                    evt.data.get("message", "error"),
                    finish_reason=err_finish,
                    code=err_code,
                    usage=evt.data.get("usage"),
                    model_name=cfg.model_name,
                )
                yield evt
                return
            yield evt
    except asyncio.CancelledError:
        # Persist partial output so a cancelled durable run is recoverable.
        assistant_msg.metadata_ = ChatService._meta(
            cfg, [], "stream_disconnected", assistant_msg.metadata_
        )
        _apply_usage_accounting(
            assistant_msg, cfg.model_name, ctx.extra.get("usage")
        )
        # Quota charge (Task 11): even a partial turn consumed tokens.
        await _charge_quota_if_enabled(str(user.id), assistant_msg)
        await _persist_partial(db, assistant_msg)
        raise


# Module-level singleton — the service is stateless, so one shared instance.
chat_service = ChatService()
