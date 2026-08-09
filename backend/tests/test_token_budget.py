from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.agents.token_budget import (
    PromptAdmissionError,
    admit_latest_turn,
    calculate_prompt_budget,
)
from app.model_capabilities import ModelCapabilities
from app.services.chat_service import _admit_and_trim_history


def test_prompt_budget_reserves_output_tools_and_safety_margin():
    caps = ModelCapabilities(context_window=10_000, max_output_tokens=2_000)

    budget = calculate_prompt_budget(
        caps, requested_output=3_000, tool_schema_tokens=500, safety_ratio=0.05
    )

    assert budget.reserved_output_tokens == 2_000
    assert budget.tool_schema_tokens == 500
    assert budget.safety_margin_tokens == 500
    assert budget.input_tokens == 7_000


def test_prompt_budget_clamps_requested_output_to_model_limit():
    caps = ModelCapabilities(context_window=8_000, max_output_tokens=1_000)

    budget = calculate_prompt_budget(caps, requested_output=5_000)

    assert budget.reserved_output_tokens == 1_000
    assert budget.safety_margin_tokens == 400
    assert budget.input_tokens == 6_600


def test_prompt_budget_uses_minimum_safety_margin():
    caps = ModelCapabilities(context_window=4_000, max_output_tokens=500)

    budget = calculate_prompt_budget(caps, requested_output=300, safety_ratio=0.01)

    assert budget.safety_margin_tokens == 256
    assert budget.input_tokens == 3_444


@pytest.mark.parametrize(
    ("requested_output", "tool_schema_tokens", "safety_ratio"),
    [(0, 0, 0.05), (100, -1, 0.05), (100, 0, -0.01), (100, 0, float("nan"))],
)
def test_invalid_budget_inputs_raise_stable_typed_error(
    requested_output: int, tool_schema_tokens: int, safety_ratio: float
):
    caps = ModelCapabilities(context_window=1_000, max_output_tokens=500)

    with pytest.raises(PromptAdmissionError) as exc_info:
        calculate_prompt_budget(
            caps,
            requested_output=requested_output,
            tool_schema_tokens=tool_schema_tokens,
            safety_ratio=safety_ratio,
        )

    assert exc_info.value.code == "invalid_prompt_budget"


def test_nonpositive_input_budget_raises_stable_typed_error():
    caps = ModelCapabilities(context_window=500, max_output_tokens=400)

    with pytest.raises(PromptAdmissionError) as exc_info:
        calculate_prompt_budget(caps, requested_output=400)

    assert exc_info.value.code == "invalid_prompt_budget"


def test_oversized_latest_turn_is_rejected():
    with pytest.raises(PromptAdmissionError) as exc_info:
        admit_latest_turn(latest_turn_tokens=501, input_budget=500)

    assert exc_info.value.code == "message_too_large"


def test_chat_history_admission_trims_prior_turns_but_keeps_newest():
    messages = [
        {"role": "system", "content": "system"},
        {"role": "user", "content": "old " + ("x" * 20_000)},
        {"role": "assistant", "content": "old answer"},
        {"role": "user", "content": "newest turn"},
    ]
    cfg = SimpleNamespace(max_context_tokens=2_000, max_tokens=500)

    admitted = _admit_and_trim_history(messages, cfg, model_name="mock-model")

    assert admitted[-1] == {"role": "user", "content": "newest turn"}
    assert all("old " not in str(message.get("content")) for message in admitted)


def test_chat_history_admission_never_silently_truncates_current_turn():
    messages = [
        {"role": "system", "content": "system"},
        {"role": "user", "content": "x" * 20_000},
    ]
    cfg = SimpleNamespace(max_context_tokens=1_000, max_tokens=400)

    with pytest.raises(PromptAdmissionError) as exc_info:
        _admit_and_trim_history(messages, cfg, model_name="mock-model")

    assert exc_info.value.code == "message_too_large"


def test_protected_system_and_latest_turn_must_fit_together(monkeypatch):
    from app.services import chat_service as module

    monkeypatch.setattr(module, "_estimate_tokens", lambda text, _model: len(text))
    messages = [
        {"role": "system", "content": "s" * 600},
        {"role": "user", "content": "current"},
    ]
    cfg = SimpleNamespace(max_context_tokens=1_000, max_tokens=100)

    with pytest.raises(PromptAdmissionError) as exc_info:
        _admit_and_trim_history(messages, cfg, model_name="mock-model")

    assert exc_info.value.code == "prompt_too_large"


def test_multimodal_latest_turn_reserves_tokens_per_image():
    messages = [
        {"role": "system", "content": "system"},
        {
            "role": "user",
            "content": [
                {"type": "text", "text": "describe these"},
                *[
                    {"type": "image_url", "image_url": {"url": "data:image/png;base64,eA=="}}
                    for _ in range(3)
                ],
            ],
        },
    ]
    cfg = SimpleNamespace(max_context_tokens=3_000, max_tokens=500)

    with pytest.raises(PromptAdmissionError) as exc_info:
        _admit_and_trim_history(messages, cfg, model_name="mock-model")

    assert exc_info.value.code == "message_too_large"


def test_final_admission_uses_final_tool_route(monkeypatch):
    from app.services import chat_service as module
    from app.services.chat_service import _finalize_prompt_messages

    seen = {}

    def estimate(_cfg, *, enable_tools, route, model_name):
        seen.update(enable_tools=enable_tools, route=route, model_name=model_name)
        return 700

    monkeypatch.setattr(module, "_estimate_available_tool_schema_tokens", estimate)
    cfg = SimpleNamespace(
        max_context_tokens=1_000,
        max_tokens=100,
        model_name="mock-model",
    )
    route = object()

    with pytest.raises(PromptAdmissionError) as exc_info:
        _finalize_prompt_messages(
            [{"role": "system", "content": "s"}, {"role": "user", "content": "u"}],
            cfg,
            enable_tools=True,
            route=route,
            image_parts=[],
        )

    assert exc_info.value.code == "invalid_prompt_budget"
    assert seen == {"enable_tools": True, "route": route, "model_name": "mock-model"}
