"""引擎路径（WorkflowEngine）的执行正确性回归：工具 / 上游 / 超时 / 重试 / 取消。

对应验收文档 §11（``docs/superpowers/specs/2026-09-18-agent-engine-takeover-design.md``）
里「两条路径必须等价」的五条硬指标。用例分层原则：

  * 调度语义（上游可见集、挂钟超时）在引擎单元层测 —— 用注入的假执行器，
    不需要 LLM，也不受模板 plan 变化影响。
  * 接线语义（工具构建、重试事件、取消收尾）必须过
    ``ChatOrchestrator.stream``，因为它们坏的正是「引擎 ↔ RunEnvironment ↔
    CrewAI」之间的那几根线。

SQLite 是 StaticPool 单连接、进程内共享的，所以所有落库断言都按本次
``ctx.run_id`` 过滤。
"""
from __future__ import annotations

import asyncio
import time
import uuid
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from app.agents.orchestrator import ChatOrchestrator
from app.agents.run_environment import RunEnvironment
from app.agents.runtime.stage_executor import CrewAIStageExecutor
from app.agents.stage_context import make_stage_context
from app.agents.workflow import engine as engine_module
from app.agents.workflow.engine import WorkflowEngine, _dependency_view
from app.agents.workflow.executor import StageAdapterExecutor
from app.agents.workflow.schemas import (
    Plan,
    RetryPolicy,
    Step,
    StepError,
    StepObservation,
)
from app.agents.workflow.verifier import ScriptedVerifier
from app.models import AgentRun
from tests.test_engine_routing import (
    _FakeWorkflowExecutor,
    _patch_flag,
    _seed_ctx,
    _spy_crewai,
)


# --------------------------------------------------------------------------- #
# ① 引擎路径的 Crew 必须有工具，且与 walker 同一来源
# --------------------------------------------------------------------------- #
def _tool_names(tools) -> set[str]:
    return {getattr(t, "name", "") for t in (tools or [])}


async def test_engine_stages_get_the_same_tools_as_the_walker(db_session):
    """引擎构造的 stage 的 ``agent.tools`` 必须与 walker **逐 stage 相同**，且检索
    角色真的拿到工具。

    旧实现是 ``builder(llm=llm, tools=[], ...)``，而 crew builder 内部写的是
    ``tools=tools or None`` —— 名单内 profile 一上引擎就静默地不检索、不调
    工具（工具面板全空、回答没有来源）。

    注意不能要求「每个 stage 都有工具」：模板里只有 researcher 挂工具，
    analyst/writer 在 walker 上同样没有 —— 钉的是「两边同源」，加上
    「researcher 必须非空」来防止再次整体退化成无工具。
    """
    from app.agents.adapters.llm_adapter import CrewAILLMFactory
    from app.agents.crews import build_research_stages
    from app.agents.runtime.crewai_runtime import build_runtime_tools

    ctx = await _seed_ctx(db_session)
    orch = ChatOrchestrator()
    run = await orch._create_run(ctx)
    env = RunEnvironment.for_call(ctx)

    adapter = await orch._build_stage_adapter(ctx, run, env)
    engine_tool_sets = {
        sid: _tool_names(getattr(spec.agent, "tools", None))
        for sid, spec in adapter._stages.items()
    }

    # walker 的同一份工具构建 + 同一个 crew builder，逐 stage 对得上。
    llm = CrewAILLMFactory.from_model_config(ctx.model_config, budget_guard=env.guard)
    tools = await build_runtime_tools(ctx, stage_ctx=env.stage_ctx)
    assert _tool_names(tools), "runtime 工具集为空时这条用例失去判别力"
    _, walker_stages = build_research_stages(
        llm=llm, tools=tools, question=ctx.user_content or ""
    )
    walker_tool_sets = {
        spec.agent_id: _tool_names(getattr(spec.agent, "tools", None))
        for spec in walker_stages
    }

    assert engine_tool_sets == walker_tool_sets
    assert engine_tool_sets["researcher"] == _tool_names(tools)


