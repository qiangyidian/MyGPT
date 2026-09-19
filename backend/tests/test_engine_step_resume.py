"""B16c：引擎的步骤级 resume（跨进程崩溃后不重跑已完成步骤）。

覆盖三件事：
  ① 检查点读写闭环 —— 落库的产出能被原样读回。
  ② ``apply_checkpoints`` 的复用判据 —— 依赖不齐的步骤绝不复用。
  ③ 引擎端到端：第一轮崩溃后第二轮只跑没完成的步骤，面板节点状态与终账用量
     都要把复用的那步算进去（它的 token 真实花过，但没结算过）。
"""
from __future__ import annotations

import asyncio
import uuid


from app.agents.workflow.checkpoints import (
    CHECKPOINT_STEP_TYPE,
    StepCheckpointStore,
    apply_checkpoints,
)
from app.agents.workflow.engine import WorkflowEngine
from app.agents.workflow.schemas import (
    Plan,
    Step,
    StepObservation,
    VerificationVerdict,
    VerifierResult,
)
from app.agents.workflow.verifier import ScriptedVerifier
from app.models import AgentRun, AgentStep
from tests.test_engine_routing import _SEEDED_USER


def _plan(*ids: str, deps: dict[str, list[str]] | None = None) -> Plan:
    deps = deps or {}
    return Plan(
        goal="resume",
        profile="deep_research",
        steps=[Step(id=i, dependencies=list(deps.get(i, []))) for i in ids],
    )


async def _create_run_row(db_session) -> AgentRun:
    run = AgentRun(
        id=uuid.uuid4(),
        user_id=_SEEDED_USER,
        conversation_id=uuid.uuid4(),
        status="running",
        flow_name="native_chat",
    )
    db_session.add(run)
    await db_session.commit()
    return run


# --------------------------------------------------------------------------- #
# ① 读写闭环
# --------------------------------------------------------------------------- #
async def test_checkpoint_roundtrips_output_and_usage(db_session):
    run = await _create_run_row(db_session)
    store = StepCheckpointStore(db_session)
    await store.save(
        run.id,
        StepObservation(
            step_id="researcher",
            output="证据若干",
            usage={"prompt_tokens": 10, "completion_tokens": 3},
            attempts=2,
        ),
    )
    await db_session.commit()

    loaded = await StepCheckpointStore(db_session).load(run.id)
    obs = loaded["researcher"]
    assert obs.output == "证据若干"
    assert obs.attempts == 2
    assert obs.usage == {"prompt_tokens": 10, "completion_tokens": 3}
    # 检查点写在 step_type="plan" 上，不与网关写的 llm/tool 步骤混淆。
    assert obs.step_id == "researcher"


async def test_empty_and_oversized_outputs_are_not_checkpointed(db_session):
    """空产出没有复用价值；超长产出更像异常回显，宁缺毋滥。"""
    run = await _create_run_row(db_session)
    store = StepCheckpointStore(db_session)
    assert await store.save(run.id, StepObservation(step_id="a", output="")) is None
    assert (
        await store.save(run.id, StepObservation(step_id="b", output="x" * 300_000))
        is None
    )
    await db_session.commit()
    assert await StepCheckpointStore(db_session).load(run.id) == {}


async def test_checkpoints_are_scoped_to_their_run(db_session):
    """StaticPool 下所有用例共用一条连接，跨 run 泄漏必须靠 run_id 过滤挡住。"""
    run_a = await _create_run_row(db_session)
    run_b = await _create_run_row(db_session)
    await StepCheckpointStore(db_session).save(
        run_a.id, StepObservation(step_id="researcher", output="A 的产出")
    )
    await db_session.commit()

    loaded = await StepCheckpointStore(db_session).load(run_b.id)
    assert loaded == {}, loaded

    cleared = await StepCheckpointStore(db_session).clear(run_a.id)
    await db_session.commit()
    assert cleared >= 1
    assert await StepCheckpointStore(db_session).load(run_a.id) == {}


# --------------------------------------------------------------------------- #
# ② 复用判据
# --------------------------------------------------------------------------- #
def test_apply_checkpoints_reuses_only_dependency_complete_steps():
    """链上第一步没有检查点 → 整条链都不能复用（下游会带着半截上下文假完成）。"""
    plan = _plan("a", "b", "c", deps={"b": ["a"], "c": ["b"]})
    reused = apply_checkpoints(
        plan,
        {"b": StepObservation(step_id="b", output="B"), "c": StepObservation(step_id="c", output="C")},
    )
    assert reused == []
    assert [s.skip for s in plan.steps] == [False, False, False]
    assert plan.carry_observations == {}


