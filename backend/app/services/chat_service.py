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
import uuid
from dataclasses import replace
from typing import Any, AsyncIterator

import tiktoken
from sqlalchemy import delete, desc, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.agents.orchestrator import chat_orchestrator
from app.agents.intent_router import decide_route
from app.agents.planning import (
    extract_goal,
    should_summarize,
    summarize_history,
)
from app.agents.schemas import (
    AgentEvent,
    AgentTurnContext,
    ExecutionMode,
)
from app.agents.state_store import load_state, save_summary, upsert_goal
from app.core.exceptions import AppException
from app.models import Conversation, KnowledgeBase, Message, ModelConfig, ToolCall, User
from app.providers.registry import get_provider_for_config
from app.rag.rag_service import rag_service
from app.schemas import ChatRequest, Citation
from app.services.attachment_service import resolve_and_bind_attachments

logger = logging.getLogger(__name__)

# Fallback context budget when a config has no usable token limit configured.
_DEFAULT_MAX_CONTEXT_TOKENS = 8192

# Rough chars-per-token used for naive fallback counting when tiktoken has no
# encoding for a model (e.g. obscure model names). Keeps trimming conservative.
_CHARS_PER_TOKEN = 4

# Default system prompt when a conversation defines none.
_DEFAULT_SYSTEM_PROMPT = "You are a helpful, concise assistant."

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


async def _load_history(db: AsyncSession, conversation_id: uuid.UUID) -> list[Message]:
    """Return conversation messages oldest-first."""
    result = await db.execute(
        select(Message)
        .where(Message.conversation_id == conversation_id)
        .order_by(Message.created_at.asc())
    )
    return list(result.scalars().all())


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

    def total() -> int:
        # Count the whole serialized entry (incl. tool_calls payloads) so we
        # don't underestimate the true prompt size.
        import json

        return sum(
            _estimate_tokens(json.dumps(m, ensure_ascii=False, default=str), model_name)
            for m in messages
        )

    if total() <= max_tokens:
        return messages

    while len(messages) > 2 and total() > max_tokens:
        del messages[1]
    return messages


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


def _augment_with_attachments(user_content: str, attachment_text: str) -> str:
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
    max_chars = 8000
    if len(snippet) > max_chars:
        snippet = snippet[:max_chars] + "\n…（内容已截断，完整内容见附件）"
    return f"{user_content}\n\n[附件内容]\n{snippet}"


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
        await db.commit()
    except Exception:  # pragma: no cover - best effort only
        await db.rollback()