async def test_engine_tools_are_run_scoped_and_share_the_env_stage_ctx(db_session):
    """工具必须带本次 run 的身份，且读 RunEnvironment 共享的那一份 stage_ctx。

    ``bind_tool_context`` 的调用者身份收口在 ``build_crewai_tool`` →
    ``ToolGateway`` 这条链上；引擎若另起炉灶建工具，审计行会挂错 run、
    工具事件也会归属到空的 agent。
    """
    ctx = await _seed_ctx(db_session)
    orch = ChatOrchestrator()
    run = await orch._create_run(ctx)
    env = RunEnvironment.for_call(ctx)

    adapter = await orch._build_stage_adapter(ctx, run, env)
    spec = adapter._stages["researcher"]
    tools = list(getattr(spec.agent, "tools", None) or [])
    assert tools
    for tool in tools:
        # per-run 归属：适配器构造期就把 run_id 焊进了工具（PrivateAttr）。
        assert tool._run_id == run.id
        # 共享 stage_ctx：工具事件的 agent 归属来自 env 的那一份，不是新建一份。
        assert adapter._stage_ctx is env.stage_ctx


# --------------------------------------------------------------------------- #
# ② 上游上下文按依赖集过滤（并行独立性必须是真的）
# --------------------------------------------------------------------------- #
class _UpstreamSpyExecutor:
    """记录每一步实际看到的 upstream 键集。"""

    def __init__(self, outputs: dict[str, str] | None = None) -> None:
        self.outputs = outputs or {}
        self.seen: dict[str, list[str]] = {}
        self.calls: list[str] = []

    async def execute(
        self, step: Step, upstream: dict[str, StepObservation]
    ) -> StepObservation:
        self.calls.append(step.id)
        self.seen[step.id] = sorted(upstream)
        await asyncio.sleep(0)  # 让并行兄弟真正交叉执行
        return StepObservation(
            step_id=step.id, output=self.outputs.get(step.id, f"[{step.id}]")
        )


def _fanout_plan() -> Plan:
    """root -> (a ∥ b) -> join：a、b 互不相干，join 汇合两者。"""
    return Plan(
        version=1,
        goal="g",
        steps=[
            Step(id="root"),
            Step(id="a", dependencies=["root"]),
            Step(id="b", dependencies=["root"]),
            Step(id="join", dependencies=["a", "b"]),
        ],
    )


async def test_parallel_siblings_do_not_see_each_other():
    ex = _UpstreamSpyExecutor()
    result = await WorkflowEngine(executor=ex, verifier=ScriptedVerifier(["pass"])).run(
        _fanout_plan()
    )
    assert result.status == "completed"
    # 旧实现把「全部累计观测」传给每一步：并行兄弟一旦互相看到对方产出，并行
    # 独立性就是假的（debate 的两个 advocate、task_decomposition 的 worker），
    # prompt 也随步数线性膨胀。
    assert ex.seen["a"] == ["root"], ex.seen
    assert ex.seen["join"] == ["a", "b"], ex.seen  # 真 join 不被削弱
    assert ex.seen["root"] == [], ex.seen


async def test_sibling_that_finished_first_stays_out_of_later_upstream():
    """确定性判别式：a 的观测**已经**记进 observations 时，b 仍不得看到 a。

    上一条依赖调度交叉的运气，这条用 ``on_step_end`` + ``before_step`` 卡住
    时序：b 计算 upstream 时 a 必然已完成。旧实现（传全量累计观测）在这里
    必然失败。
    """
    ex = _UpstreamSpyExecutor()
    a_settled = asyncio.Event()

    async def on_end(step_id: str, output: str, usage) -> None:
        if step_id == "a":
            a_settled.set()

    async def before(step_id: str) -> None:
        if step_id == "b":
            await a_settled.wait()

    engine = WorkflowEngine(
        executor=ex,
        verifier=ScriptedVerifier(["pass"]),
        on_step_end=on_end,
        before_step=before,
    )
    result = await engine.run(_fanout_plan())
    assert result.status == "completed"
    assert "a" in ex.seen["join"], "join 仍要汇合两边"
    assert ex.seen["b"] == ["root"], f"b 看到了非依赖步 {ex.seen['b']}"


