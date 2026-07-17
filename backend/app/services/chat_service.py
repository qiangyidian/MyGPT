"""Chat orchestration service.

This is the linchpin that ties together conversations, model providers, RAG
retrieval, and tool execution into a single streaming pipeline.

The public entry point is ``ChatService.stream(db, user, request)`` — an async
generator yielding SSE event dicts shaped ``{"event": <name>, "data": {...}}``.
A thin router layer translates these into ``text/event-stream`` frames; this
module stays free of FastAPI types so it can be unit-tested in isolation.

Pipeline (per the cross-module contract):
  1. Resolve or create the conversation; resolve the ModelConfig to use.
  2. Persist the user message (skipped on regenerate).
  3. Build the message list: system prompt (+ optional RAG context + citations).
  4. Trim history to fit ``cfg.max_context_tokens`` (tiktoken-based, oldest first).
  5. Create a pending assistant Message row; emit a ``meta`` event.
  6. Stream from the provider; on a tool-calling finish, run the tools, append
     results, and re-stream (up to 4 rounds). Yield ``token`` events live; save
     partial output if the client disconnects.
  7. Persist the final assistant content + metadata; emit ``done`` (or ``error``).
"""
from __future__ import annotations

import asyncio
import json
import logging
import uuid
from typing import Any, AsyncIterator

import tiktoken
from sqlalchemy import delete, desc, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core.exceptions import AppException
from app.models import Conversation, Message, ModelConfig, ToolCall, User
from app.providers.base import ChatOptions, ProviderError, ToolCallDef
from app.providers.registry import get_provider_for_config
from app.rag.rag_service import rag_service
from app.schemas import ChatRequest, Citation
from app.tools.executor import execute_tool_calls
from app.tools.registry_init import get_default_registry

logger = logging.getLogger(__name__)

# How many tool-calling rounds we allow before giving up and returning whatever
# text accumulated. Prevents runaway loops where a model keeps requesting tools.
_MAX_TOOL_ROUNDS = 8

# Fallback context budget when a config has no usable token limit configured.
_DEFAULT_MAX_CONTEXT_TOKENS = 8192

# Rough chars-per-token used for naive fallback counting when tiktoken has no
# encoding for a model (e.g. obscure model names). Keeps trimming conservative.
_CHARS_PER_TOKEN = 4

# Default system prompt when a conversation defines none.
_DEFAULT_SYSTEM_PROMPT = "You are a helpful, concise assistant."

