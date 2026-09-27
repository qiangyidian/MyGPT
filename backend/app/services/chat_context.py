"""Token accounting and prompt admission helpers for chat requests."""
from __future__ import annotations

import logging
from typing import Any

import tiktoken

from app.agents.token_budget import (
    PROMPT_TOO_LARGE,
    PromptAdmissionError,
    admit_latest_turn,
    calculate_prompt_budget,
)
from app.model_capabilities import capabilities_from_config
from app.models import ModelConfig

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

# Resolved tiktoken encodings by model name. encoding_for_model is registry-
# cached internally, but the miss path (unknown model -> fallback chain) was
# re-walked per MESSAGE on every trim; memoize the final encoding per model.
_encoding_cache: dict[str, tiktoken.Encoding | None] = {}


def _resolve_encoding(model_name: str) -> tiktoken.Encoding | None:
    if model_name in _encoding_cache:
        return _encoding_cache[model_name]
    enc: tiktoken.Encoding | None
    try:
        enc = tiktoken.encoding_for_model(model_name)
    except Exception:
        try:
            enc = tiktoken.get_encoding("cl100k_base")
        except Exception:
            enc = None
    _encoding_cache[model_name] = enc
    return enc


def _estimate_tokens(text: str, model_name: str) -> int:
    """Best-effort token count.

    Tries the tiktoken encoding matching ``model_name``; on any failure falls
    back to cl100k_base, then to a character heuristic — we never hard-fail on
    trimming, we just get less precise.
    """
    if not text:
        return 0
    enc = _resolve_encoding(model_name)
    if enc is not None:
        return len(enc.encode(text))
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
    if not enable_tools or not caps.supports_tools:
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
    except Exception:
        logger.warning("tool schema token estimation failed", exc_info=True)
        return 0