def test_dependency_view_is_the_filter():
    """``_dependency_view``：只给声明过的依赖，按声明序，缺观测就跳过。"""
    obs = {
        "a": StepObservation(step_id="a", output="A"),
        "b": StepObservation(step_id="b", output="B"),
    }
    assert _dependency_view(Step(id="c"), obs) == {}
    assert _dependency_view(Step(id="c", dependencies=["b", "a"]), obs) == {
        "b": obs["b"],
        "a": obs["a"],
    }
    assert list(_dependency_view(Step(id="c", dependencies=["b", "a"]), obs)) == [
        "b",
        "a",
    ]
    # 依赖还没产出（被撤的 skip 步）不会炸。
    assert _dependency_view(Step(id="c", dependencies=["gone", "a"]), obs) == {
        "a": obs["a"]
    }


class _NoopInner:
    """替掉 StageAdapterExecutor 内的真 CrewAI 执行器，只看拼出来的 context。"""

    def __init__(self) -> None:
        self.contexts: list[str | None] = []

    async def execute(self, **kw):
        self.contexts.append(kw["context"])

        class _R:
            raw = "ok"
            structured = None
            usage = None

        return _R()


def _adapter_with_inner():
    stage_ctx = make_stage_context(str(uuid.uuid4()))
    spec = type("Spec", (), {"agent": object(), "task": object()})()
    adapter = StageAdapterExecutor({"b": spec}, stage_ctx)
    inner = _NoopInner()
    adapter._inner = inner
    return adapter, inner, stage_ctx


async def test_stage_adapter_serializes_only_declared_upstream():
    adapter, inner, _ctx = _adapter_with_inner()
    await adapter.execute(
        Step(id="b", dependencies=["a"]),
        {"a": StepObservation(step_id="a", output="A")},
    )
    assert inner.contexts == ["[a output]\nA"]
    # 无依赖 → None（与 walker 给首个 stage 传 context=None 等价）。
    await adapter.execute(Step(id="b"), {})
    assert inner.contexts[-1] is None


async def test_stage_adapter_injects_pending_instructions_once():
    """引擎路径也必须消费「用户追加指导」（以前收进队列没人取 → 静默丢失）。"""
    adapter, inner, stage_ctx = _adapter_with_inner()
    stage_ctx.pending_instructions.append("换成中文写")

    await adapter.execute(Step(id="b"), {})
    assert "用户追加指导" in (inner.contexts[-1] or "")
    assert "换成中文写" in (inner.contexts[-1] or "")
    assert stage_ctx.pending_instructions == [], "每条指导只投一次"

    await adapter.execute(Step(id="b"), {})
    assert inner.contexts[-1] is None


# --------------------------------------------------------------------------- #
# ③ 步骤级挂钟超时
# --------------------------------------------------------------------------- #
class _HangingExecutor:
    """永不返回的步骤 —— 旧实现下整轮会一直挂着。"""

    def __init__(self) -> None:
        self.calls = 0
        self.entered = asyncio.Event()

    async def execute(
        self, step: Step, upstream: dict[str, StepObservation]
    ) -> StepObservation:
        self.calls += 1
        self.entered.set()
        await asyncio.sleep(3600)
        return StepObservation(step_id=step.id, output="never")


async def test_hanging_step_fails_within_its_timeout_seconds():
    """step.timeout_seconds 以前全仓无消费点；现在真的限制挂钟。"""
    ex = _HangingExecutor()
    plan = Plan(
        version=1,
        goal="g",
        steps=[Step(id="slow", timeout_seconds=0.05, retry_policy=RetryPolicy())],
    )
    started = time.monotonic()
    result = await WorkflowEngine(executor=ex, verifier=ScriptedVerifier(["pass"])).run(plan)
    elapsed = time.monotonic() - started

    assert elapsed < 5, f"挂死步骤没有被超时收掉（耗时 {elapsed:.1f}s）"
    assert result.status == "failed"
    assert "slow" in (result.error or "")
    assert ex.calls == 1  # max_retries=0 → 不重试


