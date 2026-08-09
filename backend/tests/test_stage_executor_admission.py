from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.agents.runtime.stage_executor import CrewAIStageExecutor
from app.agents.stage_context import make_stage_context
from app.agents.token_budget import PromptAdmissionError


class RecordingAgent:
    tools = []

    def __init__(self):
        self.contexts = []

    async def aexecute_task(self, task, context=None):
        self.contexts.append(context)
        return SimpleNamespace(raw="ok")


def _stage_context(*, context_window: int = 2_000, max_tokens: int = 200):
    stage_ctx = make_stage_context("stage-admission")
    stage_ctx.model_config = SimpleNamespace(
        max_context_tokens=context_window,
        max_tokens=max_tokens,
        model_name="mock-model",
    )
    return stage_ctx


async def test_stage_executor_accepts_bounded_context():
    agent = RecordingAgent()
    task = SimpleNamespace(id="t1", description="analyze evidence")

    result = await CrewAIStageExecutor().execute(
        agent_id="analyst",
        agent=agent,
        task=task,
        context="bounded dependency context",
        stage_ctx=_stage_context(),
    )

    assert result.raw == "ok"
    assert agent.contexts == ["bounded dependency context"]


async def test_stage_executor_compacts_oversized_dependency_context_before_dispatch():
    agent = RecordingAgent()
    task = SimpleNamespace(id="t1", description="analyze evidence")
    original = "x" * 20_000

    await CrewAIStageExecutor().execute(
        agent_id="analyst",
        agent=agent,
        task=task,
        context=original,
        stage_ctx=_stage_context(),
    )

    assert len(agent.contexts) == 1
    assert len(agent.contexts[0]) < len(original)
    assert "truncated" in agent.contexts[0].lower()


async def test_stage_executor_rejects_oversized_task_before_dispatch():
    agent = RecordingAgent()
    task = SimpleNamespace(id="t1", description="x" * 5_000)

    with pytest.raises(PromptAdmissionError) as exc_info:
        await CrewAIStageExecutor().execute(
            agent_id="analyst",
            agent=agent,
            task=task,
            context=None,
            stage_ctx=_stage_context(context_window=1_000, max_tokens=200),
        )

    assert exc_info.value.code == "message_too_large"
    assert agent.contexts == []
