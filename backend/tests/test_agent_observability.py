"""Task 9: 引擎灰度与重试的指标可见性。

指标只在 metric recorder 捕获层断言（不依赖 Prometheus exporter）：
  * 引擎真正接管时发 ``agent.engine.profile``（profile 维度可见，灰度面可量化）
  * 名单/开关未放行时不发（避免指标噪音）
  * 引擎 transient 重试发 ``workflow.step.retry``（重试率可观测）
"""
from __future__ import annotations

import pytest

from app.agents.orchestrator import ChatOrchestrator
from app.agents.workflow.engine import WorkflowEngine
from app.agents.workflow.schemas import Plan, RetryPolicy, Step, StepError
from app.core.config import get_settings
from app.observability import set_metric_recorder


@pytest.fixture()
def metric_recorder():
    rec: list[dict] = []
    set_metric_recorder(rec)
    yield rec
    set_metric_recorder(None)


def _selection(*, multi: bool = True, profile: str = "deep_research"):
    from app.agents.orchestrator import RuntimeSelection

    return RuntimeSelection(
        requested_runtime="crewai",
        selected_runtime="crewai",
        available=True,
        fallback_reason=None,
        multi_agent_requested=multi,
        multi_agent_executed=multi,
        agent_profile=profile,
        requested_mode="expert",
        effective_mode="expert",
        is_demo=False,
    )


def _patch_settings(monkeypatch, *, engine: str, profiles: str) -> None:
    s = get_settings()
    monkeypatch.setattr(s, "AGENT_WORKFLOW_ENGINE", engine, raising=False)
    monkeypatch.setattr(
        s, "AGENT_WORKFLOW_ENGINE_PROFILES", profiles, raising=False
    )


def test_engine_takeover_emits_profile_counter(monkeypatch, metric_recorder):
    _patch_settings(monkeypatch, engine="1", profiles="deep_research")
    routed = ChatOrchestrator()._should_route_to_engine(_selection())

    assert routed is True
    entries = [
        r for r in metric_recorder
        if r["kind"] == "counter" and r["name"] == "agent.engine.profile"
    ]
    assert entries, f"expected agent.engine.profile entry, got {metric_recorder}"
    assert entries[0]["attributes"].get("profile") == "deep_research"


def test_engine_not_routed_emits_no_profile_counter(monkeypatch, metric_recorder):
    # 名单外的 profile 不接管 → 不发接管指标。
    _patch_settings(monkeypatch, engine="1", profiles="deep_research")
    assert ChatOrchestrator()._should_route_to_engine(
        _selection(profile="debate")
    ) is False
    # 总开关关 → 不接管，同样不发。
    _patch_settings(monkeypatch, engine="", profiles="deep_research")
    assert ChatOrchestrator()._should_route_to_engine(_selection()) is False

    entries = [
        r for r in metric_recorder
        if r["kind"] == "counter" and r["name"] == "agent.engine.profile"
    ]
    assert not entries, f"接管指标不应出现，got {entries}"


async def test_transient_retry_emits_workflow_step_retry_counter(metric_recorder):
    class FlakyOnce:
        def __init__(self) -> None:
            self.n = 0

        async def execute(self, step, upstream):
            self.n += 1
            if self.n == 1:
                raise StepError("connection reset", transient=True)
            from app.agents.workflow.schemas import StepObservation

            return StepObservation(step_id=step.id, output="ok")

    plan = Plan(
        version=1,
        goal="g",
        steps=[Step(id="solo", retry_policy=RetryPolicy(max_retries=1))],
    )
    engine = WorkflowEngine(executor=FlakyOnce())
    result = await engine.run(plan)

    assert result.status == "completed"
    entries = [
        r for r in metric_recorder
        if r["kind"] == "counter" and r["name"] == "workflow.step.retry"
    ]
    assert entries, f"expected workflow.step.retry entry, got {metric_recorder}"
    assert entries[0]["attributes"].get("attempt") == 2
    assert entries[0]["attributes"].get("step") == "solo"