async def test_step_timeout_is_transient_and_retried():
    """超时按 transient 走重试语义（而不是把整轮拖死）。"""
    ex = _HangingExecutor()
    plan = Plan(
        version=1,
        goal="g",
        steps=[
            Step(id="slow", timeout_seconds=0.05, retry_policy=RetryPolicy(max_retries=1))
        ],
    )
    started = time.monotonic()
    result = await WorkflowEngine(executor=ex, verifier=ScriptedVerifier(["pass"])).run(plan)
    elapsed = time.monotonic() - started

    assert ex.calls == 2, f"超时应触发一次重试，calls={ex.calls}"
    assert elapsed < 5
    assert result.status == "failed"
    # 错误消息里带 timeout：即便某步的策略没声明 StepError，字符串匹配也能
    # 把它认成 transient。
    assert "timeout" in (result.error or "").lower() or "slow" in (result.error or "")


async def test_budget_remaining_caps_the_step_deadline():
    """没有 timeout_seconds 时，预算剩余秒数封顶单步挂钟。

    生产里模板 plan 的步骤**都没有** timeout_seconds（planner 从不设置），
    所以这条才是真正兜住「挂死步骤越过 AGENT_MAX_RUNTIME_SECONDS」的线。
    """
    ex = _HangingExecutor()
    engine = WorkflowEngine(
        executor=ex,
        verifier=ScriptedVerifier(["pass"]),
        budget_remaining_seconds=lambda: 0.05,
    )
    started = time.monotonic()
    result = await engine.run(Plan(version=1, goal="g", steps=[Step(id="slow")]))
    elapsed = time.monotonic() - started

    assert elapsed < 5, f"预算没有封顶步骤挂钟（耗时 {elapsed:.1f}s）"
    assert result.status == "failed"


def test_step_deadline_is_the_min_of_step_and_budget():
    e_budget30 = WorkflowEngine(
        executor=_UpstreamSpyExecutor(), budget_remaining_seconds=lambda: 30.0
    )
    assert e_budget30._step_timeout_seconds(Step(id="a", timeout_seconds=5)) == 5.0
    assert e_budget30._step_timeout_seconds(Step(id="a")) == 30.0

    e_budget3 = WorkflowEngine(
        executor=_UpstreamSpyExecutor(), budget_remaining_seconds=lambda: 3.0
    )
    assert e_budget3._step_timeout_seconds(Step(id="a", timeout_seconds=5)) == 3.0

    # 两个上限都没有 → 不限时（保持旧的「无预算取值器」语义，注入执行器的测试
    # 不会被莫名切断）。
    assert (
        WorkflowEngine(executor=_UpstreamSpyExecutor())._step_timeout_seconds(
            Step(id="a")
        )
        is None
    )
    # 非正的 timeout_seconds 视为未声明。
    assert (
        WorkflowEngine(executor=_UpstreamSpyExecutor())._step_timeout_seconds(
            Step(id="a", timeout_seconds=0)
        )
        is None
    )


async def test_budget_reader_failure_does_not_break_execution():
    """预算读数抛异常不能反过来打断执行（也不该吞掉取消）。"""
    ex = _UpstreamSpyExecutor()

    def _boom() -> float:
        raise RuntimeError("guard exploded")

    engine = WorkflowEngine(
        executor=ex, verifier=ScriptedVerifier(["pass"]), budget_remaining_seconds=_boom
    )
    result = await engine.run(Plan(version=1, goal="g", steps=[Step(id="a")]))
    assert result.status == "completed"


async def test_cancelled_error_from_budget_reader_propagates():
    """取值器抛 CancelledError 必须原样上抛，不能被当成「读数失败」。"""

    def _cancel() -> float:
        raise asyncio.CancelledError()

    engine = WorkflowEngine(
        executor=_UpstreamSpyExecutor(),
        verifier=ScriptedVerifier(["pass"]),
        budget_remaining_seconds=_cancel,
    )
    with pytest.raises(asyncio.CancelledError):
        await engine.run(Plan(version=1, goal="g", steps=[Step(id="a")]))


