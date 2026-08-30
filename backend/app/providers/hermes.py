"""Hermes Agent provider — server-side agent behind two transports.

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

**Runs transport (2026-08).** When ``GET /v1/capabilities`` advertises
``run_submission`` + ``run_events_sse`` + ``run_stop``, ``stream_chat`` prefers
the richer Runs API instead of chat/completions:

    POST /v1/runs                     → {"run_id": "run_..."}
    GET  /v1/runs/{run_id}/events     → SSE (tokens, tool.*, subagent.*, run.*)
    POST /v1/runs/{run_id}/stop       → graceful interruption

The Runs stream unlocks subagent delegation events (``subagent.start`` /
``subagent.complete`` → ``meta["hermes_subagent"]``) and true server-side
cancellation via :meth:`stop_run`. Multi-turn context rides the existing
session headers (Hermes keeps per-session memory server-side), so the run
``input`` is just the latest user message. Any probe failure or missing
feature falls back to the inherited chat/completions path — zero behavior
change on older Hermes deployments.

Because tools are server-side, this provider strips any locally-built tool
schemas from the request — sending them would be ignored at best and a 400 at
worst, and the local tool loop must not engage (finish_reason never becomes
``tool_calls``).
"""
from __future__ import annotations

import json
import logging
import time
from typing import Any, AsyncIterator

import httpx

from app.providers.base import ChatDelta, ChatOptions, ProviderError
from app.providers.openai_compatible import (
    OpenAICompatibleProvider,
    _RETRYABLE_EXC,
    _to_provider_error,
)

logger = logging.getLogger(__name__)

# Capabilities probe cache lifetime — avoids one extra round-trip per turn on
# a healthy deployment while still picking up a server upgrade within minutes.
_CAPABILITIES_TTL_SECONDS = 300.0

