"""Anthropic native provider (Messages API /v1/messages).

Talks to Anthropic's first-party Messages API wire format (also served by
Anthropic-compatible gateways). The rest of this codebase speaks the OpenAI
message/tool vocabulary (role=tool messages, tool_calls on assistant turns,
OpenAI function schemas), so this provider's job is bidirectional translation:

    outbound (request)
        OpenAI messages          → Anthropic messages (+ top-level system)
        system-role messages     → folded into the top-level system prompt
        OpenAI function schemas  → Anthropic tools (name/description/input_schema)
        tool_choice auto/none    → Anthropic tool_choice auto/none
        ChatOptions              → temperature/top_p/stop_sequences/max_tokens

    inbound (response / stream)
        content blocks           → content string + tool_calls list
        stop_reason              → OpenAI finish_reason
        usage                    → prompt_tokens/completion_tokens/total_tokens

Streaming: Anthropic SSE (message_start / content_block_start /
content_block_delta / content_block_stop / message_delta / message_stop) is
translated on the fly into the ChatDelta shape the native runtime consumes —
text deltas carry content, input_json_delta accumulates per-index tool calls
and is flushed as complete ToolCallDefs (same contract as the OpenAI provider's
accumulator), and message_delta carries the real finish_reason + usage.

Embeddings: Anthropic has no embeddings endpoint — embeddings() raises
ProviderError telling the operator to point an openai-compatible embedding
config at an embedding service instead.

Auth: x-api-key + anthropic-version headers (Authorization: Bearer also sent
when an api_key is set, for OAuth-token gateways).
"""
from __future__ import annotations

import json
import logging
from collections.abc import AsyncIterator
from typing import Any

import httpx

from app.core.config import get_settings
from app.observability import observe_counter, observe_histogram, observe_span
from app.providers.base import (
    PROVIDER_ERR_NETWORK,
    PROVIDER_ERR_TIMEOUT,
    ChatDelta,
    ChatOptions,
    ChatResult,
    ModelProvider,
    ProviderError,
    ToolCallDef,
    admit_provider_payload,
)

logger = logging.getLogger(__name__)

_ANTHROPIC_VERSION = "2023-06-01"
_DEFAULT_MAX_TOKENS = 4096  # Anthropic requires max_tokens on every request

# Anthropic stop_reason → our canonical FinishReason vocabulary.
_STOP_REASON_MAP = {
    "end_turn": "stop",
    "stop_sequence": "stop",
    "max_tokens": "length",
    "tool_use": "tool_calls",
    "refusal": "content_filter",
    "pause_turn": "stop",
}

# Retried transient failures (mirrors openai_compatible policy).
_RETRYABLE_EXC = (httpx.TransportError, httpx.TimeoutException)
_RETRYABLE_STATUS = {408, 429, 500, 502, 503, 504, 529}  # 529 = anthropic overloaded


def _split_system(messages: list[dict[str, Any]]) -> tuple[str, list[dict[str, Any]]]:
    """Extract system prompts; return (system_text, remaining messages).

    OpenAI-style system messages (and any leading ones) are folded into the
    single top-level system string Anthropic expects. Non-leading system
    messages (mid-conversation operator notes) are folded in order too — they
    can't be represented mid-messages on the Anthropic side.
    """
    system_parts: list[str] = []
    rest: list[dict[str, Any]] = []
    for msg in messages:
        content = msg.get("content")
        text = content if isinstance(content, str) else ""
        if msg.get("role") == "system":
            if text.strip():
                system_parts.append(text)
            continue
        rest.append(msg)
    return "\n\n".join(system_parts), rest


def _content_to_text(content: Any) -> str:
    """Flatten an OpenAI content value (str | list of parts) to plain text."""
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for part in content:
            if isinstance(part, dict) and part.get("type") == "text":
                parts.append(part.get("text") or "")
        return "".join(parts)
    return str(content)


