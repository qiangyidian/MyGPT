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

import base64
import binascii
import json
import logging
import os
import re
import time
from collections.abc import AsyncIterator
from typing import Any

import httpx

from app.providers.base import ChatDelta, ChatOptions, ProviderError
from app.providers.openai_compatible import (
    _RETRYABLE_EXC,
    OpenAICompatibleProvider,
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
        saw_text = False  # any token delta streamed for this run
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
                # ``run.completed`` carries the full final answer in ``output``
                # (older versions: ``message``/``text``). Use it only when no
                # token deltas streamed — the consumer appends every content
                # chunk, so yielding it after streamed deltas would double the
                # message text.
                final_text = (
                    "" if saw_text
                    else str(data.get("output") or data.get("message") or data.get("text") or "")
                )
                yield ChatDelta(
                    content=final_text,
                    finish_reason=_RUN_TERMINAL_FINISH[kind],  # type: ignore[arg-type]
                    usage=final_usage,
                )
                return

            # -- token deltas -------------------------------------------------------
            # Hermes v0.20.x streams text as ``message.delta``; the other names
            # cover older/alternate versions (matched leniently on purpose).
            if kind in ("message.delta", "assistant.delta", "token", "run.delta"):
                text = data.get("delta") or data.get("text") or ""
                if text:
                    saw_text = True
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

    # -- file delivery (2026-08: chat file cards) -------------------------------
    # A Hermes turn often ends with "文件已生成: /root/report.pptx" — the file
    # lives on the HERMES host, unreachable from the browser. fetch_file brings
    # it back as bytes so the chat layer can land it in the platform's artifact
    # store and render a download card in the conversation.

    # Hard cap for one fetched file — matches the platform's upload ceiling and
    # keeps a misbehaving agent from streaming gigabytes through the chat turn.
    MAX_FETCH_FILE_BYTES = 64 * 1024 * 1024  # 64 MB

    async def fetch_file(self, path: str) -> tuple[bytes, str]:
        """Read one file from the Hermes host → (data, media_type).

        Strategy: local direct read when the path exists on THIS machine
        (backend co-located with Hermes — the common self-hosted deploy);
        otherwise ask the agent itself to base64 the file via a one-shot run.
        Raises ProviderError on any failure (missing, too large, unparseable).
        """
        if not path or not _is_absolute_path(path):
            raise ProviderError(f"not an absolute path: {path!r}")

        # Local fast path (same-host deploy).
        if os.path.isfile(path):
            size = os.path.getsize(path)
            if size > self.MAX_FETCH_FILE_BYTES:
                raise ProviderError(f"file too large ({size} bytes): {path}")
            with open(path, "rb") as fh:
                return fh.read(), _guess_media_type(path)

        # Remote: one-shot run that prints the file as base64.
        data = await self._fetch_file_via_agent(path)
        return data, _guess_media_type(path)

    async def _fetch_file_via_agent(self, path: str) -> bytes:
        """Have the Hermes agent read ``path`` and return its base64 content.

        Works on both transports: the chat/completions fallback buffers the
        whole assistant message; the Runs path subscribes to one run's events.
        The prompt pins an exact output format (``<B64>...</B64>``) and bans
        commentary so decoding is deterministic.
        """
        prompt = (
            f"Read the file at {path} and output ONLY its base64 encoding "
            f"wrapped exactly like <B64>BASE64</B64> with no other text, no "
            f"markdown fences, no commentary. If the file does not exist or "
            f"you cannot read it, output exactly <B64_ERROR>cannot read</B64_ERROR>."
        )
        chunks: list[ChatDelta] = []

        async def _collect() -> None:
            if await self._probe_runs_support():
                async for d in self._stream_via_runs(
                    [{"role": "user", "content": prompt}], None
                ):
                    chunks.append(d)
            else:
                async for d in super(HermesProvider, self).stream_chat(
                    [{"role": "user", "content": prompt}], None
                ):
                    chunks.append(d)

        try:
            await _collect()
        except ProviderError:
            raise
        except Exception as exc:
            raise ProviderError(f"hermes fetch_file transport failed: {exc}") from exc

        text = "".join(c.content for c in chunks)
        return _decode_b64_payload(text, path)

    def _fetch_via_chat_completions(self, prompt: str) -> AsyncIterator[ChatDelta]:
        """Direct generator over the inherited chat/completions transport.

        (Kept as a thin named wrapper for testability.)
        """
        return super().stream_chat([{"role": "user", "content": prompt}], None)

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


# ---------------------------------------------------------------------------
# File-delivery helpers (module-level for testability)
# ---------------------------------------------------------------------------

# Extensions safe to auto-attach as chat file cards. Anything else (source
# code of the server itself, dotfiles, executables) is left as plain text —
# we only turn DELIVERABLES into downloads, never arbitrary host files.
DELIVERABLE_EXTENSIONS = frozenset({
    ".pdf", ".pptx", ".ppt", ".docx", ".doc", ".xlsx", ".xls",
    ".csv", ".txt", ".md", ".json", ".xml", ".yaml", ".yml",
    ".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg", ".zip",
    ".tar", ".gz", ".mp3", ".mp4", ".wav", ".html", ".py", ".ipynb",
})

# Absolute path in the final assistant text (Windows or POSIX). Filenames
# are treated as space-free (word chars, dots, dashes, underscores, CJK-free)
# — agents overwhelmingly name generated files that way, and it keeps a
# trailing Chinese clause ("已生成 /root/x.pdf 重复一次…") from being
# swallowed into the match.
# One path segment. Must include CJK (JP/KR for good measure): Hermes writes
# deliverables like /root/report/AI新闻简报_2026-08-31.pptx, and an
# ASCII-only segment class truncates at the first 非-ASCII char so nothing
# with a Chinese filename ever matches.
_PATH_SEG = r"[A-Za-z0-9_.\-一-鿿぀-ヿ가-힯]+"
_FILE_PATH_RE = re.compile(
    r"(?<![\w./\\-])"
    r"("
    rf"[A-Za-z]:\\(?:{_PATH_SEG}\\)*{_PATH_SEG}\.[A-Za-z0-9]{{2,8}}"
    rf"|/(?:{_PATH_SEG}/)*{_PATH_SEG}\.[A-Za-z0-9]{{2,8}}"
    r")"
    r"(?![\w.])",
)

# Never auto-deliver from these prefixes even with a safe extension (the
# Hermes host's own secrets/config are not "files the user asked for").
_FORBIDDEN_PREFIXES = ("/etc/", "/proc/", "/sys/", "/dev/", "C:\\Windows\\")


def extract_deliverable_paths(text: str) -> list[str]:
    """Find file paths in an assistant reply worth turning into downloads.

    Deduplicated, order-preserved, filtered by extension whitelist and
    forbidden-location blacklist. Relative paths are ignored (too ambiguous
    — the platform can't verify where they live).
    """
    if not text:
        return []
    seen: set[str] = set()
    out: list[str] = []
    for m in _FILE_PATH_RE.finditer(text):
        raw = m.group(1).rstrip(".,;:!?)]}\"'")
        normalized = raw.replace("\\", "/")
        lowered = normalized.lower()
        ext = os.path.splitext(lowered)[1]
        if ext not in DELIVERABLE_EXTENSIONS:
            continue
        if any(lowered.startswith(p.lower().replace("\\", "/")) for p in _FORBIDDEN_PREFIXES):
            continue
        if raw not in seen:
            seen.add(raw)
            out.append(raw)
    return out


def _guess_media_type(path: str) -> str:
    """Cheap extension→MIME map (no stdlib mimetypes guess on every platform)."""
    ext = os.path.splitext(path)[1].lower()
    return _MIME_BY_EXT.get(ext, "application/octet-stream")


def _is_absolute_path(path: str) -> bool:
    """Platform-independent absolute check: ``/root/x`` is absolute even when
    the backend runs on Windows (the path refers to the POSIX Hermes host)."""
    return path.startswith("/") or re.match(r"^[A-Za-z]:[\\/]", path) is not None


_MIME_BY_EXT: dict[str, str] = {
    ".pdf": "application/pdf",
    ".pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    ".ppt": "application/vnd.ms-powerpoint",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".doc": "application/msword",
    ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    ".xls": "application/vnd.ms-excel",
    ".csv": "text/csv",
    ".txt": "text/plain",
    ".md": "text/markdown",
    ".json": "application/json",
    ".xml": "application/xml",
    ".yaml": "application/yaml",
    ".yml": "application/yaml",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
    ".svg": "image/svg+xml",
    ".zip": "application/zip",
    ".tar": "application/x-tar",
    ".gz": "application/gzip",
    ".mp3": "audio/mpeg",
    ".mp4": "video/mp4",
    ".wav": "audio/wav",
    ".html": "text/html",
    ".py": "text/x-python",
    ".ipynb": "application/x-ipynb+json",
}


_B64_RE = re.compile(r"<B64>([A-Za-z0-9+/=\s]*)</B64>")
_B64_ERROR_RE = re.compile(r"<B64_ERROR>(.*?)</B64_ERROR>")


def _decode_b64_payload(text: str, path: str) -> bytes:
    """Extract + decode the <B64>…</B64> payload from an agent reply."""
    err = _B64_ERROR_RE.search(text)
    if err and not _B64_RE.search(text):
        raise ProviderError(f"hermes agent could not read {path}: {err.group(1).strip()}")
    m = _B64_RE.search(text)
    if not m:
        snippet = text.strip()[:120]
        raise ProviderError(
            f"hermes agent returned no <B64> payload for {path} (got: {snippet!r})"
        )
    compact = re.sub(r"\s+", "", m.group(1))
    try:
        data = base64.b64decode(compact, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise ProviderError(f"hermes agent returned invalid base64 for {path}") from exc
    if not data:
        raise ProviderError(f"hermes agent returned empty file for {path}")
    if len(data) > HermesProvider.MAX_FETCH_FILE_BYTES:
        raise ProviderError(f"file too large via agent ({len(data)} bytes): {path}")
    return data