def test_apply_checkpoints_walks_the_chain_top_down():
    plan = _plan("a", "b", "c", deps={"b": ["a"], "c": ["b"]})
    reused = apply_checkpoints(
        plan,
        {
            "b": StepObservation(step_id="b", output="B"),
            "a": StepObservation(step_id="a", output="A"),
            "c": StepObservation(step_id="c", output="C"),
        },
    )
    assert reused == ["a", "b", "c"]
    assert all(s.skip for s in plan.steps)
    assert plan.carry_observations["c"].output == "C"


def test_apply_checkpoints_stops_at_the_first_missing_link():
    """a 有、b 没有、c 有 → 只复用 a；c 的上游缺失，不能跳过 b 直接用它。"""
    plan = _plan("a", "b", "c", deps={"b": ["a"], "c": ["b"]})
    reused = apply_checkpoints(
        plan,
        {"a": StepObservation(step_id="a", output="A"), "c": StepObservation(step_id="c", output="C")},
    )
    assert reused == ["a"]
    assert {s.id: s.skip for s in plan.steps} == {"a": True, "b": False, "c": False}


def test_apply_checkpoints_ignores_ids_not_in_this_plan():
    """换了拓扑后多出来的步骤没有检查点；多余的检查点 id 也不该污染计划。"""
    plan = _plan("x")
    reused = apply_checkpoints(plan, {"ghost": StepObservation(step_id="ghost", output="?")})
    assert reused == []
    assert plan.carry_observations == {}


def test_apply_checkpoints_with_no_checkpoints_is_a_noop():
    plan = _plan("a")
    assert apply_checkpoints(plan, {}) == []
    assert plan.steps[0].skip is False


# --------------------------------------------------------------------------- #
# ③ 引擎端到端
# --------------------------------------------------------------------------- #
async def test_engine_writes_a_checkpoint_per_completed_step(db_session):
    """每成功一步就地落一条检查点 —— 崩溃只能停在「已写完的那步之后」。"""
    run = await _create_run_row(db_session)
    factory = _session_factory(db_session)
    runs: list[str] = []

    class _Exec:
        async def execute(self, step, upstream):
            runs.append(step.id)
            await asyncio.sleep(0)
            if step.id == "writer":
                raise RuntimeError("进程在这一步炸了")
            return StepObservation(step_id=step.id, output=f"out:{step.id}")

    engine = WorkflowEngine(
        executor=_Exec(),
        verifier=ScriptedVerifier([VerifierResult(verdict=VerificationVerdict.fail)]),
        run_id=run.id,
        session_factory=factory,
    )
    result = await engine.run(
        _plan("researcher", "writer", deps={"writer": ["researcher"]})
    )
    assert result.status == "failed"
    # 失败路径**不清**检查点：下一轮正是靠它跳过 researcher。
    loaded = await StepCheckpointStore(await _open(factory)).load(run.id)
    assert set(loaded) == {"researcher"}, loaded
    assert loaded["researcher"].output == "out:researcher"


async def test_successful_run_clears_its_checkpoints(db_session):
    """整轮成功后复用价值消失，留在审计表里只会长期占空间。"""
    run = await _create_run_row(db_session)
    factory = _session_factory(db_session)

    class _Exec:
        async def execute(self, step, upstream):
            await asyncio.sleep(0)
            return StepObservation(step_id=step.id, output=f"out:{step.id}")

    engine = WorkflowEngine(
        executor=_Exec(),
        verifier=ScriptedVerifier([
            VerifierResult(verdict=VerificationVerdict.pass_),
        ]),
        run_id=run.id,
        session_factory=factory,
    )
    result = await engine.run(_plan("researcher", "writer", deps={"writer": ["researcher"]}))
    assert result.status == "completed"
    await _assert_step_rows(db_session, run.id, expected=0)


