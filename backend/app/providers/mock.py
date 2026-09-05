"""Mock provider.

Returns canned, deterministic responses so the app can run end-to-end without
any real model endpoint. Streaming is observable: the reply is yielded
word-by-word with small delays.

When called with tools available (agent / deep-search mode), it SIMULATES one
``web_search`` tool call on the first round and then answers on the second — so
the multi-round reasoning + search UX is demoable with zero external services.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
from collections.abc import AsyncIterator
from typing import Any

from app.core.config import get_settings
from app.providers.base import (
    ChatDelta,
    ChatOptions,
    ChatResult,
    ModelProvider,
    ToolCallDef,
    admit_provider_payload,
)


class MockProvider(ModelProvider):
    """Echo-style provider. No network, no secrets required."""

    provider_name = "mock"

    @staticmethod
    def _last_user_text(messages: list[dict[str, Any]]) -> str:
        for msg in reversed(messages):
            if msg.get("role") == "user":
                content = msg.get("content")
                if isinstance(content, str):
                    return content
                if isinstance(content, list):
                    parts = [p.get("text", "") for p in content if isinstance(p, dict)]
                    return " ".join(p for p in parts if p)
                return ""
        return ""

    @staticmethod
    def _canned_reply(messages: list[dict[str, Any]]) -> str:
        last_user = MockProvider._last_user_text(messages)
        echo = f"You said: {last_user}" if last_user else "Hello from the mock provider."
        return (
            f"{echo}\n\n(mock provider — no real model was contacted. "
            f"Configure an OpenAI-compatible endpoint to enable live responses.)"
        )

    async def chat(
        self, messages: list[dict[str, Any]], options: ChatOptions | None = None
    ) -> ChatResult:
        admit_provider_payload(self, messages, options)
        # Simulate a little processing latency.
        await asyncio.sleep(0.01)
        content = self._canned_reply(messages)
        return ChatResult(
            content=content,
            tool_calls=None,
            finish_reason="stop",
            usage={
                "prompt_tokens": sum(
                    len(str(m.get("content", "")).split()) for m in messages
                ),
                "completion_tokens": len(content.split()),
                "total_tokens": sum(
                    len(str(m.get("content", "")).split()) for m in messages
                ) + len(content.split()),
            },
        )

    async def stream_chat(
        self, messages: list[dict[str, Any]], options: ChatOptions | None = None
    ) -> AsyncIterator[ChatDelta]:
        """Yield the canned reply word-by-word so streaming is visibly observable.

        In agent mode (tools provided) and before any tool result has come back
        this turn, emit a single simulated ``web_search`` call instead — the
        agent loop will execute it, feed the result back, and we answer next round.
        """
        opts = admit_provider_payload(self, messages, options)
        already_searched = any(m.get("role") == "tool" for m in messages)
        if opts.tools and not already_searched:
            query = (self._last_user_text(messages) or "the topic").strip().replace("\n", " ")[:80]
            await asyncio.sleep(0.15)
            yield ChatDelta(
                tool_calls=[
                    ToolCallDef(
                        id="call_mock_search",
                        name="web_search",
                        arguments=json.dumps({"query": query}, ensure_ascii=False),
                    )
                ],
                finish_reason="tool_calls",
            )
            return

        content = self._canned_reply(messages)
        # Split keeping whitespace tokens so the reassembled text matches exactly.
        tokens = content.split(" ")
        for i, tok in enumerate(tokens):
            sep = " " if i > 0 else ""
            await asyncio.sleep(0.05)  # observable cadence
            yield ChatDelta(content=f"{sep}{tok}", tool_calls=None, finish_reason=None)
        # Final usage-only chunk (mirrors OpenAI include_usage). Without this the
        # streaming path's token accounting is never exercised, so downstream
        # persistence of usage metadata was untestable.
        yield ChatDelta(
            content="",
            tool_calls=None,
            finish_reason="stop",
            usage={
                "prompt_tokens": sum(
                    len(str(m.get("content", "")).split()) for m in messages
                ),
                "completion_tokens": len(content.split()),
                "total_tokens": sum(
                    len(str(m.get("content", "")).split()) for m in messages
                ) + len(content.split()),
            },
        )

    async def embeddings(self, texts: list[str], model: str | None = None) -> list[list[float]]:
        """Deterministic fake vectors of dim QDRANT_EMBEDDING_DIM.

        Same input -> same vector (hash-seeded), so retrieval / dedup logic that
        depends on embedding stability still behaves correctly under the mock.
        """
        dim = get_settings().QDRANT_EMBEDDING_DIM
        vectors: list[list[float]] = []
        for text in texts:
            digest = hashlib.sha256(text.encode("utf-8")).digest()
            # Expand the 32-byte digest into `dim` floats by cycling bytes.
            vec = []
            for i in range(dim):
                b = digest[i % len(digest)]
                # Map byte [0,255] to [-1,1]-ish float, normalized.
                vec.append((b / 255.0) * 2 - 1)
            vectors.append(vec)
        return vectors
