"""Hermes Agent provider — OpenAI-compatible transport, server-side agent.

Hermes (http://…:8642) exposes a standard ``/v1/chat/completions`` surface, so
the HTTP/SSE plumbing is inherited from :class:`OpenAICompatibleProvider`. Two
things make Hermes different and are handled here:

1. **Tools execute on the Hermes server.** A turn may internally run web
   search, terminal, browser automation, etc. (27 toolsets). The stream never
   carries OpenAI ``tool_calls`` for us to execute — instead it interleaves
   custom SSE events::

        event: hermes.tool.progress
        data: {"tool": "web_search", "emoji": "🔎", "label": "AI news today",
               "toolCallId": "call_...", "status": "running"|"completed"}

   Those are surfaced to the caller via ``ChatDelta.meta["hermes_tool"]``;
   the chat layer re-emits them as regular ``tool_call``/``tool_result``
   agent events so the existing frontend progress UI renders them.

2. **Session identity headers.** ``X-Hermes-Session-Id`` scopes one
   conversation (per-conversation memory isolation) and ``X-Hermes-Session-Key``
   scopes long-term memory across a user's conversations. The provider takes
   them as constructor args; the chat service injects the platform's
   conversation/user ids.

Because tools are server-side, this provider strips any locally-built tool
schemas from the request — sending them would be ignored at best and a 400 at
worst, and the local tool loop must not engage (finish_reason never becomes
``tool_calls``).
"""
from __future__ import annotations

import json
import logging
from typing import Any, AsyncIterator

import httpx

from app.providers.base import ChatDelta, ChatOptions
from app.providers.openai_compatible import OpenAICompatibleProvider

logger = logging.getLogger(__name__)


class HermesProvider(OpenAICompatibleProvider):
    """Hermes agent behind the OpenAI-compatible transport."""

    provider_name = "hermes"

    def __init__(
        self,
        *,
        base_url: str,
        api_key: str = "",
        model: str = "hermes-agent",
        session_id: str = "",
        session_key: str = "",
        **extra: Any,
    ) -> None:
        super().__init__(
            base_url=base_url,
            api_key=api_key,
            model=model,
            **extra,
        )
        # Per-conversation memory scope + cross-conversation long-term memory.
        self.hermes_session_id = session_id
        self.hermes_session_key = session_key

    # -- request shaping -----------------------------------------------------
    def _headers(self) -> dict[str, str]:
        h = super()._headers()
        if self.hermes_session_id:
            h["X-Hermes-Session-Id"] = self.hermes_session_id
        if self.hermes_session_key:
            h["X-Hermes-Session-Key"] = self.hermes_session_key
        return h

    @staticmethod
    def _build_chat_payload(
        model: str,
        messages: list[dict[str, Any]],
        options: ChatOptions | None,
        stream: bool,
    ) -> dict[str, Any]:
        """Drop local tool schemas — Hermes runs its own tools server-side."""
        payload = OpenAICompatibleProvider._build_chat_payload(
            model, messages, options, stream
        )
        payload.pop("tools", None)
        payload.pop("tool_choice", None)
        return payload

    # -- streaming -------------------------------------------------------------
    async def _iter_sse(self, resp: httpx.Response) -> AsyncIterator[ChatDelta]:
        """Parse OpenAI chunks + ``hermes.tool.progress`` interleaved events.

        Tool progress is delivered out-of-band on ``ChatDelta.meta`` (a plain
        dict, ignored by everything that doesn't look for it) so content
        deltas, finish reasons and usage flow through the inherited contract
        untouched.
        """
        tool_accum: dict[int, dict[str, Any]] = {}
        seen_real_finish = None
        final_usage: dict[str, int] | None = None

        async for raw_line in resp.aiter_lines():
            if not raw_line:
                continue
            line = raw_line.strip()
            if not line.startswith("data:"):
                continue
            data_str = line[len("data:"):].strip()
            if data_str == "[DONE]":
                if tool_accum:
                    yield self._flush_tool_accum(tool_accum)
                    tool_accum.clear()
                    seen_real_finish = "tool_calls"
                if seen_real_finish is None:
                    yield ChatDelta(content="", tool_calls=None, finish_reason="stop")
                if final_usage is not None:
                    yield ChatDelta(content="", tool_calls=None, finish_reason=None, usage=final_usage)
                return
            try:
                obj = json.loads(data_str)
            except json.JSONDecodeError:
                continue

            # Hermes custom event: server-side tool progress. Never carries
            # choices; re-emitted as a meta delta for the chat layer.
            if obj.get("tool") and obj.get("toolCallId") and not obj.get("choices"):
                yield ChatDelta(
                    content="",
                    tool_calls=None,
                    finish_reason=None,
                    meta={"hermes_tool": obj},
                )
                continue

            if obj.get("usage"):
                final_usage = obj["usage"]
            choices = obj.get("choices") or []
            if not choices:
                continue
            choice = choices[0]
            delta = choice.get("delta") or {}
            content = delta.get("content") or ""
            finish_reason = choice.get("finish_reason")
            if content:
                yield ChatDelta(content=content, tool_calls=None, finish_reason=finish_reason)
                if finish_reason:
                    seen_real_finish = finish_reason
            elif finish_reason:
                yield ChatDelta(content="", tool_calls=None, finish_reason=finish_reason)
                seen_real_finish = finish_reason

        if final_usage is not None:
            yield ChatDelta(content="", tool_calls=None, finish_reason=None, usage=final_usage)

    @staticmethod
    def _flush_tool_accum(tool_accum: dict[int, dict[str, Any]]) -> ChatDelta:
        return OpenAICompatibleProvider._flush_tool_accum(tool_accum)
