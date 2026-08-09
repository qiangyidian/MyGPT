"""OpenAI-compatible provider.

Talks to any endpoint that follows the OpenAI Chat Completions / Embeddings
shape (vLLM, Ollama's OpenAI shim, OpenAI itself, etc.). All HTTP goes through
httpx.AsyncClient with a 30s timeout and tenacity retry on transient errors.
"""
from __future__ import annotations

import asyncio
import json
from typing import Any, AsyncIterator

import httpx
from tenacity import (
    AsyncRetrying,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from app.providers.base import (
    ChatDelta,
    ChatOptions,
    ChatResult,
    FinishReason,
    ModelProvider,
    PROVIDER_ERR_NETWORK,
    PROVIDER_ERR_TIMEOUT,
    ProviderError,
    ToolCallDef,
)
from app.core.config import get_settings

# Retried transient failures: network blips, timeouts, and 5xx.
_RETRYABLE_EXC = (
    httpx.TransportError,
    httpx.TimeoutException,
)

_RETRYABLE_STATUS = {408, 429, 500, 502, 503, 504}


def _is_retryable_response(resp: httpx.Response) -> bool:
    return resp.status_code in _RETRYABLE_STATUS


def _to_provider_error(exc: Exception, *, where: str) -> ProviderError:
    """Map an httpx exception to a typed ProviderError.

    Timeouts get a distinct code (PROVIDER_ERR_TIMEOUT) so the runtime can map
    them to finish_reason="timeout" instead of a generic "error".
    """
    if isinstance(exc, httpx.TimeoutException):
        return ProviderError(
            f"model endpoint timed out ({where})", code=PROVIDER_ERR_TIMEOUT
        )
    return ProviderError(f"transport failure ({where}): {exc}", code=PROVIDER_ERR_NETWORK)


class OpenAICompatibleProvider(ModelProvider):
    """OpenAI-shaped chat + embeddings over an async HTTP client."""

    provider_name = "openai-compatible"

    def __init__(self, *, base_url: str, api_key: str = "", model: str = "", **_: Any) -> None:
        super().__init__(base_url=base_url, api_key=api_key, model=model)
        # Generous read timeout so slow / long (code) generations aren't killed
        # mid-stream; connect stays short. Driven by Settings, not hardcoded.
        s = get_settings()
        self._timeout = httpx.Timeout(
            read=s.MODEL_READ_TIMEOUT_SECONDS,
            connect=s.MODEL_CONNECT_TIMEOUT_SECONDS,
            write=s.MODEL_WRITE_TIMEOUT_SECONDS,
            pool=s.MODEL_POOL_TIMEOUT_SECONDS,
        )

    # -- HTTP helpers --------------------------------------------------------
    def _headers(self) -> dict[str, str]:
        h: dict[str, str] = {"Content-Type": "application/json", "Accept": "application/json"}
        if self.api_key:
            h["Authorization"] = f"Bearer {self.api_key}"
        return h

    def _chat_url(self) -> str:
        return f"{self.base_url}/chat/completions"

    def _embeddings_url(self) -> str:
        return f"{self.base_url}/embeddings"

    async def _request(
        self,
        client: httpx.AsyncClient,
        url: str,
        payload: dict[str, Any],
    ) -> httpx.Response:
        """POST with retry on transient errors. Raises ProviderError on failure."""
        async for attempt in AsyncRetrying(
            stop=stop_after_attempt(5),
            wait=wait_exponential(multiplier=1, min=1, max=10),
            retry=retry_if_exception_type(_RETRYABLE_EXC),
            reraise=True,
        ):
            with attempt:
                resp = await client.post(url, json=payload, headers=self._headers())
                # Retry on transient HTTP status by raising a transport error.
                if _is_retryable_response(resp):
                    raise httpx.TransportError(
                        f"transient HTTP {resp.status_code} from {url}"
                    )
                return resp
        # Unreachable: AsyncRetrying either returns or reraises.
        raise ProviderError(f"unreachable retry exit for {url}")

    @staticmethod
    def _raise_for_status(resp: httpx.Response, url: str) -> None:
        if resp.status_code >= 400:
            # Surface the upstream body so callers can see the real error.
            snippet = resp.text[:500] if resp.text else ""
            if resp.status_code in (401, 403):
                raise ProviderError(
                    f"auth error {resp.status_code} from {url}: {snippet}"
                )
            raise ProviderError(
                f"HTTP {resp.status_code} from {url}: {snippet}"
            )

    # -- request body builders ---------------------------------------------
    @staticmethod
    def _build_chat_payload(
        model: str, messages: list[dict[str, Any]], options: ChatOptions | None, stream: bool
    ) -> dict[str, Any]:
        opts = options or ChatOptions()
        payload: dict[str, Any] = {
            "model": model,
            "messages": messages,
            "temperature": opts.temperature,
            "top_p": opts.top_p,
            "stream": stream,
        }
        if stream:
            # Ask OpenAI-compatible endpoints to emit a final usage-only chunk so
            # we can persist per-message token accounting (cost/budget).
            payload["stream_options"] = {"include_usage": True}
        if opts.tools:
            payload["tools"] = opts.tools
            payload["tool_choice"] = opts.tool_choice
        if opts.stop:
            payload["stop"] = opts.stop
        if opts.extra:
            payload.update(opts.extra)
        # Generic output budgeting maps to exactly one provider parameter.
        # Remove either spelling supplied through ``extra`` so callers can
        # never accidentally emit both and trigger an upstream 400.
        payload.pop("max_tokens", None)
        payload.pop("max_completion_tokens", None)
        if opts.max_tokens is not None:
            payload[opts.output_token_parameter] = opts.max_tokens
        return payload

    @staticmethod
    def _parse_tool_calls(raw: list[dict[str, Any]] | None) -> list[ToolCallDef]:
        if not raw:
            return []
        out: list[ToolCallDef] = []
        for tc in raw:
            tc_id = tc.get("id") or ""
            func = tc.get("function") or {}
            out.append(
                ToolCallDef(
                    id=tc_id,
                    name=func.get("name") or "",
                    arguments=func.get("arguments") or "",
                )
            )
        return out

    # -- ModelProvider impl --------------------------------------------------
    async def chat(
        self, messages: list[dict[str, Any]], options: ChatOptions | None = None
    ) -> ChatResult:
        payload = self._build_chat_payload(self.model, messages, options, stream=False)
        try:
            async with httpx.AsyncClient(timeout=self._timeout) as client:
                resp = await self._request(client, self._chat_url(), payload)
        except _RETRYABLE_EXC as exc:
            raise _to_provider_error(exc, where="chat") from exc

        self._raise_for_status(resp, self._chat_url())

        try:
            data = resp.json()
        except ValueError as exc:
            raise ProviderError(f"invalid JSON from model endpoint: {exc}") from exc

        try:
            choice = (data.get("choices") or [{}])[0]
        except IndexError:
            choice = {}
        message = choice.get("message") or {}
        content = message.get("content") or ""
        tool_calls = self._parse_tool_calls(message.get("tool_calls"))
        finish_reason = choice.get("finish_reason") or "stop"
        usage = data.get("usage")
        return ChatResult(
            content=content,
            tool_calls=tool_calls or None,
            finish_reason=finish_reason,
            usage=usage,
        )

    async def stream_chat(
        self, messages: list[dict[str, Any]], options: ChatOptions | None = None
    ) -> AsyncIterator[ChatDelta]:
        """Yield ChatDelta chunks parsed from the SSE stream.

        Note: the base class declares this as a plain (non-async) generator,
        but it is always consumed via `async for`. We define it as an async
        generator (the natural shape for an SSE stream) — callers iterate it
        with `async for`, which is compatible with the AsyncIterator contract.
        """
        payload = self._build_chat_payload(self.model, messages, options, stream=True)
        try:
            async with httpx.AsyncClient(timeout=self._timeout) as client:
                # Retry the INITIAL response on transient errors (502/503/429/...)
                # AND transient transport errors (connect/read timeout) — both are
                # safe because no tokens have been emitted yet. Once the stream
                # starts iterating, we do NOT retry (would duplicate tokens).
                for attempt in range(1, 6):
                    started_iter = False
                    try:
                        async with client.stream(
                            "POST", self._chat_url(), json=payload, headers=self._headers()
                        ) as resp:
                            if resp.status_code in _RETRYABLE_STATUS:
                                await resp.aread()  # drain the error body before retrying
                                if attempt < 5:
                                    await asyncio.sleep(min(2 ** attempt, 10))
                                    continue
                                raise ProviderError(
                                    f"transient HTTP {resp.status_code} from {self._chat_url()} after retries"
                                )
                            if resp.status_code >= 400:
                                body = (await resp.aread()).decode(errors="replace")[:500]
                                if resp.status_code in (401, 403):
                                    raise ProviderError(
                                        f"auth error {resp.status_code} from {self._chat_url()}: {body}"
                                    )
                                raise ProviderError(
                                    f"HTTP {resp.status_code} from {self._chat_url()}: {body}"
                                )
                            started_iter = True
                            async for chunk in self._iter_sse(resp):
                                yield chunk
                            return
                    except _RETRYABLE_EXC as exc:
                        # Transport error before the first token → retry (matches
                        # the comment). A mid-stream error (started_iter) is NOT
                        # retried — resending would duplicate emitted tokens.
                        if not started_iter and attempt < 5:
                            await asyncio.sleep(min(2 ** attempt, 10))
                            continue
                        raise _to_provider_error(exc, where="stream") from exc
        except ProviderError:
            raise
        except _RETRYABLE_EXC as exc:
            raise _to_provider_error(exc, where="stream") from exc

    async def _iter_sse(self, resp: httpx.Response) -> AsyncIterator[ChatDelta]:
        """Parse OpenAI-style SSE: lines `data: {json}`, terminator `data: [DONE]`.

        Preserves the REAL upstream finish_reason: a `[DONE]` marker is a transport
        terminator only. If the stream already carried an explicit finish_reason
        (length/content_filter/stop/...), we do NOT emit a synthetic `stop` that
        would overwrite it (the old code did, clobbering `length`).
        """
        # Accumulate per-tool-call deltas (id/function.name/arguments stream in pieces).
        tool_accum: dict[int, dict[str, Any]] = {}
        seen_real_finish: FinishReason | None = None
        final_usage: dict[str, int] | None = None
        async for raw_line in resp.aiter_lines():
            if not raw_line:
                continue
            line = raw_line.strip()
            if not line.startswith("data:"):
                # Ignore keepalive/comments.
                continue
            data_str = line[len("data:"):].strip()
            if data_str == "[DONE]":
                # Flush any accumulated tool calls as a final delta.
                if tool_accum:
                    yield self._flush_tool_accum(tool_accum)
                    tool_accum.clear()
                    # A tool-call flush carries finish_reason="tool_calls" — treat
                    # it as the real reason so we don't then emit a synthetic
                    # "stop" that would clobber it (and drop the tool calls).
                    seen_real_finish = "tool_calls"
                # Only synthesize a `stop` if the stream never carried an explicit
                # finish_reason; otherwise the real reason already went out.
                if seen_real_finish is None:
                    yield ChatDelta(content="", tool_calls=None, finish_reason="stop")
                # Emit the captured usage (if any) as a final out-of-band delta so
                # callers can persist per-message token accounting.
                if final_usage is not None:
                    yield ChatDelta(content="", tool_calls=None, finish_reason=None, usage=final_usage)
                return
            try:
                obj = json.loads(data_str)
            except json.JSONDecodeError:
                # Skip malformed chunk rather than killing the stream.
                continue
            choices = obj.get("choices") or []
            if not choices:
                # usage-only / routing chunk — capture usage (for token accounting)
                # but never end generation on it.
                if obj.get("usage"):
                    final_usage = obj["usage"]
                continue
            choice = choices[0]
            delta = choice.get("delta") or {}
            content = delta.get("content") or ""
            finish_reason = choice.get("finish_reason")
            raw_tcs = delta.get("tool_calls")
            if raw_tcs:
                # delta tool_calls carry an `index` to identify which call they extend.
                for tc in raw_tcs:
                    idx = tc.get("index", 0)
                    slot = tool_accum.setdefault(
                        idx, {"id": "", "name": "", "arguments": ""}
                    )
                    if tc.get("id"):
                        slot["id"] = tc["id"]
                    func = tc.get("function") or {}
                    if func.get("name"):
                        slot["name"] = slot["name"] + func["name"]
                    if func.get("arguments"):
                        slot["arguments"] = slot["arguments"] + func["arguments"]
            # Emit content deltas immediately; hold tool calls until flushed
            # so callers see well-formed ToolCallDefs.
            if content:
                yield ChatDelta(content=content, tool_calls=None, finish_reason=finish_reason)
                if finish_reason:
                    seen_real_finish = finish_reason
            elif finish_reason:
                # Terminal chunk (possibly alongside tool calls). Flush tools first
                # and always carry the real reason — the old `and not raw_tcs` guard
                # dropped finish_reason when a terminal chunk also held tool calls.
                if tool_accum:
                    yield self._flush_tool_accum(tool_accum)
                    tool_accum.clear()
                yield ChatDelta(content="", tool_calls=None, finish_reason=finish_reason)
                seen_real_finish = finish_reason

    @staticmethod
    def _flush_tool_accum(tool_accum: dict[int, dict[str, Any]]) -> ChatDelta:
        calls = [
            ToolCallDef(
                id=slot.get("id") or "",
                name=slot.get("name") or "",
                arguments=slot.get("arguments") or "",
            )
            for _, slot in sorted(tool_accum.items())
        ]
        return ChatDelta(content="", tool_calls=calls, finish_reason="tool_calls")

    async def embeddings(self, texts: list[str], model: str | None = None) -> list[list[float]]:
        payload = {
            "model": model or self.model,
            "input": texts,
        }
        url = self._embeddings_url()
        try:
            async with httpx.AsyncClient(timeout=self._timeout) as client:
                resp = await self._request(client, url, payload)
        except _RETRYABLE_EXC as exc:
            raise _to_provider_error(exc, where="embeddings") from exc

        self._raise_for_status(resp, url)

        try:
            data = resp.json()
        except ValueError as exc:
            raise ProviderError(f"invalid JSON from embeddings endpoint: {exc}") from exc

        items = data.get("data") or []
        # Preserve input order via the `index` field when present.
        indexed: list[tuple[int, list[float]]] = []
        for item in items:
            vec = item.get("embedding")
            if vec is None:
                continue
            idx = item.get("index", len(indexed))
            indexed.append((idx, [float(x) for x in vec]))
        indexed.sort(key=lambda t: t[0])
        return [vec for _, vec in indexed]
