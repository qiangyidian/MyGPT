"""OpenAI-compatible provider.

Talks to any endpoint that follows the OpenAI Chat Completions / Embeddings
shape (vLLM, Ollama's OpenAI shim, OpenAI itself, etc.). All HTTP goes through
httpx.AsyncClient with a 30s timeout and tenacity retry on transient errors.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Any, AsyncIterator, Literal

import httpx
from tenacity import (
    AsyncRetrying,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from app.core.config import get_settings
from app.observability import observe_counter, observe_histogram, observe_span
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
    admit_provider_payload,
)

logger = logging.getLogger(__name__)

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
    logger.warning("model transport failure during %s: %r", where, exc)
    return ProviderError(
        f"model endpoint transport failure ({where})", code=PROVIDER_ERR_NETWORK
    )


class OpenAICompatibleProvider(ModelProvider):
    """OpenAI-shaped chat + embeddings over an async HTTP client."""

    provider_name = "openai-compatible"

    def __init__(
        self,
        *,
        base_url: str,
        api_key: str = "",
        model: str = "",
        output_token_parameter: Literal["max_tokens", "max_completion_tokens"] = "max_tokens",
        **extra: Any,
    ) -> None:
        super().__init__(
            base_url=base_url,
            api_key=api_key,
            model=model,
            output_token_parameter=output_token_parameter,
            **extra,
        )
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

    @staticmethod
    async def _sleep_before_retry(
        options: ChatOptions | None, next_attempt: int, delay: float
    ) -> None:
        """Authorize one retry before sleeping; bound sleep by run time left."""
        gate = options.retry_gate if options is not None else None
        remaining = gate(next_attempt) if gate is not None else None
        try:
            async with asyncio.timeout(remaining):
                await asyncio.sleep(delay)
        except TimeoutError as exc:
            raise ProviderError(
                "model retry exceeded the remaining run time",
                code=PROVIDER_ERR_TIMEOUT,
            ) from exc

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
        raise ProviderError("model endpoint retry failed")

    async def _request_form(
        self,
        client: httpx.AsyncClient,
        url: str,
        *,
        data: dict[str, Any] | None = None,
        files: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
    ) -> httpx.Response:
        """Multipart POST with the SAME transient-retry policy as :meth:`_request`.

        Used by the multimodal routes (transcription, image-edit) that send
        multipart bodies, so a 429/5xx on transcription retries just like a
        chat completion would.
        """
        async for attempt in AsyncRetrying(
            stop=stop_after_attempt(5),
            wait=wait_exponential(multiplier=1, min=1, max=10),
            retry=retry_if_exception_type(_RETRYABLE_EXC),
            reraise=True,
        ):
            with attempt:
                resp = await client.post(url, data=data, files=files, headers=headers or {})
                if _is_retryable_response(resp):
                    raise httpx.TransportError(
                        f"transient HTTP {resp.status_code} from {url}"
                    )
                return resp
        raise ProviderError("model endpoint retry failed")

    @staticmethod
    def _raise_for_status(resp: httpx.Response, _url: str) -> None:
        if resp.status_code >= 400:
            logger.warning("model endpoint returned HTTP %s", resp.status_code)
            if resp.status_code in (401, 403):
                raise ProviderError(
                    "model endpoint authentication failed"
                )
            raise ProviderError(f"model endpoint returned HTTP {resp.status_code}")

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
        # Observability (Task 11b): one span per model call; attributes are
        # redacted by observe_span (the api_key never appears). Inert no-op when
        # exporters are absent / OTEL_ENABLED is off.
        started = time.monotonic()
        with observe_span("model.call", model=self.model, provider=self.provider_name):
            admitted_options = admit_provider_payload(self, messages, options)
            payload = self._build_chat_payload(
                self.model, messages, admitted_options, stream=False
            )
            try:
                async with httpx.AsyncClient(timeout=self._timeout) as client:
                    resp = await self._request(client, self._chat_url(), payload)
            except _RETRYABLE_EXC as exc:
                observe_counter(
                    "model.calls", 1, model=self.model, outcome="error",
                )
                raise _to_provider_error(exc, where="chat") from exc

            self._raise_for_status(resp, self._chat_url())

            try:
                data = resp.json()
            except ValueError as exc:
                raise ProviderError("model endpoint returned invalid JSON") from exc

            try:
                choice = (data.get("choices") or [{}])[0]
            except IndexError:
                choice = {}
            message = choice.get("message") or {}
            content = message.get("content") or ""
            tool_calls = self._parse_tool_calls(message.get("tool_calls"))
            finish_reason = choice.get("finish_reason") or "stop"
            usage = data.get("usage")
        observe_counter(
            "model.calls", 1, model=self.model, outcome=str(finish_reason or "ok"),
        )
        observe_histogram(
            "model.latency_ms",
            int((time.monotonic() - started) * 1000),
            model=self.model,
            operation="chat",
        )
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
        started = time.monotonic()
        # Observability (Task 11b): span wraps the whole stream dispatch. We
        # can't wrap the yielding loop in a single `with` (it spans awaits that
        # yield to the caller), so the span opens here and the counter/histogram
        # fire at the end. Inert when exporters are off.
        with observe_span("model.stream", model=self.model, provider=self.provider_name):
            admitted_options = admit_provider_payload(self, messages, options)
            payload = self._build_chat_payload(
                self.model, messages, admitted_options, stream=True
            )
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
                                        await self._sleep_before_retry(
                                            admitted_options,
                                            attempt + 1,
                                            min(2 ** attempt, 10),
                                        )
                                        continue
                                    raise ProviderError(
                                        f"model endpoint returned HTTP {resp.status_code} after retries"
                                    )
                                if resp.status_code >= 400:
                                    await resp.aread()
                                    logger.warning(
                                        "model endpoint returned HTTP %s",
                                        resp.status_code,
                                    )
                                    if resp.status_code in (401, 403):
                                        raise ProviderError(
                                            "model endpoint authentication failed"
                                        )
                                    raise ProviderError(
                                        f"model endpoint returned HTTP {resp.status_code}"
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
                                await self._sleep_before_retry(
                                    admitted_options,
                                    attempt + 1,
                                    min(2 ** attempt, 10),
                                )
                                continue
                            raise _to_provider_error(exc, where="stream") from exc
            except ProviderError:
                observe_counter(
                    "model.calls", 1, model=self.model, outcome="error",
                )
                raise
            except _RETRYABLE_EXC as exc:
                observe_counter(
                    "model.calls", 1, model=self.model, outcome="error",
                )
                raise _to_provider_error(exc, where="stream") from exc
        observe_counter(
            "model.calls", 1, model=self.model, outcome="streamed",
        )
        observe_histogram(
            "model.latency_ms",
            int((time.monotonic() - started) * 1000),
            model=self.model,
            operation="stream",
        )

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
            if obj.get("usage"):
                final_usage = obj["usage"]
            choices = obj.get("choices") or []
            if not choices:
                # usage-only / routing chunk — capture usage (for token accounting)
                # but never end generation on it.
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

        # Some compatible gateways close the HTTP body without a literal
        # ``[DONE]`` marker. Usage-only chunks are still authoritative and must
        # survive that transport variation; never synthesize a finish reason.
        if final_usage is not None:
            yield ChatDelta(
                content="", tool_calls=None, finish_reason=None, usage=final_usage
            )

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
            raise ProviderError("model endpoint returned invalid embeddings JSON") from exc

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

    # ------------------------------------------------------------------ #
    # Multimodal routes (Task 10) — gated by capability checks.
    # ------------------------------------------------------------------ #
    # Each method re-checks the relevant ModelCapabilities flag BEFORE any HTTP
    # dispatch (defense-in-depth: route_multimodal already validated input
    # parts, but a misconfigured caller must never send audio to a text-only
    # endpoint). The endpoint paths follow the OpenAI shape (/audio/transcriptions,
    # /audio/speech, /images/generations, /images/edits).
    def _require_capability(self, flag: str, modality: str, label: str) -> None:
        from app.providers.multimodal import ModelCapabilityError

        if not bool(getattr(self.capabilities, flag, False)):
            raise ModelCapabilityError(
                f"model does not support {label}",
                modality=modality,
            )

    def _transcription_url(self) -> str:
        return f"{self.base_url}/audio/transcriptions"

    def _speech_url(self) -> str:
        return f"{self.base_url}/audio/speech"

    def _images_url(self) -> str:
        return f"{self.base_url}/images/generations"

    def _images_edits_url(self) -> str:
        return f"{self.base_url}/images/edits"

    async def transcribe(
        self,
        audio: bytes,
        *,
        mime_type: str = "audio/wav",
        filename: str = "audio.wav",
        language: str | None = None,
        prompt: str | None = None,
    ) -> str:
        """Audio → text (OpenAI-compatible /audio/transcriptions).

        Requires ``supports_audio_input``. Returns the transcribed text.
        """
        self._require_capability("supports_audio_input", "audio", "audio input (transcription)")
        # Multipart form per the OpenAI shape.
        files = {"file": (filename, audio, mime_type)}
        data: dict[str, str] = {"model": self.model}
        if language:
            data["language"] = language
        if prompt:
            data["prompt"] = prompt
        url = self._transcription_url()
        headers = {"Authorization": f"Bearer {self.api_key}"} if self.api_key else {}
        try:
            async with httpx.AsyncClient(timeout=self._timeout) as client:
                resp = await self._request_form(client, url, data=data, files=files, headers=headers)
        except _RETRYABLE_EXC as exc:
            raise _to_provider_error(exc, where="transcribe") from exc
        self._raise_for_status(resp, url)
        try:
            payload = resp.json()
        except ValueError as exc:
            raise ProviderError("transcription endpoint returned invalid JSON") from exc
        return payload.get("text") or ""

    async def speak(
        self,
        text: str,
        *,
        voice: str = "alloy",
        response_format: str = "mp3",
    ) -> bytes:
        """Text → audio (OpenAI-compatible /audio/speech).

        Requires ``supports_audio_output``. Returns the raw audio bytes.
        """
        self._require_capability("supports_audio_output", "audio_output", "audio output (speech)")
        payload = {
            "model": self.model,
            "input": text,
            "voice": voice,
            "response_format": response_format,
        }
        url = self._speech_url()
        try:
            async with httpx.AsyncClient(timeout=self._timeout) as client:
                # Route through _request so a transient 429/5xx retries (the
                # speech endpoint is the same transport as chat completions).
                resp = await self._request(client, url, payload)
        except _RETRYABLE_EXC as exc:
            raise _to_provider_error(exc, where="speak") from exc
        self._raise_for_status(resp, url)
        return resp.content

    async def generate_image(
        self,
        prompt: str,
        *,
        size: str = "1024x1024",
        n: int = 1,
        response_format: str = "url",
        model: str | None = None,
    ) -> list[dict[str, Any]]:
        """Text → image(s) (OpenAI-compatible /images/generations).

        Requires ``supports_image_generation``. Returns a list of
        ``{url|b64_json}`` dicts in input order.
        """
        self._require_capability("supports_image_generation", "image_generation", "image generation")
        payload: dict[str, Any] = {
            "model": model or self.model,
            "prompt": prompt,
            "size": size,
            "n": n,
            "response_format": response_format,
        }
        url = self._images_url()
        try:
            async with httpx.AsyncClient(timeout=self._timeout) as client:
                resp = await self._request(client, url, payload)
        except _RETRYABLE_EXC as exc:
            raise _to_provider_error(exc, where="generate_image") from exc
        self._raise_for_status(resp, url)
        try:
            data = resp.json()
        except ValueError as exc:
            raise ProviderError("image endpoint returned invalid JSON") from exc
        return list(data.get("data") or [])

    async def edit_image(
        self,
        image: bytes,
        *,
        prompt: str,
        mask: bytes | None = None,
        mime_type: str = "image/png",
        size: str = "1024x1024",
        n: int = 1,
        response_format: str = "url",
        model: str | None = None,
    ) -> list[dict[str, Any]]:
        """Image + prompt → edited image(s) (OpenAI-compatible /images/edits).

        Requires ``supports_image_generation``. ``image`` (and optional ``mask``)
        are uploaded as multipart form parts.
        """
        self._require_capability("supports_image_generation", "image_generation", "image editing")
        files: dict[str, tuple[str, bytes, str]] = {
            "image": ("image.png", image, mime_type),
        }
        if mask is not None:
            files["mask"] = ("mask.png", mask, mime_type)
        data: dict[str, str] = {
            "model": model or self.model,
            "prompt": prompt,
            "size": size,
            "n": str(n),
            "response_format": response_format,
        }
        url = self._images_edits_url()
        headers = {"Authorization": f"Bearer {self.api_key}"} if self.api_key else {}
        try:
            async with httpx.AsyncClient(timeout=self._timeout) as client:
                resp = await self._request_form(client, url, data=data, files=files, headers=headers)
        except _RETRYABLE_EXC as exc:
            raise _to_provider_error(exc, where="edit_image") from exc
        self._raise_for_status(resp, url)
        try:
            payload = resp.json()
        except ValueError as exc:
            raise ProviderError("image-edit endpoint returned invalid JSON") from exc
        return list(payload.get("data") or [])

