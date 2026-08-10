"""Build a CrewAI ``LLM`` from an app :class:`ModelConfig`.

CrewAI uses LiteLLM under the hood. For OpenAI-compatible endpoints we prefix
the model name with ``openai/`` and pass ``base_url`` + decrypted ``api_key``.
The API key never leaves the existing encrypted ``ModelConfig`` — CrewAI does
not read browser input or a separate secret store.
"""
from __future__ import annotations

import asyncio
import json
from functools import wraps
from typing import Any

from app.agents.token_budget import (
    PROMPT_TOO_LARGE,
    PromptAdmissionError,
    calculate_prompt_budget,
)
from app.core.security import decrypt_secret
from app.model_capabilities import capabilities_from_config
from app.models import ModelConfig


def _final_payload_parts(
    args: tuple[Any, ...], kwargs: dict[str, Any]
) -> tuple[Any | None, Any | None]:
    messages = args[0] if args else kwargs.get("messages")
    tools = args[1] if len(args) > 1 else kwargs.get("tools")
    return messages, tools


def _admit_final_crewai_payload(messages: Any, tools: Any, cfg: Any) -> None:
    """Reject the final serialized CrewAI payload before provider delegation."""
    if messages is None:
        return

    from app.services.chat_service import _estimate_tokens

    caps = capabilities_from_config(cfg)
    model_name = getattr(cfg, "model_name", "") or ""
    normalized_messages = (
        [{"role": "user", "content": messages}]
        if isinstance(messages, str)
        else messages
    )
    message_tokens = _estimate_tokens(
        json.dumps(normalized_messages, ensure_ascii=False, default=str), model_name
    )
    tool_tokens = (
        _estimate_tokens(
            json.dumps(tools, ensure_ascii=False, default=str), model_name
        )
        if tools
        else 0
    )
    budget = calculate_prompt_budget(
        caps,
        requested_output=caps.max_output_tokens,
        tool_schema_tokens=tool_tokens,
    )
    if message_tokens > budget.input_tokens:
        raise PromptAdmissionError(
            PROMPT_TOO_LARGE,
            "The final CrewAI model payload exceeds the configured prompt budget",
        )


def wrap_crewai_llm_with_budget(
    llm: Any, cfg: Any, *, budget_guard: Any = None
) -> Any:
    """Decorate CrewAI's real sync/async call boundary without changing type.

    CrewAI validates ``Agent.llm`` as a ``BaseLLM``. Decorating the existing
    instance in place preserves its concrete provider class, serialization,
    capability methods, and every attribute while gating both final call paths.
    """
    if getattr(llm, "_model_budget_guarded", False):
        if getattr(llm, "_run_budget_guard", None) is not budget_guard:
            raise ValueError("CrewAI LLM cannot be rebound to another run budget guard")
        return llm

    original_call = getattr(llm, "call", None)
    if not callable(original_call):
        raise TypeError("CrewAI LLM must expose call()")

    @wraps(original_call)
    def guarded_call(*args: Any, **kwargs: Any) -> Any:
        messages, tools = _final_payload_parts(args, kwargs)
        _admit_final_crewai_payload(messages, tools, cfg)
        if budget_guard is not None:
            budget_guard.enter_step()
            budget_guard.check()
        result = original_call(*args, **kwargs)
        if budget_guard is not None:
            budget_guard.check()
        return result

    llm.call = guarded_call

    original_acall = getattr(llm, "acall", None)
    if callable(original_acall):

        @wraps(original_acall)
        async def guarded_acall(*args: Any, **kwargs: Any) -> Any:
            messages, tools = _final_payload_parts(args, kwargs)
            _admit_final_crewai_payload(messages, tools, cfg)
            if budget_guard is None:
                return await original_acall(*args, **kwargs)
            budget_guard.enter_step()
            budget_guard.check()
            try:
                async with asyncio.timeout(budget_guard.remaining_seconds):
                    result = await original_acall(*args, **kwargs)
            except TimeoutError as exc:
                from app.agents.schemas import BudgetExceeded

                raise BudgetExceeded(
                    f"time budget ({budget_guard.limits.max_runtime_seconds}s) exceeded"
                ) from exc
            budget_guard.check()
            return result

        llm.acall = guarded_acall

    llm._model_budget_guarded = True
    llm._run_budget_guard = budget_guard
    return llm


class CrewAILLMFactory:
    """Turn a ModelConfig row into a CrewAI LLM instance."""

    @staticmethod
    def from_model_config(cfg: ModelConfig, *, budget_guard: Any = None) -> Any:
        from crewai import LLM  # lazy: crewai is optional

        api_key = decrypt_secret(cfg.api_key_encrypted or "") or "dummy"

        # LiteLLM convention: openai-compatible providers use the "openai/" prefix.
        model_name = cfg.model_name
        if "/" not in model_name:
            model_name = f"openai/{model_name}"

        kwargs: dict[str, Any] = dict(
            model=model_name,
            base_url=cfg.api_base_url,
            api_key=api_key,
            temperature=cfg.temperature,
            # Stream so the gateway's per-request actor (30s on the GLM proxy)
            # stays alive while tokens flow. Non-stream, a single 2048-token
            # reasoning call takes ~70s and 500s ("Actor timed out").
            stream=True,
            # The workflow owns retries so each attempt crosses the run budget.
            max_retries=0,
        )
        output_parameter = capabilities_from_config(cfg).output_token_parameter
        kwargs[output_parameter] = cfg.max_tokens
        if cfg.top_p is not None:
            kwargs["top_p"] = cfg.top_p

        return wrap_crewai_llm_with_budget(
            LLM(**kwargs), cfg, budget_guard=budget_guard
        )