async def test_orchestrator_wires_timeout_and_retry_and_cancel_hooks(
    db_session, monkeypatch
):
    """orchestrator 必须把预算 / 重试 / 取消三根线接到引擎上（以前都没接）。"""
    captured: dict[str, object] = {}
    real = WorkflowEngine

    class _RecordingEngine(real):  # type: ignore[misc,valid-type]
        def __init__(self, **kw):
            captured.update(kw)
            super().__init__(**kw)

    monkeypatch.setattr(engine_module, "WorkflowEngine", _RecordingEngine)
    _patch_flag(monkeypatch, engine="1", crewai=True)
    ctx = await _seed_ctx(db_session)
    ctx.extra["workflow_executor"] = _FakeWorkflowExecutor(
        outputs={"researcher": "ev", "analyst": "f", "writer": "answer"}
    )
    _spy_crewai(monkeypatch)

    async for _ in ChatOrchestrator().stream(ctx):
        pass

    assert captured, "引擎没被构造"
    # ③ 步骤挂钟的预算上限
    budget = captured.get("budget_remaining_seconds")
    assert callable(budget)
    value = budget()
    assert isinstance(value, float) and value > 0, value
    # ④ transient 重试的观测通道
    assert captured.get("on_step_retry") is not None
    # ⑤ 取消按 cancelled 收尾
    assert captured.get("on_step_cancel") is not None


# --------------------------------------------------------------------------- #
# ④ 重试事件接线：transient 失败 → 重试中 → 成功的完整序列
# --------------------------------------------------------------------------- #
class _FlakyExecutor:
    """前 ``until-1`` 次调用某步时抛 transient 错误，之后成功。"""

    def __init__(self, *, step_id: str, until: int = 2) -> None:
        self.step_id = step_id
        self.until = until
        self.calls: dict[str, int] = {}

    async def execute(
        self, step: Step, upstream: dict[str, StepObservation]
    ) -> StepObservation:
        self.calls[step.id] = self.calls.get(step.id, 0) + 1
        if step.id == self.step_id and self.calls[step.id] < self.until:
            await asyncio.sleep(0.02)  # 让 duration_ms 可观测（非 0）
            raise StepError("connection reset by peer", transient=True)
        return StepObservation(step_id=step.id, output=f"[{step.id}] output")


async def test_retry_marks_retrying_and_completes_cleanly(db_session, monkeypatch):
    """重试必须让节点带上「第 N 次尝试」，成功后残留全清。

    旧实现没接 ``on_step_retry``，``emit_agent_retrying`` 在生产里没有调用者：
    重试期间面板上完全看不出在重试，且万一节点被置成失败就停在那里。
    """
    _patch_flag(monkeypatch, engine="1", crewai=True)
    ctx = await _seed_ctx(db_session)
    ex = _FlakyExecutor(step_id="researcher")
    ctx.extra["workflow_executor"] = ex
    _spy_crewai(monkeypatch)

    events: list[tuple[str, dict]] = []
    async for evt in ChatOrchestrator().stream(ctx):
        events.append((evt.kind, evt.data))

    kinds = [k for k, _ in events]
    assert kinds[-1] == "done", kinds
    assert ex.calls.get("researcher") == 2, ex.calls

    researcher_events = [
        d
        for k, d in events
        if k == "agent_status" and d.get("agent_id") == "researcher"
    ]
    assert researcher_events, "researcher 必须有 agent_status 事件"
    retrying = [d for d in researcher_events if d.get("retrying")]
    assert retrying, f"transient 重试必须发 retrying 标记：{researcher_events}"
    assert retrying[0]["retrying"]["attempt"] == 2, retrying[0]
    assert retrying[0]["status"] == "running", retrying[0]
    statuses = [d["status"] for d in researcher_events]
    assert statuses[-1] == "completed", statuses
    # retrying 事件必须出现在 completed 之前（面板上的顺序就是用户的感知）。
    assert researcher_events.index(retrying[0]) < len(researcher_events) - 1

    # 终态：completed、无 error 残留、无 retrying 残留、duration 非零。
    row = (
        await db_session.execute(select(AgentRun).where(AgentRun.id == ctx.run_id))
    ).scalar_one()
    await db_session.refresh(row)
    nodes = {n["id"]: n for n in row.graph_state["nodes"]}
    researcher_node = nodes["researcher"]
    assert researcher_node["status"] == "completed", researcher_node
    assert not researcher_node.get("error"), researcher_node
    assert not researcher_node.get("retrying"), researcher_node
    assert researcher_node.get("duration_ms"), f"重试成功的节点必须有耗时：{researcher_node}"
    # writer 依赖 researcher：重试成功后下游照常跑完（不是假失败）。
    assert nodes["writer"]["status"] == "completed"
    assert ctx.assistant_msg.content == "[writer] output"
    # 引擎把尝试次数带进 usage，成本核算看得见重试。
    assert (researcher_node.get("usage") or {}).get("attempts") == 2, researcher_node