def _convert_message(msg: dict[str, Any]) -> dict[str, Any] | None:
    """OpenAI message → Anthropic message (content blocks vocabulary).

    - user/assistant text          → text block
    - user image_url parts         → image block (url or base64 source)
    - assistant tool_calls         → tool_use blocks (input parsed from JSON)
    - role=tool (tool result)      → user message with a tool_result block
    Returns None for messages that carry nothing representable.
    """
    role = msg.get("role")
    content = msg.get("content")

    if role == "tool":
        return {
            "role": "user",
            "content": [{
                "type": "tool_result",
                "tool_use_id": msg.get("tool_call_id") or "",
                "content": _content_to_text(content),
            }],
        }

    blocks: list[dict[str, Any]] = []
    text = _content_to_text(content)
    if text:
        blocks.append({"type": "text", "text": text})

    # Multimodal parts (user turns carrying image_url).
    if isinstance(content, list):
        for part in content:
            if not isinstance(part, dict):
                continue
            if part.get("type") == "image_url":
                url = ""
                src = part.get("image_url")
                if isinstance(src, dict):
                    url = src.get("url") or ""
                elif isinstance(src, str):
                    url = src
                if not url:
                    continue
                if url.startswith("data:"):
                    # data:<media>;base64,<payload>
                    try:
                        head, payload = url.split(",", 1)
                        media_type = head[5:].split(";")[0] or "image/png"
                        blocks.append({
                            "type": "image",
                            "source": {"type": "base64", "media_type": media_type, "data": payload},
                        })
                    except ValueError:
                        continue
                else:
                    blocks.append({
                        "type": "image",
                        "source": {"type": "url", "url": url},
                    })

    # Assistant tool calls → tool_use blocks. The runtime replays its own
    # OpenAI-shaped assistant turns back in, so translate faithfully.
    if role == "assistant" and msg.get("tool_calls"):
        for tc in msg["tool_calls"]:
            func = tc.get("function") or {}
            try:
                args = json.loads(func.get("arguments") or "{}")
            except json.JSONDecodeError:
                args = {}
            blocks.append({
                "type": "tool_use",
                "id": tc.get("id") or "",
                "name": func.get("name") or "",
                "input": args if isinstance(args, dict) else {"_value": args},
            })

    if not blocks:
        return None
    out_role = "assistant" if role == "assistant" else "user"
    # Single bare text block → plain string content (cheapest wire shape).
    if len(blocks) == 1 and blocks[0]["type"] == "text":
        return {"role": out_role, "content": blocks[0]["text"]}
    return {"role": out_role, "content": blocks}


def _convert_tools(tools: list[dict[str, Any]] | None) -> list[dict[str, Any]]:
    """OpenAI function schemas → Anthropic tools (name/description/input_schema)."""
    if not tools:
        return []
    out: list[dict[str, Any]] = []
    for t in tools:
        if not isinstance(t, dict):
            continue
        func = t.get("function") if t.get("type") == "function" else t
        if not isinstance(func, dict) or not func.get("name"):
            continue
        out.append({
            "name": func["name"],
            "description": func.get("description") or "",
            "input_schema": func.get("parameters") or {"type": "object", "properties": {}},
        })
    return out


def _convert_tool_choice(choice: Any) -> Any:
    """OpenAI tool_choice → Anthropic tool_choice (None when unrepresentable)."""
    if choice is None:
        return None
    if choice == "auto":
        return {"type": "auto"}
    if choice == "none":
        return {"type": "none"}  # best-effort; callers rarely send this
    if choice == "required":
        return {"type": "any"}
    if isinstance(choice, dict) and choice.get("type") == "function":
        name = (choice.get("function") or {}).get("name")
        if name:
            return {"type": "tool", "name": name}
    return None


def _blocks_to_result(content: list[dict[str, Any]]) -> tuple[str, list[ToolCallDef]]:
    """Anthropic content blocks → (text, tool_calls)."""
    texts: list[str] = []
    calls: list[ToolCallDef] = []
    for block in content:
        btype = block.get("type")
        if btype == "text":
            texts.append(block.get("text") or "")
        elif btype == "tool_use":
            calls.append(ToolCallDef(
                id=block.get("id") or "",
                name=block.get("name") or "",
                arguments=json.dumps(block.get("input") or {}, ensure_ascii=False),
            ))
    return "".join(texts), calls


def _map_usage(usage: dict[str, Any] | None) -> dict[str, int]:
    """Anthropic usage → OpenAI usage vocabulary our accounting expects.

    Absent fields are OMITTED (not zero-filled): stream usage arrives in two
    snapshots (message_start carries input_tokens, message_delta carries
    output_tokens) that merge afterwards — zero-fills would clobber the real
    values from the earlier snapshot. total_tokens is likewise recomputed at
    merge time (see _merge_stream_usage), never trusted from a partial snapshot.
    """
    if not isinstance(usage, dict):
        return {}
    out: dict[str, int] = {}
    if usage.get("input_tokens") is not None:
        out["prompt_tokens"] = int(usage["input_tokens"])
    if usage.get("output_tokens") is not None:
        out["completion_tokens"] = int(usage["output_tokens"])
    # Cache fields stay under their Anthropic names for cost accounting.
    for key in ("cache_read_input_tokens", "cache_creation_input_tokens"):
        if usage.get(key) is not None:
            out[key] = int(usage[key])
    # Non-streaming responses carry both fields; totals computed once here.
    if "prompt_tokens" in out and "completion_tokens" in out:
        out["total_tokens"] = out["prompt_tokens"] + out["completion_tokens"]
    return out


