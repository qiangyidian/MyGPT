"""WorkflowEngine 的只读步骤回调。

回调用于把引擎的步骤生命周期接到 RunEnvironment 上（发 agent_status /
step_output / step_progress）。核心保证：**观测不得影响执行**。
"""
from __future__ import annotations

from app.agents.workflow.engine import WorkflowEngine
from app.agents.workflow.executor import RecordingExecutor
from app.agents.workflow.planner import build_deep_research_plan
from app.agents.workflow.schemas import Plan, RetryPolicy, Step, StepError


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


async def test_retry_callback_fires_on_transient_failure():
    """transient 失败重试时，on_step_retry 必须被调用（观测通道）。"""
    retries: list[tuple[str, int, str]] = []
    calls: list[str] = []

    async def on_retry(step_id: str, attempt: int, error: str) -> None:
        retries.append((step_id, attempt, error))

    class FlakyOnce:
        """首次抛 transient，之后成功。"""

        def __init__(self) -> None:
            self.n = 0

        async def execute(self, step, upstream):
            self.n += 1
            calls.append(step.id)
            if self.n == 1:
                raise StepError("connection reset", transient=True)
            from app.agents.workflow.schemas import StepObservation

            return StepObservation(step_id=step.id, output="ok")

    plan = Plan(
        version=1,
        goal="g",
        steps=[Step(id="solo", retry_policy=RetryPolicy(max_retries=1))],
    )
    engine = WorkflowEngine(executor=FlakyOnce(), on_step_retry=on_retry)
    result = await engine.run(plan)

    assert result.status == "completed"
    assert calls == ["solo", "solo"], "应当重试一次"
    assert len(retries) == 1
    assert retries[0][0] == "solo"
    assert retries[0][1] == 2  # 即将进行的第 2 次尝试
    assert "connection reset" in retries[0][2]


async def test_retry_callback_not_fired_on_permanent_failure():
    retries: list[tuple[str, int, str]] = []

    class AlwaysFails:
        async def execute(self, step, upstream):
            raise StepError("nope", transient=False)

    engine = WorkflowEngine(
        executor=AlwaysFails(),
        on_step_retry=lambda sid, att, err: retries.append((sid, att, err)),
    )
    result = await engine.run(Plan(version=1, goal="g", steps=[Step(id="solo")]))

    assert result.status == "failed"
    assert retries == [], "永久失败不应触发重试回调"


async def test_retry_callback_exception_never_breaks_the_run():
    class FlakyOnce:
        def __init__(self) -> None:
            self.n = 0

        async def execute(self, step, upstream):
            self.n += 1
            if self.n == 1:
                raise StepError("timeout", transient=True)
            from app.agents.workflow.schemas import StepObservation

            return StepObservation(step_id=step.id, output="ok")

    def exploding(sid, att, err) -> None:
        raise RuntimeError("observer bug")

    engine = WorkflowEngine(executor=FlakyOnce(), on_step_retry=exploding)
    result = await engine.run(
        Plan(
            version=1,
            goal="g",
            steps=[Step(id="solo", retry_policy=RetryPolicy(max_retries=1))],
        )
    )
    assert result.status == "completed"