async def test_permanent_failure_still_falls_back_to_crewai(db_session, monkeypatch):
    """永久失败仍然回退 CrewAI（回归护栏：③/⑤ 的改动不得削弱回退）。"""
    from app.agents.runtime.stage_executor import FakeStageExecutor

    _patch_flag(monkeypatch, engine="1", crewai=True)
    ctx = await _seed_ctx(db_session)
    ctx.extra["workflow_executor"] = _FakeWorkflowExecutor(fail_step="researcher")
    ctx.extra["stage_executor"] = FakeStageExecutor()
    crewai_calls = _spy_crewai(monkeypatch)

    kinds: list[str] = []
    async for evt in ChatOrchestrator().stream(ctx):
        kinds.append(evt.kind)

    assert crewai_calls["n"] == 1, "永久失败必须回退 CrewAI"
    assert kinds[-1] == "done", kinds


# --------------------------------------------------------------------------- #
# ⑤a 取消收尾：节点不卡 running/pending，终态快照落库，且不重跑整轮
# --------------------------------------------------------------------------- #
async def test_cooperative_cancel_settles_nodes_and_does_not_rerun_turn(
    db_session, monkeypatch
):
    """用户取消 → 整轮收成 cancelled，绝不回退 CrewAI 把整轮再跑一遍。

    旧实现里步骤重试循环的 ``except BaseException`` 把 CancelledError 当成
    「永久失败」，引擎返回 failed，orchestrator 又把它当成异常去回退 CrewAI ——
    用户点停止反而让整轮从头重跑；且取消那一步卡在 running。
    """
    _patch_flag(monkeypatch, engine="1", crewai=True)
    ctx = await _seed_ctx(db_session)
    crewai_calls = _spy_crewai(monkeypatch)

    class _CancellingExecutor(_FakeWorkflowExecutor):
        async def execute(self, step, upstream):
            obs = await super().execute(step, upstream)
            if step.id == "researcher":
                # 用户在第一步跑完之后点了「停止」。
                ctx.extra["run_control"].cancel.set()
            return obs

    ctx.extra["workflow_executor"] = _CancellingExecutor(
        outputs={"researcher": "ev", "analyst": "f", "writer": "answer"}
    )

    events: list[tuple[str, dict]] = []
    with pytest.raises(asyncio.CancelledError):
        async for evt in ChatOrchestrator().stream(ctx):
            events.append((evt.kind, evt.data))

    assert crewai_calls["n"] == 0, "取消后不得回退 CrewAI 重跑整轮"
    run_statuses = [d["status"] for k, d in events if k == "run_status"]
    assert "cancelled" in run_statuses, run_statuses

    row = (
        await db_session.execute(select(AgentRun).where(AgentRun.id == ctx.run_id))
    ).scalar_one()
    await db_session.refresh(row)
    statuses = {n["id"]: n["status"] for n in row.graph_state["nodes"]}
    assert statuses["researcher"] == "completed", statuses
    # 未启动的下游一步都不留在 running/pending。
    assert statuses["analyst"] == "cancelled", statuses
    assert statuses["writer"] == "cancelled", statuses
    assert row.graph_state["status"] == "cancelled", row.graph_state["status"]