def _merge_stream_usage(
    current: dict[str, int] | None, snapshot: dict[str, Any] | None
) -> dict[str, int]:
    """Merge a stream usage snapshot; recompute totals from merged parts."""
    merged = {**(current or {}), **_map_usage(snapshot)}
    merged["total_tokens"] = (
        merged.get("prompt_tokens", 0) + merged.get("completion_tokens", 0)
    )
    return merged


class AnthropicProvider(ModelProvider):
    """Anthropic Messages API behind the same ModelProvider contract."""

    provider_name = "anthropic"

    def __init__(
        self,
        *,
        base_url: str = "",
        api_key: str = "",
        model: str = "",
        **extra: Any,
    ) -> None:
        # Default to the first-party endpoint when the config left it blank —
        # importing a Claude model shouldn't require typing a URL at all.
        resolved_base = base_url or "https://api.anthropic.com"
        super().__init__(
            base_url=resolved_base,
            api_key=api_key,
            model=model,
            **extra,
        )
        s = get_settings()
        self._timeout = httpx.Timeout(
            read=s.MODEL_READ_TIMEOUT_SECONDS,
            connect=s.MODEL_CONNECT_TIMEOUT_SECONDS,
            write=s.MODEL_WRITE_TIMEOUT_SECONDS,
            pool=s.MODEL_POOL_TIMEOUT_SECONDS,
        )

    # -- HTTP helpers --------------------------------------------------------
    def _headers(self) -> dict[str, str]:
        h: dict[str, str] = {
            "Content-Type": "application/json",
            "Accept": "application/json",
            "anthropic-version": _ANTHROPIC_VERSION,
        }
        if self.api_key:
            h["x-api-key"] = self.api_key
            # OAuth-style bearer tokens (ant auth print-credentials) authenticate
            # via Authorization instead of x-api-key; sending both is harmless
            # for API-key auth and required for bearer-token gateways.
            h["Authorization"] = f"Bearer {self.api_key}"
        return h

    def _messages_url(self) -> str:
        # base_url may or may not include the /v1 suffix; normalize.
        base = self.base_url.rstrip("/")
        if base.endswith("/v1"):
            return f"{base}/messages"
        return f"{base}/v1/messages"

    @staticmethod
    def _raise_for_status(resp: httpx.Response) -> None:
        if resp.status_code >= 400:
            logger.warning("anthropic endpoint returned HTTP %s", resp.status_code)
            if resp.status_code in (401, 403):
                raise ProviderError("anthropic endpoint authentication failed")
            raise ProviderError(f"anthropic endpoint returned HTTP {resp.status_code}")

    def _build_payload(
        self,
        messages: list[dict[str, Any]],
        options: ChatOptions | None,
        stream: bool,
    ) -> dict[str, Any]:
        opts = options or ChatOptions()
        system, rest = _split_system(messages)
        converted: list[dict[str, Any]] = []
        for msg in rest:
            block_msg = _convert_message(msg)
            if block_msg is not None:
                converted.append(block_msg)
        payload: dict[str, Any] = {
            "model": self.model,
            "max_tokens": opts.max_tokens if opts.max_tokens is not None else _DEFAULT_MAX_TOKENS,
            "messages": converted or [{"role": "user", "content": ""}],
        }
        if system:
            payload["system"] = system
        if stream:
            payload["stream"] = True
        # Sampling: Anthropic accepts temperature XOR top_p — sending both is a
        # 400 on the first-party API, so prefer temperature when both are set.
        if opts.temperature is not None:
            payload["temperature"] = opts.temperature
        elif opts.top_p is not None and opts.top_p != 1.0:
            payload["top_p"] = opts.top_p
        if opts.stop:
            payload["stop_sequences"] = list(opts.stop)
        tools = _convert_tools(opts.tools)
        if tools:
            payload["tools"] = tools
            tool_choice = _convert_tool_choice(opts.tool_choice)
            if tool_choice is not None:
                payload["tool_choice"] = tool_choice
        # reasoning_effort (set by the native runtime when the model config
        # declares supports_reasoning_effort) maps onto Anthropic effort.
        if isinstance(opts.extra, dict):
            effort = opts.extra.get("reasoning_effort")
            if effort in ("low", "medium", "high", "xhigh", "max"):
                payload["output_config"] = {"effort": effort}
        return payload

    # -- ModelProvider impl --------------------------------------------------
    async def chat(
        self, messages: list[dict[str, Any]], options: ChatOptions | None = None
    ) -> ChatResult:
        import time

        started = time.monotonic()
        with observe_span("model.call", model=self.model, provider=self.provider_name):
            admitted = admit_provider_payload(self, messages, options)
            payload = self._build_payload(messages, admitted, stream=False)
            try:
                async with httpx.AsyncClient(timeout=self._timeout) as client:
                    resp = await self._request_with_retry(client, payload)
            except _RETRYABLE_EXC as exc:
                observe_counter("model.calls", 1, model=self.model, outcome="error")
                raise self._to_provider_error(exc, where="chat") from exc

            self._raise_for_status(resp)
            try:
                data = resp.json()
            except ValueError as exc:
                raise ProviderError("anthropic endpoint returned invalid JSON") from exc

            if data.get("type") == "error":
                err = data.get("error") or {}
                raise ProviderError(f"anthropic error: {err.get('message') or err.get('type') or 'unknown'}")

            content = data.get("content") or []
            text, tool_calls = _blocks_to_result(content)
            finish = _STOP_REASON_MAP.get(data.get("stop_reason") or "", "stop")
            usage = _map_usage(data.get("usage"))
        observe_counter("model.calls", 1, model=self.model, outcome=str(finish))
        observe_histogram(
            "model.latency_ms", int((time.monotonic() - started) * 1000),
            model=self.model, operation="chat",
        )
        return ChatResult(
            content=text,
            tool_calls=tool_calls or None,
            finish_reason=finish,
            usage=usage,
        )

    async def stream_chat(
        self, messages: list[dict[str, Any]], options: ChatOptions | None = None
    ) -> AsyncIterator[ChatDelta]:
        import time

        started = time.monotonic()
        with observe_span("model.stream", model=self.model, provider=self.provider_name):
            admitted = admit_provider_payload(self, messages, options)
            payload = self._build_payload(messages, admitted, stream=True)
            try:
                async with httpx.AsyncClient(timeout=self._timeout) as client:
                    async with client.stream(
                        "POST", self._messages_url(), json=payload, headers=self._headers()
                    ) as resp:
                        if resp.status_code in _RETRYABLE_STATUS or resp.status_code >= 400:
                            body = (await resp.aread()).decode("utf-8", errors="replace")
                            if resp.status_code in (401, 403):
                                raise ProviderError("anthropic endpoint authentication failed")
                            raise ProviderError(
                                f"anthropic endpoint returned HTTP {resp.status_code}: {body[:300]}"
                            )
                        async for chunk in self._iter_sse(resp):
                            yield chunk
                        return
            except ProviderError:
                observe_counter("model.calls", 1, model=self.model, outcome="error")
                raise
            except _RETRYABLE_EXC as exc:
                observe_counter("model.calls", 1, model=self.model, outcome="error")
                raise self._to_provider_error(exc, where="stream") from exc
        observe_counter("model.calls", 1, model=self.model, outcome="streamed")
        observe_histogram(
            "model.latency_ms", int((time.monotonic() - started) * 1000),
            model=self.model, operation="stream",
        )

    async def _request_with_retry(
        self, client: httpx.AsyncClient, payload: dict[str, Any]
    ) -> httpx.Response:
        """POST with bounded retry on transient statuses (pre-stream only)."""
        import asyncio

        last_error: Exception | None = None
        for attempt in range(1, 6):
            try:
                resp = await client.post(self._messages_url(), json=payload, headers=self._headers())
                if resp.status_code in _RETRYABLE_STATUS and attempt < 5:
                    await asyncio.sleep(min(2 ** attempt, 10))
                    continue
                return resp
            except _RETRYABLE_EXC as exc:
                last_error = exc
                if attempt < 5:
                    await asyncio.sleep(min(2 ** attempt, 10))
                    continue
                raise
        raise ProviderError(f"anthropic endpoint retry failed: {last_error}")

    async def _iter_sse(self, resp: httpx.Response) -> AsyncIterator[ChatDelta]:
        """Translate Anthropic SSE events into ChatDelta chunks.

        Tool inputs arrive as incremental ``input_json_delta`` fragments on a
        content block; they are accumulated per block index and flushed as one
        well-formed ToolCallDef set when the block (or message) closes — the
        same contract the OpenAI provider's accumulator provides downstream.
        """
        tool_blocks: dict[int, dict[str, str]] = {}   # index → {id, name, args}
        active_block: dict[str, Any] = {}             # index → block header
        finish_reason: str | None = None
        final_usage: dict[str, int] | None = None

        async for raw_line in resp.aiter_lines():
            line = (raw_line or "").strip()
            if not line.startswith("data:"):
                continue
            data_str = line[len("data:"):].strip()
            if not data_str or data_str == "[DONE]":
                continue
            try:
                evt = json.loads(data_str)
            except json.JSONDecodeError:
                continue
            etype = evt.get("type")

            if etype == "message_start":
                msg = evt.get("message") or {}
                if msg.get("usage"):
                    final_usage = _merge_stream_usage(final_usage, msg["usage"])

            elif etype == "content_block_start":
                block = evt.get("content_block") or {}
                idx = int(evt.get("index") or 0)
                active_block[idx] = block
                if block.get("type") == "tool_use":
                    tool_blocks[idx] = {
                        "id": block.get("id") or "",
                        "name": block.get("name") or "",
                        "args": "",
                    }

            elif etype == "content_block_delta":
                delta = evt.get("delta") or {}
                idx = int(evt.get("index") or 0)
                dtype = delta.get("type")
                if dtype == "text_delta" and delta.get("text"):
                    yield ChatDelta(content=delta["text"], tool_calls=None, finish_reason=None)
                elif dtype == "input_json_delta":
                    frag = delta.get("partial_json")
                    if frag and idx in tool_blocks:
                        tool_blocks[idx]["args"] += frag
                # thinking_delta: reasoning summaries — not surfaced as content
                # (persisting them into chat transcripts would corrupt replies).

            elif etype == "content_block_stop":
                idx = int(evt.get("index") or 0)
                block = active_block.pop(idx, None)
                if block is not None and block.get("type") == "tool_use":
                    # Validate accumulated args; drop only if malformed. The
                    # block stays in tool_blocks for the terminal flush below.
                    slot = tool_blocks.get(idx)
                    if slot:
                        try:
                            json.loads(slot["args"] or "{}")
                        except json.JSONDecodeError:
                            logger.warning(
                                "anthropic tool_use args malformed for %s; dropping",
                                slot.get("name"),
                            )
                            tool_blocks.pop(idx, None)

            elif etype == "message_delta":
                delta = evt.get("delta") or {}
                stop = delta.get("stop_reason")
                if stop:
                    finish_reason = _STOP_REASON_MAP.get(stop, "stop")
                if evt.get("usage"):
                    # message_start carries input_tokens; message_delta carries
                    # the final output_tokens. Merge rather than replace so the
                    # terminal usage delta is complete.
                    final_usage = _merge_stream_usage(final_usage, evt["usage"])

            elif etype == "error":
                err = evt.get("error") or {}
                raise ProviderError(
                    f"anthropic stream error: {err.get('message') or err.get('type') or 'unknown'}"
                )

        # Flush accumulated tool calls as the terminal delta (mirrors the
        # OpenAI provider flushing its accumulator on [DONE]).
        if tool_blocks:
            calls = [
                ToolCallDef(id=s["id"], name=s["name"], arguments=s["args"] or "{}")
                for _, s in sorted(tool_blocks.items())
            ]
            yield ChatDelta(content="", tool_calls=calls, finish_reason="tool_calls")
            finish_reason = "tool_calls"
        yield ChatDelta(
            content="", tool_calls=None,
            finish_reason=finish_reason or "stop",
            usage=final_usage,
        )

    async def embeddings(self, texts: list[str], model: str | None = None) -> list[list[float]]:
        raise ProviderError(
            "Anthropic provides no embeddings API. Configure an openai-compatible "
            "embedding model (e.g. a dedicated embedding service) for RAG instead."
        )

    @staticmethod
    def _to_provider_error(exc: Exception, *, where: str) -> ProviderError:
        if isinstance(exc, httpx.TimeoutException):
            return ProviderError(
                f"anthropic endpoint timed out ({where})", code=PROVIDER_ERR_TIMEOUT
            )
        logger.warning("anthropic transport failure during %s: %r", where, exc)
        return ProviderError(
            f"anthropic endpoint transport failure ({where})", code=PROVIDER_ERR_NETWORK
        )