# Runs API event → finish_reason. Anything else terminal falls back to "stop".
_RUN_TERMINAL_FINISH = {
    "run.completed": "stop",
    "run.failed": "error",
    "run.cancelled": "cancelled",
}


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
        # Runs-transport state (probe cache + the run id of the stream in
        # flight, so stop_run() can target it).
        self._runs_supported: bool | None = None
        self._runs_probed_at: float = 0.0
        self._active_run_id: str | None = None

    # -- request shaping -----------------------------------------------------
    def _headers(self) -> dict[str, str]:
        h = super()._headers()
        if self.hermes_session_id:
            h["X-Hermes-Session-Id"] = self.hermes_session_id
        if self.hermes_session_key:
            h["X-Hermes-Session-Key"] = self.hermes_session_key
        return h

    def _sse_headers(self) -> dict[str, str]:
        """Headers for SSE subscriptions (Accept: text/event-stream)."""
        h = self._headers()
        h["Accept"] = "text/event-stream"
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

    # -- Runs API --------------------------------------------------------------
    async def _probe_runs_support(self) -> bool:
        """True when the server advertises the Runs surface we rely on.

        Cached for :data:`_CAPABILITIES_TTL_SECONDS`; any transport error or
        malformed payload reads as "unsupported" so we fall back rather than
        breaking the turn.
        """
        now = time.monotonic()
        if (
            self._runs_supported is not None
            and now - self._runs_probed_at < _CAPABILITIES_TTL_SECONDS
        ):
            return self._runs_supported
        supported = False
        try:
            async with httpx.AsyncClient(timeout=self._timeout) as client:
                resp = await client.get(
                    f"{self.base_url}/capabilities", headers=self._headers()
                )
            if resp.status_code == 200:
                feats = (resp.json() or {}).get("features") or {}
                supported = bool(
                    feats.get("run_submission")
                    and feats.get("run_events_sse")
                    and feats.get("run_stop")
                )
        except (httpx.HTTPError, ValueError) as exc:
            logger.debug("hermes capabilities probe failed (%s); fallback", exc)
            supported = False
        self._runs_supported = supported
        self._runs_probed_at = now
        return supported

    async def _stream_via_runs(
        self, messages: list[dict[str, Any]], options: ChatOptions | None
    ) -> AsyncIterator[ChatDelta]:
        """One Hermes run: submit → subscribe → translate events to ChatDeltas.

        Raises ProviderError on submit/transport failure so the caller can
        decide to fall back to the chat/completions path (only before any
        delta has been emitted — matching the inherited retry semantics).
        """
        # Runs is a single-turn entry: the LAST user message is the input;
        # earlier context lives in Hermes' session memory via our headers.
        user_msgs = [m for m in messages if m.get("role") == "user"]
        input_text = ""
        if user_msgs:
            content = user_msgs[-1].get("content")
            if isinstance(content, str):
                input_text = content
            elif isinstance(content, list):  # multimodal parts
                input_text = " ".join(
                    p.get("text", "") for p in content if isinstance(p, dict)
                )
        if not input_text:
            # Degenerate transcript (system-only / continuation) — Runs can't
            # express it; signal the caller to use the fallback transport.
            raise ProviderError("hermes runs transport needs a user input")

        payload: dict[str, Any] = {"input": input_text, "model": self.model}
        if self.hermes_session_id:
            payload["session_id"] = self.hermes_session_id
        # Front-load system guidance so a per-conversation persona survives.
        sys_msgs = [m for m in messages if m.get("role") == "system"]
        if sys_msgs:
            sys_text = sys_msgs[-1].get("content")
            if isinstance(sys_text, str) and sys_text.strip():
                payload["instructions"] = sys_text

        try:
            async with httpx.AsyncClient(timeout=self._timeout) as client:
                submit = await client.post(
                    f"{self.base_url}/runs", json=payload, headers=self._headers()
                )
                if submit.status_code >= 400:
                    await submit.aread()
                    raise ProviderError(
                        f"hermes runs submit returned HTTP {submit.status_code}"
                    )
                run_id = (submit.json() or {}).get("run_id") or ""
                if not run_id:
                    raise ProviderError("hermes runs submit returned no run_id")
                self._active_run_id = str(run_id)
                try:
                    async with client.stream(
                        "GET",
                        f"{self.base_url}/runs/{run_id}/events",
                        headers=self._sse_headers(),
                    ) as resp:
                        if resp.status_code >= 400:
                            raise ProviderError(
                                f"hermes runs events returned HTTP {resp.status_code}"
                            )
                        async for delta in self._iter_run_events(resp):
                            yield delta
                finally:
                    self._active_run_id = None
        except _RETRYABLE_EXC as exc:
            raise _to_provider_error(exc, where="hermes runs") from exc

    async def _iter_run_events(self, resp: httpx.Response) -> AsyncIterator[ChatDelta]:
        """Translate the Runs SSE stream into the ChatDelta contract.

        Event names are matched leniently (``run.completed`` vs ``completed``,
        ``assistant.delta`` vs ``token``) so minor Hermes version drift doesn't
        silently drop tokens.
        """
        final_usage: dict[str, int] | None = None
        async for raw_line in resp.aiter_lines():
            line = raw_line.strip()
            if not line.startswith("data:"):
                continue
            data_str = line[len("data:"):].strip()
            if not data_str or data_str == "[DONE]":
                continue
            try:
                obj = json.loads(data_str)
            except json.JSONDecodeError:
                continue
            if not isinstance(obj, dict):
                continue

            kind = str(
                obj.get("event") or obj.get("type") or obj.get("status") or ""
            )
            data = obj.get("data") if isinstance(obj.get("data"), dict) else obj

            # -- terminal lifecycle ------------------------------------------------
            if kind in _RUN_TERMINAL_FINISH:
                if isinstance(data.get("usage"), dict):
                    final_usage = data["usage"]
                yield ChatDelta(
                    content="",
                    finish_reason=_RUN_TERMINAL_FINISH[kind],  # type: ignore[arg-type]
                    usage=final_usage,
                )
                return

            # -- token deltas -------------------------------------------------------
            if kind in ("assistant.delta", "token", "run.delta"):
                text = data.get("delta") or data.get("text") or ""
                if text:
                    yield ChatDelta(content=str(text))

            # -- tool progress (same shape as hermes.tool.progress) -----------------
            elif kind in ("tool.started", "tool.completed", "hermes.tool.progress"):
                status = (
                    "completed"
                    if kind == "tool.completed" or data.get("status") == "completed"
                    else "running"
                )
                yield ChatDelta(
                    content="",
                    meta={
                        "hermes_tool": {
                            "tool": data.get("tool") or data.get("name") or "tool",
                            "toolCallId": data.get("toolCallId")
                            or data.get("tool_call_id")
                            or data.get("call_id")
                            or f"run-tool-{time.monotonic_ns()}",
                            "label": data.get("label") or "",
                            "emoji": data.get("emoji") or "",
                            "status": status,
                        }
                    },
                )

            # -- subagent delegation --------------------------------------------------
            elif kind in ("subagent.start", "subagent.complete"):
                done = kind == "subagent.complete"
                yield ChatDelta(
                    content="",
                    meta={
                        "hermes_subagent": {
                            "subagentId": data.get("child_session_id")
                            or data.get("subagent_id")
                            or f"subagent-{time.monotonic_ns()}",
                            "label": data.get("label")
                            or data.get("name")
                            or data.get("summary")
                            or "子代理任务",
                            "status": (
                                data.get("status")
                                if done
                                else "running"
                            ),
                            "summary": data.get("summary") or "",
                            "duration": data.get("duration"),
                            "tokens": data.get("tokens"),
                        }
                    },
                )

            # -- usage snapshots (non-terminal) ---------------------------------------
            if isinstance(obj.get("usage"), dict):
                final_usage = obj["usage"]

        # Stream ended without a terminal event (server closed early).
        yield ChatDelta(content="", finish_reason="stop", usage=final_usage)

    async def stop_run(self) -> None:
        """Ask Hermes to stop the in-flight run (graceful, server-side).

        Fire-and-forget by design: the local cancel path must never block on
        the upstream round-trip. No-op when no run is active or the Runs
        transport isn't in use.
        """
        run_id = self._active_run_id
        if not run_id:
            return
        self._active_run_id = None
        try:
            async with httpx.AsyncClient(timeout=self._timeout) as client:
                await client.post(
                    f"{self.base_url}/runs/{run_id}/stop", headers=self._headers()
                )
        except httpx.HTTPError as exc:
            logger.debug("hermes stop_run failed for %s: %r", run_id, exc)

    # -- ModelProvider: stream -------------------------------------------------
    async def stream_chat(
        self, messages: list[dict[str, Any]], options: ChatOptions | None = None
    ) -> AsyncIterator[ChatDelta]:
        """Prefer the Runs transport; fall back to chat/completions SSE.

        The fallback triggers on: capabilities probe failure, a submit error,
        or any Runs transport error raised BEFORE the first delta (after that,
        retrying would duplicate tokens, matching the inherited policy).
        """
        if await self._probe_runs_support():
            emitted = False
            try:
                async for delta in self._stream_via_runs(messages, options):
                    emitted = True
                    yield delta
                return
            except ProviderError:
                if emitted:
                    raise
                logger.warning(
                    "hermes runs transport failed pre-stream; "
                    "falling back to chat/completions"
                )
        async for delta in super().stream_chat(messages, options):
            yield delta

    async def chat(
        self, messages: list[dict[str, Any]], options: ChatOptions | None = None
    ):
        """Non-streaming chat stays on chat/completions (Runs is stream-only)."""
        return await super().chat(messages, options)

    # -- legacy chat/completions SSE (fallback transport) ------------------------
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
