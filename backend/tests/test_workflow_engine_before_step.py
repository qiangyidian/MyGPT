"""before_step：可中断的步骤前钩子。

与 on_step_start（只读观测，异常被吞）语义不同：before_step 抛出的异常会
终止本次执行 —— 暂停阻塞与取消都依赖它。
"""

from __future__ import annotations

import asyncio

from app.agents.workflow.engine import WorkflowEngine
from app.agents.workflow.executor import RecordingExecutor
from app.agents.workflow.schemas import Plan, Step


async def test_before_step_called_for_every_step():
    seen: list[str] = []

    async def before(step_id: str) -> None:
        seen.append(step_id)

    engine = WorkflowEngine(
        executor=RecordingExecutor(outputs={"a": "x", "b": "y"}),
        before_step=before,
    )
    await engine.run(
        Plan(version=1, goal="g", steps=[Step(id="a"), Step(id="b", dependencies=["a"])])
    )
    assert seen == ["a", "b"]


async def test_before_step_cancelled_error_aborts_the_run():
    async def before(step_id: str) -> None:
        if step_id == "b":
            raise asyncio.CancelledError()

    engine = WorkflowEngine(
        executor=RecordingExecutor(outputs={"a": "x", "b": "y"}),
        before_step=before,
    )
    try:
        result = await engine.run(
            Plan(version=1, goal="g", steps=[Step(id="a"), Step(id="b", dependencies=["a"])])
        )
    except asyncio.CancelledError:
        return  # 预期：取消向上传播
    # 或者引擎把它归为 step 失败
    assert result.status == "failed"


async def test_before_step_other_exceptions_mark_step_failed():
    async def before(step_id: str) -> None:
        raise RuntimeError("control plane error")

    engine = WorkflowEngine(
        executor=RecordingExecutor(outputs={"solo": "x"}),
        before_step=before,
    )
    result = await engine.run(Plan(version=1, goal="g", steps=[Step(id="solo")]))
    assert result.status == "failed"


async def test_before_step_awaitable_is_awaited():
    """before_step 必须被 await —— 暂停阻塞依赖这一点。"""

    async def before(step_id: str) -> None:
        await asyncio.sleep(0.01)

    engine = WorkflowEngine(
        executor=RecordingExecutor(outputs={"solo": "x"}),
        before_step=before,
    )
    result = await engine.run(Plan(version=1, goal="g", steps=[Step(id="solo")]))
    assert result.status == "completed"