async def test_outer_task_cancel_settles_inflight_step(db_session, monkeypatch):
    """客户端断连（消费任务被撤）：在途步骤与整轮都按 cancelled 收尾。"""
    _patch_flag(monkeypatch, engine="1", crewai=True)
    ctx = await _seed_ctx(db_session)
    ex = _HangingExecutor()
    ctx.extra["workflow_executor"] = ex
    _spy_crewai(monkeypatch)

    async def _consume() -> None:
        async for _ in ChatOrchestrator().stream(ctx):
            pass

    task = asyncio.create_task(_consume())
    await asyncio.wait_for(ex.entered.wait(), timeout=10)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    row = (
        await db_session.execute(select(AgentRun).where(AgentRun.id == ctx.run_id))
    ).scalar_one()
    await db_session.refresh(row)
    statuses = {n["id"]: n["status"] for n in row.graph_state["nodes"]}
    assert set(statuses.values()) == {"cancelled"}, statuses
    assert row.graph_state["status"] == "cancelled"


async def test_before_step_cancellation_is_not_a_step_failure():
    """引擎单元层：``before_step``（暂停/取消检查点）抛取消 = 整轮取消。

    旧实现把它当成「永久失败」→ 引擎返回 failed → orchestrator 回退 CrewAI
    重跑整轮。取消必须原样上抛，且不得走 failed 观测通道。
    """
    ex = _UpstreamSpyExecutor()
    cancelled: list[str] = []

    async def before(step_id: str) -> None:
        if step_id == "b":
            raise asyncio.CancelledError()

    engine = WorkflowEngine(
        executor=ex,
        verifier=ScriptedVerifier(["pass"]),
        before_step=before,
        on_step_cancel=lambda sid: cancelled.append(sid),
        on_step_error=lambda sid, msg: pytest.fail("取消不得走 failed 通道"),
    )
    with pytest.raises(asyncio.CancelledError):
        await engine.run(_fanout_plan())
    # b 从未被执行；a 先跑完（取消不得把已完成的工作算成失败）。
    assert "b" not in ex.seen, ex.seen
    assert "a" in ex.seen, ex.seen


async def test_inflight_step_cancellation_reports_cancelled():
    """在途步骤被撤（外层任务取消）：走 on_step_cancel，不走 on_step_error。"""
    cancelled: list[str] = []

    class _CancellingExecutor:
        async def execute(self, step, upstream):
            raise asyncio.CancelledError()

    engine = WorkflowEngine(
        executor=_CancellingExecutor(),
        verifier=ScriptedVerifier(["pass"]),
        on_step_cancel=lambda sid: cancelled.append(sid),
        on_step_error=lambda sid, msg: pytest.fail("取消不得走 failed 通道"),
    )
    with pytest.raises(asyncio.CancelledError):
        await engine.run(
            Plan(version=1, goal="g", steps=[Step(id="a"), Step(id="b")])
        )
    assert cancelled, "在途步骤必须以 cancelled 观测收尾"


# --------------------------------------------------------------------------- #
# ⑤b stage 归属：并发步骤各归各的
# --------------------------------------------------------------------------- #
async def test_stage_attribution_is_per_asyncio_task():
    """两个并发 stage 不得互相覆盖 agent 归属（旧单槽会）。"""
    stage_ctx = make_stage_context(str(uuid.uuid4()))
    observed: dict[str, str] = {}

    async def _stage(agent_id: str, hold: float) -> None:
        token = stage_ctx.set_stage(agent_id=agent_id, task_id=f"t-{agent_id}")
        try:
            await asyncio.sleep(hold)
            observed[agent_id] = stage_ctx.agent_id
        finally:
            stage_ctx.reset_stage(token)

    # b 后写单槽、先返回：旧实现下 a 结束时会读到 b 的归属。
    await asyncio.gather(_stage("a", 0.03), _stage("b", 0.005))
    assert observed == {"a": "a", "b": "b"}, observed