class ChatService:
    """Orchestrates a single chat turn end to end."""

    async def stream(
        self, db: AsyncSession, user: User, request: ChatRequest
    ) -> AsyncIterator[dict[str, Any]]:
        """Yield SSE event dicts for one chat turn.

        Always yields at least one event. On any unrecoverable failure inside
        the pipeline an ``error`` event is emitted and the generator stops, so a
        router can rely on the stream terminating cleanly.
        """
        try:
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

    async def _run(
        self, db: AsyncSession, user: User, request: ChatRequest
    ) -> AsyncIterator[dict[str, Any]]:
        # 1. Resolve conversation + model.
        conversation = await _get_or_create_conversation(db, user, request)
        cfg = await _resolve_model_config(db, request, conversation)

        # 1b. Resolve the execution route from the user-facing mode. The UI sends
        # ``mode`` (auto | search | deep_research | create | data_analysis); the
        # intent router turns it into execution_mode / profile / tools. Legacy
        # explicit execution_mode='agent' (with default mode) still forces the
        # multi-agent runtime so existing clients/tests keep working.
        # Effective KB set: explicit per-turn multi-select wins, else the legacy
        # single id, else the conversation's stored KB.
        kb_ids: list[uuid.UUID] = list(request.knowledge_base_ids or [])
        if not kb_ids and request.knowledge_base_id is not None:
            kb_ids = [request.knowledge_base_id]
        elif not kb_ids and conversation.knowledge_base_id is not None:
            kb_ids = [conversation.knowledge_base_id]
        # Ownership: a user may only run against their own (or system-wide) model
        # config and their own knowledge bases — never another user's.
        if cfg.user_id is not None and cfg.user_id != user.id:
            raise AppException(404, "model_not_found", "Model config not found")
        for _kb_id in kb_ids:
            kb_row = await db.get(KnowledgeBase, _kb_id)
            if kb_row is None or kb_row.user_id != user.id:
                raise AppException(404, "knowledge_base_not_found", "Knowledge base not found")
        route = decide_route(
            request.mode,
            has_knowledge_base=bool(kb_ids),
            has_attachment=bool(request.attachment_ids),
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
        if request.regenerate:
            user_content = await _delete_last_assistant_message(db, conversation.id)
        else:
            if user_content.strip():
                user_msg = Message(
                    conversation_id=conversation.id,
                    role="user",
                    content=user_content,
                )
                db.add(user_msg)
                await db.flush()
                # Bind chat attachments to this user message (ownership-checked).
                if request.attachment_ids:
                    try:
                        summaries, attachment_text = await resolve_and_bind_attachments(
                            db, user.id, conversation.id, user_msg.id, request.attachment_ids
                        )
                        user_msg.metadata_ = {**(user_msg.metadata_ or {}), "attachments": summaries}
                        if attachment_text:
                            user_content = _augment_with_attachments(user_content, attachment_text)
                    except AppException:
                        raise
                    except Exception as exc:  # noqa: BLE001 — attachments are best-effort
                        logger.warning("attachment binding failed: %s", exc)
                # Cheap sidebar preview (last user message text).
                conversation.last_message_preview = (user_content or "").strip()[:280]

        # 3. System prompt + optional RAG retrieval.
        citations: list[Citation] = []
        rag_context = ""
        if kb_ids:
            try:
                rag_context, citations = await rag_service.retrieve(
                    db, user_content, kb_ids, top_k=5
                )
            except Exception as exc:
                # RAG is best-effort: a retrieval failure must not kill the chat.
                logger.warning("RAG retrieval failed, continuing without context: %s", exc)
                rag_context, citations = "", []

        if citations:
            yield _event(
                "citations",
                {"citations": [c.model_dump(mode="json") for c in citations]},
            )

        # 3b. Load cross-turn state (goal + rolling summary + facts).
        flow_state = await load_state(db, conversation.id, user.id)

        system_prompt = _build_system_prompt(conversation, rag_context)
        prefix = ""
        if enable_tools:
            # Agent / tools mode: structured execution, no raw chain-of-thought.
            prefix += _AGENT_TASK_PREAMBLE + "\n\n"
        if flow_state.conversation_summary:
            prefix += (
                "Earlier in this conversation (summary):\n"
                f"{flow_state.conversation_summary}\n\n"
            )
        if flow_state.user_goal:
            prefix += f"User's ongoing goal: {flow_state.user_goal}\n\n"
        system_prompt = prefix + system_prompt

        # Remember the user's goal for this conversation (single 'task' memory).
        if user_content.strip():
            await upsert_goal(
                db, conversation.id, user.id, extract_goal(user_content),
                source_message_id=None,
            )

        # 4. Load + trim history.
        history = await _load_history(db, conversation.id)
        messages = _messages_to_dicts(system_prompt, history)
        max_ctx = _safe_int(cfg.max_context_tokens, _DEFAULT_MAX_CONTEXT_TOKENS)
        messages = _trim_history(messages, max_ctx, cfg.model_name)

        # 5. Pending assistant message + meta event.
        assistant_msg = Message(
            conversation_id=conversation.id,
            role="assistant",
            content="",
            model_name=cfg.model_name,
            metadata_={"status": "pending"},
        )
        db.add(assistant_msg)
        await db.flush()
        await db.commit()  # durable IDs + history before streaming starts

        yield _event(
            "meta",
            {
                "message_id": str(assistant_msg.id),
                "conversation_id": str(conversation.id),
            },
        )

        # 6. Build turn context and delegate to the orchestrator/runtime.
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
            extra={"state": flow_state, "route": route},
        )

        try:
            async for evt in chat_orchestrator.stream(ctx):
                if evt.kind == "done":
                    finish = evt.data.get("finish_reason", "stop")
                    assistant_msg.metadata_ = self._meta(
                        cfg, citations, finish, assistant_msg.metadata_
                    )
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
                    if ctx.extra.get("multi_agent"):
                        # Mark multi-agent runs so the UI shows the compact
                        # "查看执行过程" entry (single-agent runs keep ResearchSteps).
                        assistant_msg.metadata_["multi_agent"] = True
                    # Refresh the sidebar preview with the final assistant text.
                    conversation.last_message_preview = (assistant_msg.content or "")[:280]
                    await db.commit()
                    # Rolling summary: if history grew past the budget, roll the
                    # older messages into a summary memory for future turns.
                    try:
                        await self._maybe_summarize(db, conversation, cfg, user.id)
                    except Exception:  # pragma: no cover - never block done on summary
                        logger.warning("post-turn summary failed", exc_info=True)
                    yield evt.to_sse_envelope()
                    return
                if evt.kind == "error":
                    await self._finalize_error(
                        db, assistant_msg, evt.data.get("message", "error")
                    )
                    yield evt.to_sse_envelope()
                    return
                yield evt.to_sse_envelope()
        except asyncio.CancelledError:
            # Client disconnected: persist whatever text we have so far.
            logger.info("chat stream cancelled by client; saving partial output")
            assistant_msg.metadata_ = self._meta(
                cfg, citations, "cancelled", assistant_msg.metadata_
            )
            await _persist_partial(db, assistant_msg)
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
            "status": "complete" if finish_reason != "cancelled" else "cancelled",
        }
        if citations:
            metadata["citations"] = [c.model_dump(mode="json") for c in citations]
        return metadata

    async def _finalize_error(
        self, db: AsyncSession, assistant_msg: Message, message: str
    ) -> None:
        assistant_msg.metadata_ = {
            **(assistant_msg.metadata_ or {}),
            "status": "error",
            "error": message,
        }
        await db.commit()

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


# Module-level singleton — the service is stateless, so one shared instance.
chat_service = ChatService()