async def test_second_engine_run_reuses_completed_steps(db_session):
    """第一轮跑完 researcher 后进程死掉；第二轮必须不重跑它。"""
    run = await _create_run_row(db_session)
    factory = _session_factory(db_session)

    async with factory() as sess:
        await StepCheckpointStore(sess).save(
            run.id,
            StepObservation(
                step_id="researcher",
                output="已检索到的证据",
                usage={"prompt_tokens": 100, "completion_tokens": 20, "cost_usd": 0.01},
            ),
        )
        await sess.commit()

    runs: list[str] = []

    class _Exec:
        async def execute(self, step, upstream):
            runs.append(step.id)
            # 下游必须能看到复用步的产出，否则 resume 只是省钱、结果是坏的。
            assert "researcher" in upstream
            assert upstream["researcher"].output == "已检索到的证据"
            await asyncio.sleep(0)
            return StepObservation(step_id=step.id, output=f"out:{step.id}")

    seen: dict[str, tuple] = {}
    engine = WorkflowEngine(
        executor=_Exec(),
        verifier=ScriptedVerifier([
            VerifierResult(verdict=VerificationVerdict.pass_),
        ]),
        run_id=run.id,
        session_factory=factory,
        on_step_start=lambda sid: seen.setdefault("started", []).append(sid),
        on_step_end=lambda sid, output, usage: seen.setdefault(sid, (output, usage)),
    )
    plan = _plan("researcher", "writer", deps={"writer": ["researcher"]})
    result = await engine.run(plan)

    assert runs == ["writer"], runs
    assert result.reused_steps == ["researcher"]
    # 复用步的节点状态补发：面板上不能显示成「没跑」。
    assert "researcher" in seen["started"], seen
    output, usage = seen["researcher"]
    assert output == "已检索到的证据"
    assert usage == {"prompt_tokens": 100, "completion_tokens": 20, "cost_usd": 0.01}
    # 终账用量含复用步 —— 它的 token 真实发生过、上一轮却没结算。
    assert result.observations["researcher"].usage["prompt_tokens"] == 100


async def test_resume_is_silent_on_a_first_attempt(db_session):
    """没有检查点时行为与不接这个特性完全一致（首轮零副作用）。"""
    run = await _create_run_row(db_session)
    runs: list[str] = []

    class _Exec:
        async def execute(self, step, upstream):
            runs.append(step.id)
            await asyncio.sleep(0)
            return StepObservation(step_id=step.id, output=step.id)

    engine = WorkflowEngine(
        executor=_Exec(),
        verifier=ScriptedVerifier([
            VerifierResult(verdict=VerificationVerdict.pass_),
        ]),
        run_id=run.id,
        session_factory=_session_factory(db_session),
    )
    result = await engine.run(_plan("a", "b", deps={"b": ["a"]}))
    assert result.status == "completed"
    assert result.reused_steps == []
    assert runs == ["a", "b"]


async def test_checkpoint_store_failure_does_not_break_the_run(db_session):
    """检查点是优化，不是执行前提：读抛异常也要照常把整轮跑完。"""
    run = await _create_run_row(db_session)
    runs: list[str] = []

    class _Exec:
        async def execute(self, step, upstream):
            runs.append(step.id)
            await asyncio.sleep(0)
            return StepObservation(step_id=step.id, output=step.id)

    class _Boom:
        def __call__(self):
            raise RuntimeError("db down")

    engine = WorkflowEngine(
        executor=_Exec(),
        verifier=ScriptedVerifier([
            VerifierResult(verdict=VerificationVerdict.pass_),
        ]),
        run_id=run.id,
        session_factory=_Boom(),
    )
    result = await engine.run(_plan("a"))
    assert result.status == "completed"
    assert runs == ["a"]


# --------------------------------------------------------------------------- #
# helpers
def _session_factory(db_session):
    """把用例的共享连接包成「async with factory()」形态。"""

    class _Factory:
        def __call__(self):
            return _Session(db_session)

    return _Factory()


class _Session:
    """在共享连接上开一个不做 commit/rollback 的伪会话。

    SQLite StaticPool 只有一条连接，真开第二个会话会互踩；这里让 commit 成为
    flush，检查点仍落在同一条连接上（断言时按 run_id 过滤）。
    """

    def __init__(self, session) -> None:
        self._session = session

    async def __aenter__(self):
        return self._session

    async def __aexit__(self, *exc):
        return False


async def _open(factory):
    return await factory().__aenter__()


async def _assert_step_rows(db_session, run_id, *, expected: int) -> None:
    from sqlalchemy import func, select

    count = (
        await db_session.execute(
            select(func.count())
            .select_from(AgentStep)
            .where(
                AgentStep.run_id == run_id,
                AgentStep.step_type == CHECKPOINT_STEP_TYPE,
            )
        )
    ).scalar_one()
    assert count == expected, count