async def test_stage_attribution_falls_back_to_the_shared_slot_offtask():
    """工作线程（不继承 contextvar）仍读得到最后写入的归属 —— 不比修前差。"""
    stage_ctx = make_stage_context(str(uuid.uuid4()))
    token = stage_ctx.set_stage(agent_id="in-worker")
    stage_ctx.reset_stage(token)
    assert stage_ctx.agent_id == "in-worker"

    def _in_thread() -> str:
        return stage_ctx.agent_id

    loop = asyncio.get_running_loop()
    assert await loop.run_in_executor(None, _in_thread) == "in-worker"


async def test_crewai_stage_executor_binds_and_releases_attribution():
    """执行器必须「绑定 + 归还」，不归还的话兄弟步照样串味。"""
    stage_ctx = make_stage_context(str(uuid.uuid4()))
    executor = CrewAIStageExecutor()
    seen: list[str] = []

    async def _fake_run_stage(**kw):
        seen.append(f"{stage_ctx.agent_id}/{stage_ctx.task_id}")
        return "done"

    executor._run_stage = _fake_run_stage  # type: ignore[method-assign]

    outer = stage_ctx.set_stage(agent_id="outer", task_id="t-outer")
    await executor.execute(
        agent_id="inner",
        agent=object(),
        task=SimpleNamespace(id="task-1"),
        context=None,
        stage_ctx=stage_ctx,
    )
    assert seen == ["inner/task-1"], seen
    # 归还后回到外层绑定（而不是停在 inner）。
    assert stage_ctx.agent_id == "outer"
    stage_ctx.reset_stage(outer)


# --------------------------------------------------------------------------- #
# ⑤c run_controls：软上限回收绝不淘汰在跑的 run
# --------------------------------------------------------------------------- #
def test_reclaim_keeps_live_controls_at_the_soft_cap(monkeypatch):
    from app.agents import run_controls as rc

    monkeypatch.setattr(rc, "_controls", {})
    live = [rc.get_or_create(f"live-{i}") for i in range(3)]
    monkeypatch.setattr(rc, "_MAX_CONTROLS", 3)

    new = rc.get_or_create("newcomer")

    # 旧实现按插入序淘汰「最老的一条」= live-0：正在运行的 run 就此失去
    # control，用户点暂停/取消的信号被静默丢弃。
    assert rc.get("live-0") is live[0], "活跃的 control 不得被淘汰"
    assert rc.get("newcomer") is new
    assert len(rc._controls) == 4, "宁可临时突破软上限，也不丢还在跑的信号"


def test_reclaim_drops_idle_controls(monkeypatch):
    from app.agents import run_controls as rc

    monkeypatch.setattr(rc, "_controls", {})
    stale = rc.get_or_create("stale")
    fresh = rc.get_or_create("fresh")
    monkeypatch.setattr(rc, "_MAX_CONTROLS", 1)
    # 只有超过活性线（默认 30 分钟没人碰）的才算已结束/崩溃的孤儿。
    stale.last_seen = time.monotonic() - (rc._IDLE_RECLAIM_SECONDS + 1)

    rc.get_or_create("third")

    assert rc.get("stale") is None, "超时孤儿必须被回收"
    assert rc.get("fresh") is fresh


def test_control_reads_and_writes_keep_a_run_alive(monkeypatch):
    """控制面每次读写都刷新活性 —— 长轮次不会中途失去 control。"""
    from app.agents import run_controls as rc

    monkeypatch.setattr(rc, "_controls", {})
    ctl = rc.get_or_create("run-x")
    stale = time.monotonic() - (rc._IDLE_RECLAIM_SECONDS + 1)

    ctl.last_seen = stale
    assert rc.get("run-x").reclaimable is False, "get() 必须 touch"

    for act in (
        lambda: ctl.pause(),
        lambda: ctl.resume(),
        lambda: ctl.add_instruction("补充要求"),
        lambda: ctl.drain_instructions(),
        lambda: ctl.request_gate(),
    ):
        ctl.last_seen = stale
        act()
        assert ctl.reclaimable is False