# Prepended when the user enables "deep search / agent" mode. Drives the
# multi-round chain-of-thought + repeated web search behaviour (like OpenAI's
# browsing): decompose, search iteratively, read, search again, then answer.
_AGENT_PREAMBLE = (
    "你具有工具调用能力（包含 web_search 和 http_get），并且可以多次调用。\n"
    "请按“思维链”方式逐步推进：\n"
    "1) 先把用户问题拆成若干子问题；\n"
    "2) 对每个子问题调用 web_search 获取最新信息，阅读返回结果；\n"
    "3) 如信息不足，继续发起更具体的搜索（可多次）；\n"
    "4) 证据充分后，再写出简洁、有条理的最终回答，并用 [source N] 标注来源。\n"
    "偏好“多次小而精准的搜索”，而不是一次大搜索。最终回答之外的思考过程会以步骤形式展示给用户。"
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
        return sum(
            _estimate_tokens(str(m.get("content") or ""), model_name)
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
        except ProviderError as exc:
            logger.exception("provider error during chat: %s", exc)
            yield _event("error", {"code": "provider_error", "message": str(exc)})
        except Exception as exc:  # pragma: no cover - defensive last resort
            logger.exception("unexpected error during chat: %s", exc)
            yield _event("error", {"code": "internal", "message": "Internal error"})

    async def _run(
        self, db: AsyncSession, user: User, request: ChatRequest
    ) -> AsyncIterator[dict[str, Any]]:
        # 1. Resolve conversation + model.
        conversation = await _get_or_create_conversation(db, user, request)
        cfg = await _resolve_model_config(db, request, conversation)

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

        # 3. System prompt + optional RAG retrieval.
        citations: list[Citation] = []
        rag_context = ""
        kb_id = request.knowledge_base_id or conversation.knowledge_base_id
        if kb_id is not None:
            try:
                rag_context, citations = await rag_service.retrieve(
                    db, user_content, kb_id, top_k=5
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

        system_prompt = _build_system_prompt(conversation, rag_context)
        if request.enable_tools:
            # Agent / deep-search mode: instruct iterative search + reasoning.
            system_prompt = _AGENT_PREAMBLE + "\n\n" + system_prompt

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

        # 6. Provider stream + tool loop.
        provider = get_provider_for_config(cfg)
        registry = get_default_registry() if request.enable_tools else None
        # Tools run when the model declares capability, OR it's the mock provider
        # (which simulates a search step so the agent UX is demoable offline).
        tools_enabled = bool(
            registry is not None
            and (getattr(cfg, "supports_tools", False) or (cfg.provider or "") == "mock")
        )
        tool_schemas = registry.openai_schemas() if tools_enabled else None

        options = ChatOptions(
            temperature=cfg.temperature,
            top_p=cfg.top_p,
            max_tokens=_safe_int(cfg.max_tokens, 1024),
            tools=tool_schemas,
            tool_choice="auto",
        )

        finish_reason = "stop"
        holder: dict[str, Any] = {}
        try:
            async for evt in self._stream_with_tools(
                db,
                provider,
                messages,
                options,
                cfg,
                assistant_msg,
                conversation.id,
                tools_enabled,
                holder,
            ):
                yield evt
        except asyncio.CancelledError:
            # Client disconnected: persist whatever text we have so far.
            logger.info("chat stream cancelled by client; saving partial output")
            assistant_msg.metadata_ = self._meta(
                cfg, citations, "length", assistant_msg.metadata_
            )
            await _persist_partial(db, assistant_msg)
            raise
        except ProviderError as exc:
            await self._finalize_error(db, assistant_msg, str(exc))
            raise
        except Exception:
            await self._finalize_error(
                db, assistant_msg, "Internal error during generation"
            )
            raise

        # 7. Finalize + done.
        finish_reason = holder.get("finish_reason", finish_reason)
        assistant_msg.metadata_ = self._meta(cfg, citations, finish_reason, None)
        await db.commit()
        yield _event(
            "done",
            {"message_id": str(assistant_msg.id), "finish_reason": finish_reason},
        )

    async def _stream_with_tools(
        self,
        db: AsyncSession,
        provider: Any,
        messages: list[dict[str, Any]],
        options: ChatOptions,
        cfg: ModelConfig,
        assistant_msg: Message,
        conversation_id: uuid.UUID,
        tools_enabled: bool,
        holder: dict[str, Any],
    ) -> AsyncIterator[dict[str, Any]]:
        """Run the stream + tool loop, yielding token events inline.

        The final ``finish_reason`` is written into ``holder["finish_reason"]``
        (a generator cannot ``return`` a value). ``messages`` is treated
        read-only here; per-round tool turns are tracked in a local ``working``
        list so we can replay the full transcript (history + this turn's tool
        turns) on each round without mutating the caller's view.
        """
        working = list(messages)
        rounds = 0
        finish_reason = "stop"

        while rounds <= _MAX_TOOL_ROUNDS:
            rounds += 1
            accumulated: list[str] = []
            pending_tool_calls: list[ToolCallDef] = []

            async for delta in provider.stream_chat(working, options):
                if delta.content:
                    accumulated.append(delta.content)
                    # Yield the token to the caller immediately as it arrives.
                    yield _event("token", {"delta": delta.content})
                if delta.tool_calls:
                    pending_tool_calls.extend(delta.tool_calls)
                if delta.finish_reason:
                    finish_reason = delta.finish_reason

            # Fold this round's streamed text into the assistant message so a
            # mid-loop disconnect still leaves recoverable content.
            assistant_msg.content = (assistant_msg.content or "") + "".join(accumulated)

            # Tool-calling branch: execute, append results, and re-stream.
            if (
                tools_enabled
                and finish_reason == "tool_calls"
                and pending_tool_calls
                and rounds <= _MAX_TOOL_ROUNDS
            ):
                assistant_turn = {
                    "role": "assistant",
                    "content": "".join(accumulated) or "",
                    "tool_calls": [
                        {
                            "id": tc.id,
                            "type": "function",
                            "function": {
                                "name": tc.name,
                                "arguments": tc.arguments,
                            },
                        }
                        for tc in pending_tool_calls
                    ],
                }
                # Emit tool_call events so the UI can show each search/thinking step.
                for tc in pending_tool_calls:
                    try:
                        args = json.loads(tc.arguments) if tc.arguments else {}
                    except Exception:
                        args = {}
                    yield _event(
                        "tool_call", {"id": tc.id, "name": tc.name, "arguments": args}
                    )

                working.append(assistant_turn)
                # Persist the assistant turn + mark tool-calling in progress.
                assistant_msg.metadata_ = {
                    **(assistant_msg.metadata_ or {}),
                    "tool_calls": assistant_turn["tool_calls"],
                    "status": "tool_calling",
                }
                await db.commit()
                try:
                    tool_messages = await execute_tool_calls(
                        db,
                        conversation_id,
                        assistant_msg.id,
                        pending_tool_calls,
                    )
                except Exception as exc:
                    logger.exception("tool execution failed: %s", exc)
                    raise
                # Emit tool_result events so the UI can show each step's outcome.
                for tc, tm in zip(pending_tool_calls, tool_messages):
                    yield _event(
                        "tool_result",
                        {
                            "id": tc.id,
                            "name": tc.name,
                            "ok": True,
                            "result": tm.get("content"),
                            "error": None,
                        },
                    )
                working.extend(tool_messages)
                continue

            # No further tool calls (or tool budget exhausted) — done.
            break

        holder["finish_reason"] = finish_reason
        return

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
            "status": "complete",
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


# Module-level singleton — the service is stateless, so one shared instance.
chat_service = ChatService()
