"""WorkflowEngine 的只读步骤回调。

回调用于把引擎的步骤生命周期接到 RunEnvironment 上（发 agent_status /
step_output / step_progress）。核心保证：**观测不得影响执行**。
"""
from __future__ import annotations

from app.agents.workflow.engine import WorkflowEngine
from app.agents.workflow.executor import RecordingExecutor
from app.agents.workflow.planner import build_deep_research_plan
from app.agents.workflow.schemas import Plan, Step, StepError


async def test_callbacks_receive_step_ids_in_order():
    started: list[str] = []
    ended: list[tuple[str, str]] = []

    async def on_start(step_id: str) -> None:
        started.append(step_id)

    async def on_end(step_id: str, output: str | None, usage: dict | None) -> None:
        ended.append((step_id, output or ""))

    engine = WorkflowEngine(
        executor=RecordingExecutor(
            outputs={"researcher": "R", "analyst": "A", "writer": "W"}
        ),
        on_step_start=on_start,
        on_step_end=on_end,
    )
    result = await engine.run(build_deep_research_plan("q"))

    assert result.status == "completed"
    assert started == ["researcher", "analyst", "writer"]
    assert [sid for sid, _ in ended] == ["researcher", "analyst", "writer"]
    assert ended[0][1] == "R"


async def test_error_callback_fires_on_permanent_failure():
    errors: list[tuple[str, str]] = []

    async def on_error(step_id: str, message: str) -> None:
        errors.append((step_id, message))

    class Boom:
        async def execute(self, step: Step, upstream):
            raise StepError("nope", transient=False)

    engine = WorkflowEngine(executor=Boom(), on_step_error=on_error)
    result = await engine.run(
        Plan(version=1, goal="g", steps=[Step(id="solo", task_description="t")])
    )

    assert result.status == "failed"
    assert errors == [("solo", "nope")]


async def test_sync_callbacks_are_supported():
    seen: list[str] = []

    engine = WorkflowEngine(
        executor=RecordingExecutor(outputs={"solo": "x"}),
        on_step_start=lambda step_id: seen.append(step_id),
    )
    await engine.run(Plan(version=1, goal="g", steps=[Step(id="solo")]))
    assert seen == ["solo"]


async def test_callback_exceptions_never_break_the_run():
    """观测绝不 veto 执行 —— 与引擎 best-effort 持久化策略一致。"""

    def exploding(step_id: str) -> None:
        raise RuntimeError("observer bug")

    async def exploding_end(step_id: str, output: str | None, usage: dict | None) -> None:
        raise RuntimeError("observer bug")

    engine = WorkflowEngine(
        executor=RecordingExecutor(outputs={"solo": "x"}),
        on_step_start=exploding,
        on_step_end=exploding_end,
    )
    result = await engine.run(Plan(version=1, goal="g", steps=[Step(id="solo")]))
    assert result.status == "completed"
    assert result.observations["solo"].output == "x"
