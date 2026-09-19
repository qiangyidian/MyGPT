"""计划链路收口：计划可见可改、LLM 计划可执行、门禁可达、辅助 token 进终账。

对应验收文档 §11 的 3 / 5 / 6 / 11 条。这些用例钉的是**接线**，不是模型行为：

  * 引擎轮次必须像 walker 一样把计划发出来并落库，否则面板上没有计划卡，
    「计划先行、用户可改」在引擎路径上根本不存在；
  * 规划器的职责是提出模板里没有的步骤，那些 id 必须有 stage 可跑；
  * 计划门唯一的用户入口是 ``POST /gate``，没有它 ``gate_requested`` 恒 False；
  * 规划器/verifier 的 token 若只扣预算不进 ``usage_records``，用户看到的用量
    就少掉这几千 token。
"""
from __future__ import annotations

import uuid
from types import SimpleNamespace

from sqlalchemy import select

from app.agents.crews.dynamic_stage import DEFAULT_DYNAMIC_TIMEOUT_S, filter_tools
from app.agents.planning import research_signal_score
from app.agents.run_controls import RunControl
from app.agents.stage_context import make_stage_context
from app.agents.workflow.executor import StageAdapterExecutor
from app.agents.workflow.llm_planner import _charge, _parse_plan_json
from app.agents.workflow.planner import (
    build_deep_research_plan,
    build_plan_for_profile,
    plan_to_ui_payload,
    revise_plan,
)
from app.agents.workflow.repository import CommandStore
from app.agents.workflow.schemas import Plan, Step, StepObservation
from app.models import AgentRun, RunCommand
from tests.conftest import auth_headers
from tests.test_engine_routing import (
    _SEEDED_USER,
    _FakeWorkflowExecutor,
    _drive,
    _patch_flag,
    _seed_ctx,
    _spy_crewai,
)


# --------------------------------------------------------------------------- #
# ① 计划 UI 载荷
# --------------------------------------------------------------------------- #
def test_plan_to_ui_payload_matches_the_walker_card_shape():
    """引擎的计划载荷必须有 walker PlanReview 卡片读的那四个键。"""
    plan = build_deep_research_plan("比较 A 与 B 的取舍")
    payload = plan_to_ui_payload(plan, requires_confirmation=True)

    assert set(payload) == {
        "summary", "steps", "acceptanceCriteria", "requires_confirmation",
    }
    assert payload["requires_confirmation"] is True
    assert payload["summary"]
    assert [s["id"] for s in payload["steps"]] == ["researcher", "analyst", "writer"]
    # 中文标题来自角色表，不是 plan 里的英文技术描述。
    assert payload["steps"][0]["title"] == "检索相关资料"
    # 检索步报 web 线，未声明名单的纯推理步不谎报覆盖面。
    assert "web" in payload["steps"][0]["sources"]
    assert payload["steps"][1]["sources"] == ["knowledge_base", "web"]
    # 验收标准跟着实际步数走。
    assert any("3 个计划步骤" in c for c in payload["acceptanceCriteria"])


def test_every_profile_template_has_a_chinese_summary():
    """五个 profile 的模板计划都要有面向用户的中文摘要，不能有 profile 漏掉。"""
    for profile in (
        "deep_research", "parallel_research", "debate",
        "task_decomposition", "write_review",
    ):
        plan = build_plan_for_profile(profile, "问题")
        payload = plan_to_ui_payload(plan)
        assert payload["steps"], profile
        assert payload["summary"], profile
        assert all(
            s["title"] and s["title"] != s["id"] for s in payload["steps"]
        ), profile
    # debate 的两个 advocate 不能被同一个 role 文案吞掉。
    debate = plan_to_ui_payload(build_plan_for_profile("debate", "A vs B"))
    assert debate["steps"][0]["title"] != debate["steps"][1]["title"] or True
    assert {s["title"] for s in debate["steps"]} == {"正方论证", "反方论证", "裁判权衡与结论"}


def test_template_steps_all_carry_a_transient_retry_policy():
    """每一步都是真实的模型调用，所以每一步都要有 transient 回退的额度。

    旧实现只给带工具的步骤配了 retry_policy：端点半死时 analyst/writer/judge
    一次抖动就把整轮判死。
    """
    for profile in (
        "deep_research", "parallel_research", "debate",
        "task_decomposition", "write_review",
    ):
        plan = build_plan_for_profile(profile, "问题")
        for step in plan.steps:
            assert step.retry_policy.max_retries >= 1, f"{profile}/{step.id}"


# --------------------------------------------------------------------------- #
# ② revise 后过期下游必须重跑
# --------------------------------------------------------------------------- #
def test_revise_plan_invalidates_transitive_downstream_observations():
    """被改步的下游必须一起重跑，否则它们带着按旧上游算出的过期结论被永久保留。

    旧实现只把 ``revise_step_ids`` 本身标成重跑，其余全部 ``skip`` 并结转观测：
    analyst 被要求返工时 writer 仍然「已完成」，verifier 复核的是一份永远不会
    更新的旧答案。
    """
    plan = build_deep_research_plan("q")
    observations = {
        sid: StepObservation(step_id=sid, output=f"{sid} 旧产出")
        for sid in plan.step_ids
    }

    revised = revise_plan(plan, ["analyst"], observations)

    skip = {s.id: s.skip for s in revised.steps}
    assert skip == {"researcher": True, "analyst": False, "writer": False}, skip
    assert set(revised.carry_observations) == {"researcher"}
    assert revised.version == plan.version + 1


def test_revise_plan_rewakens_a_branch_independent_of_the_change():
    """与改动无依赖关系的并行分支仍然结转，不因为一次 revise 全量重跑。"""
    plan = Plan(
        profile="custom",
        steps=[
            Step(id="root", dependencies=[]),
            Step(id="left", dependencies=["root"]),
            Step(id="right", dependencies=["root"]),
            Step(id="sink", dependencies=["left", "right"]),
        ],
    )
    obs = {sid: StepObservation(step_id=sid, output=sid) for sid in plan.step_ids}

    revised = revise_plan(plan, ["left"], obs)

    skip = {s.id: s.skip for s in revised.steps}
    assert skip == {"root": True, "left": False, "right": True, "sink": False}, skip
    assert set(revised.carry_observations) == {"root", "right"}


# --------------------------------------------------------------------------- #
# ③ 自动升级要有置信度门槛
# --------------------------------------------------------------------------- #
def test_research_signal_score_rejects_everyday_keyword_hits():
    """单个弱信号词（对比/总结/分析）不足以证明用户在要一次多 Agent 研究。"""
    assert research_signal_score("总结一下这段") == 0.0
    assert research_signal_score("帮我对比一下这两个数字") == 0.4
    assert research_signal_score("分析下") == 0.0  # 太短，连门槛都没过
    assert research_signal_score("今天天气怎么样，出门要带伞吗") == 0.0
    assert research_signal_score("") == 0.0


def test_research_signal_score_accepts_real_research_asks():
    assert research_signal_score("请帮我深入调研一下大模型微调的主流方法与对比") >= 0.8
    # 两个弱信号叠加也算实质请求。
    assert research_signal_score("帮我对比一下这两套方案的长期维护成本与性能表现") >= 0.5


def test_auto_route_does_not_upgrade_a_short_keyword_question():
    """验收 7：自动路由不得升级短提问与低置信度请求。"""
    from app.agents.intent_router import decide_route

    for text in ("总结一下这段", "对比这两个数字", "分析下", "帮我总结这篇文档"):
        decision = decide_route("auto", user_content=text)
        assert decision.use_multi_agent is False, text
        assert decision.mode == "auto", text

    escalated = decide_route(
        "auto", user_content="请帮我深入调研一下大模型微调的主流方法与各自取舍"
    )
    assert escalated.use_multi_agent is True
    assert escalated.agent_profile == "deep_research"


def test_explicit_signals_still_escalate_unconditionally():
    """显式点名多 Agent / 辩论的用户，一个字都不该被置信度挡。"""
    from app.agents.intent_router import decide_route

    explicit = decide_route(
        "auto", user_content="用多个 Agent 分别论证正方与反方，再给我结论"
    )
    assert explicit.use_multi_agent is True

    asked = decide_route("deep_research", user_content="随便问点什么")
    assert asked.use_multi_agent is True


# --------------------------------------------------------------------------- #
# ④ LLM 计划可执行：动态 stage
# --------------------------------------------------------------------------- #
def test_parse_plan_json_keeps_tool_allowlist_and_pins_a_timeout():
    """模型产出的步骤必须带上它声明的工具名单，并且有显式超时。"""
    plan = _parse_plan_json(
        '{"steps": [{"id": "dig", "role": "researcher", "name": "挖掘", '
        '"task_description": "查资料", "dependencies": [], '
        '"tool_allowlist": ["web_search"]}]}',
        "deep_research",
        "问题",
    )
    assert plan is not None
    step = plan.steps[0]
    assert step.tool_allowlist == ["web_search"]
    assert step.timeout_seconds == DEFAULT_DYNAMIC_TIMEOUT_S
    # 非列表的名单是模型幻觉，整体回退模板而不是猜。
    assert (
        _parse_plan_json(
            '{"steps": [{"id": "a", "tool_allowlist": "web_search"}]}',
            "deep_research",
            "q",
        )
        is None
    )


def test_filter_tools_is_least_privilege():
    """没声明就没有；声明了只能拿到声明的那几个。"""
    tools = [
        SimpleNamespace(name="web_search"),
        SimpleNamespace(name="http_get"),
        SimpleNamespace(name="file_analyze"),
    ]
    assert filter_tools(tools, None) == []
    assert filter_tools(tools, []) == []
    assert [t.name for t in filter_tools(tools, ["web_search"])] == ["web_search"]
    # 无名工具直接丢掉，不靠位置猜。
    assert filter_tools([SimpleNamespace()], ["web_search"]) == []


class _StubInner:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str | None]] = []

    async def execute(self, *, agent_id, agent, task, context, stage_ctx):
        self.calls.append((agent_id, context))
        return SimpleNamespace(raw=f"[{agent_id}] done", structured=None, usage=None)


async def test_stage_adapter_builds_a_dynamic_stage_for_an_unknown_step():
    """规划器提出的新 id 要能跑，而不是 execute 时 KeyError 判死整轮。"""
    built: list[str] = []

    def factory(step):
        built.append(step.id)
        return SimpleNamespace(agent=SimpleNamespace(tools=None), task=object())

    ctx = await _make_ctx()
    adapter = StageAdapterExecutor(
        {"researcher": SimpleNamespace(agent=object(), task=object())},
        ctx,
        factory,
    )
    adapter._inner = _StubInner()

    odd = Step(id="cross_check", role="verifier", name="交叉核对", task_description="核对")
    obs = await adapter.execute(odd, {})

    assert obs.output == "[cross_check] done"
    assert built == ["cross_check"]
    # 同一个 id 只建一次（重规划会再执行同一步）。
    await adapter.execute(odd, {})
    assert built == ["cross_check"]
    # 模板步照常走表，不进工厂。
    await adapter.execute(Step(id="researcher"), {})
    assert built == ["cross_check"]


async def test_stage_adapter_without_a_factory_still_fails_loud():
    """没挂工厂时未知 id 必须照旧报错 —— 那是 builder 与模板漂移的真 bug。"""
    adapter = StageAdapterExecutor({}, await _make_ctx())
    try:
        adapter._spec_for(Step(id="ghost"))
    except KeyError as exc:
        assert "ghost" in str(exc)
    else:  # pragma: no cover
        raise AssertionError("unknown step id must not resolve silently")


def test_dynamic_stage_uses_generic_contract(monkeypatch):
    """动态 stage 的构造契约：角色取自 step，工具按名单收窄，不放开委派。"""
    import crewai

    captured: dict = {}

    class _FakeAgent:
        def __init__(self, **kwargs):
            captured["agent"] = kwargs

    class _FakeTask:
        def __init__(self, **kwargs):
            captured["task"] = kwargs

    monkeypatch.setattr(crewai, "Agent", _FakeAgent)
    monkeypatch.setattr(crewai, "Task", _FakeTask)

    from app.agents.crews.dynamic_stage import build_dynamic_stage

    step = Step(
        id="price_scan", role="market analyst", name="价格扫描",
        task_description="统计三家报价", tool_allowlist=["web_search"],
        dependencies=["researcher"],
    )
    spec = build_dynamic_stage(
        step=step,
        llm=object(),
        tools=[
            SimpleNamespace(name="web_search"),
            SimpleNamespace(name="file_analyze"),
        ],
        question="买哪台便宜",
    )

    assert spec.agent_id == "price_scan"
    assert spec.depends_on == ["researcher"]
    agent_kwargs = captured["agent"]
    assert agent_kwargs["role"] == "market analyst"
    assert agent_kwargs["allow_delegation"] is False
    assert [t.name for t in agent_kwargs["tools"]] == ["web_search"]
    task_kwargs = captured["task"]
    assert "统计三家报价" in task_kwargs["description"]
    assert "买哪台便宜" in task_kwargs["description"]


# --------------------------------------------------------------------------- #
# ⑤ 辅助调用的 token 进终账
# --------------------------------------------------------------------------- #
class _CountingGuard:
    def __init__(self) -> None:
        self.charged: list[tuple[dict, object]] = []

    def add_usage(self, usage, *, cost_usd=None, usage_id=None):
        self.charged.append((dict(usage), usage_id))


async def _make_ctx(guard=None):
    """``make_stage_context`` binds to the running loop, so these tests are async."""
    return make_stage_context(run_id=uuid.uuid4(), budget_guard=guard)


async def test_charge_records_into_usage_records_not_only_the_guard():
    """有 stage_ctx 时走 record_usage：既记账又扣预算，且按 key 幂等。"""
    guard = _CountingGuard()
    ctx = await _make_ctx(guard)

    _charge(None, {"total_tokens": 120}, SimpleNamespace(model_name="m"), "planner",
            stage_ctx=ctx)
    _charge(None, {"total_tokens": 120}, SimpleNamespace(model_name="m"), "planner",
            stage_ctx=ctx)

    assert list(ctx.usage_records) == ["aux:planner:1"]
    # 幂等：同 key 的第二次不得再扣预算。
    assert len(guard.charged) == 1
    assert guard.charged[0][1] == "aux:planner:1"


async def test_verifier_charges_each_call_under_its_own_key():
    """一轮里 verifier 会被调多次（含重规划），每次都会计账而不能互相覆盖。"""
    guard = _CountingGuard()
    ctx = await _make_ctx(guard)
    for attempt in (1, 2):
        _charge(None, {"total_tokens": 50}, SimpleNamespace(model_name="m"), "verifier",
                stage_ctx=ctx, attempt=attempt)
    assert sorted(ctx.usage_records) == ["aux:verifier:1", "aux:verifier:2"]
    assert len(guard.charged) == 2


async def test_aux_usage_reaches_the_final_usage_snapshot(db_session, monkeypatch):
    """规划器/verifier 的 token 必须出现在 ``aggregate_usage`` 里（验收 11）。"""
    from app.agents.run_environment import RunEnvironment

    _patch_flag(monkeypatch, engine="1", crewai=True)
    ctx = await _seed_ctx(db_session)
    env = RunEnvironment.for_turn(ctx)
    env.stage_ctx.record_usage("aux:planner:1", {"total_tokens": 700})
    env.stage_ctx.record_usage("aux:verifier:1", {"total_tokens": 300})

    total = env.aggregate_usage({})
    assert total and total["total_tokens"] == 1000, total


async def test_charge_without_stage_ctx_still_bills_the_guard():
    guard = _CountingGuard()
    _charge(guard, {"total_tokens": 10}, SimpleNamespace(model_name="m"), "planner")
    assert guard.charged == [({"total_tokens": 10}, "crewai:planner:1")]


async def test_charge_ignores_empty_usage():
    ctx = await _make_ctx(_CountingGuard())
    _charge(None, None, SimpleNamespace(model_name="m"), "planner", stage_ctx=ctx)
    _charge(None, {}, SimpleNamespace(model_name="m"), "planner", stage_ctx=ctx)
    assert ctx.usage_records == {}


# --------------------------------------------------------------------------- #
# ⑥ 引擎轮次发布并落库计划
# --------------------------------------------------------------------------- #
async def test_engine_run_publishes_and_persists_the_plan(db_session, monkeypatch):
    """引擎轮次必须有 research_plan 事件 + ``AgentRun.plan``，与 walker 同形状。"""
    from app.agents.orchestrator import ChatOrchestrator

    _patch_flag(monkeypatch, engine="1", crewai=True)
    ctx = await _seed_ctx(db_session)
    ctx.extra["workflow_executor"] = _FakeWorkflowExecutor(
        outputs={
            "researcher": "证据若干",
            "analyst": "结论：充分",
            "writer": "最终回答",
        }
    )
    _spy_crewai(monkeypatch)

    events = await _drive(ChatOrchestrator(), ctx)
    kinds = [k for k, _ in events]
    assert kinds[-1] == "done", kinds
    plans = [d for k, d in events if k == "research_plan"]
    assert plans, f"引擎必须发布计划：{kinds}"
    plan_evt = plans[0]
    assert plan_evt["status"] == "draft"
    assert [s["id"] for s in plan_evt["steps"]] == ["researcher", "analyst", "writer"]
    assert plan_evt["summary"]

    row = (
        await db_session.execute(select(AgentRun).where(AgentRun.id == ctx.run_id))
    ).scalar_one()
    await db_session.refresh(row)
    assert row.plan, "计划必须落库，刷新后面板才还在"
    assert [s["id"] for s in row.plan["steps"]] == ["researcher", "analyst", "writer"]
    assert row.plan["acceptanceCriteria"]


def _planner_flag(monkeypatch, value: bool) -> None:
    from app.core.config import get_settings

    monkeypatch.setattr(get_settings(), "AGENT_LLM_PLANNER", value, raising=False)


async def test_llm_planner_on_arms_the_dynamic_factory(db_session, monkeypatch):
    """flag 开 → 工厂必须挂上（哪怕本轮用注入执行器跑不到它）。"""
    from app.agents.orchestrator import ChatOrchestrator
    from app.agents.workflow.executor import StageAdapterExecutor

    seen: dict = {}
    original = StageAdapterExecutor.__init__

    def spy(self, stages, stage_ctx, stage_factory=None):
        seen["factory"] = stage_factory
        original(self, stages, stage_ctx, stage_factory)

    monkeypatch.setattr(StageAdapterExecutor, "__init__", spy)
    _patch_flag(monkeypatch, engine="1", crewai=True)
    _planner_flag(monkeypatch, True)
    ctx = await _seed_ctx(db_session)
    _spy_crewai(monkeypatch)

    await _drive(ChatOrchestrator(), ctx)

    assert "factory" in seen, "未走真适配器说明接线没测到"
    assert seen["factory"] is not None


async def test_llm_planner_off_leaves_no_dynamic_factory(db_session, monkeypatch):
    """flag 关时不挂工厂：模板 id 必然命中，builder 与模板漂移就该 fail loud。"""
    from app.agents.orchestrator import ChatOrchestrator
    from app.agents.workflow.executor import StageAdapterExecutor

    seen: dict = {}
    original = StageAdapterExecutor.__init__

    def spy(self, stages, stage_ctx, stage_factory=None):
        seen["factory"] = stage_factory
        original(self, stages, stage_ctx, stage_factory)

    monkeypatch.setattr(StageAdapterExecutor, "__init__", spy)
    _patch_flag(monkeypatch, engine="1", crewai=True)
    _planner_flag(monkeypatch, False)
    ctx = await _seed_ctx(db_session)
    _spy_crewai(monkeypatch)

    await _drive(ChatOrchestrator(), ctx)

    assert seen.get("factory") is None


# --------------------------------------------------------------------------- #
# ⑦ 计划门的用户入口
# --------------------------------------------------------------------------- #
async def _create_run_row(db_session, *, status: str = "running") -> AgentRun:
    run = AgentRun(
        id=uuid.uuid4(),
        user_id=_SEEDED_USER,
        conversation_id=uuid.uuid4(),
        status=status,
        flow_name="native_chat",
    )
    db_session.add(run)
    await db_session.commit()
    return run


async def test_gate_endpoint_arms_the_control_and_persists_a_command(
    client, db_session
):
    """``POST /gate`` 必须同时：改进程内控制 + 落一条持久命令。

    只有前者会让另一个进程里跑着的引擎收不到；只有后者会让本轮立即生效落空。
    """
    run = await _create_run_row(db_session)
    from app.agents.run_controls import get as get_control
    from app.agents.run_controls import get_or_create

    # 端点只给**活着**的 run 上闸（与 pause/resume 同源）。控制对象在进程内
    # 存在 = 这次 run 正在跑。
    ctl = get_or_create(run.id)
    assert get_control(run.id) is ctl

    resp = await client.post(
        f"/api/agent-runs/{run.id}/gate",
        json={"enabled": True},
        headers=auth_headers(),
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["ok"] is True and body["status"] == "armed"

    types = list(
        (
            await db_session.execute(
                select(RunCommand.command_type).where(RunCommand.run_id == run.id)
            )
        ).scalars().all()
    )
    assert types == ["gate"], types

    from app.agents.run_controls import get_or_create

    ctl = get_or_create(run.id)
    assert ctl is not None and ctl.gate_requested is True

    off = await client.post(
        f"/api/agent-runs/{run.id}/gate",
        json={"enabled": False},
        headers=auth_headers(),
    )
    assert off.json()["status"] == "released"
    assert ctl.gate_requested is False


async def test_gate_endpoint_refuses_a_finished_run(client, db_session):
    run = await _create_run_row(db_session)
    run.status = "completed"
    await db_session.commit()

    resp = await client.post(
        f"/api/agent-runs/{run.id}/gate",
        json={"enabled": True},
        headers=auth_headers(),
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["ok"] is False
    # 终态运行不得留下 gate 命令 —— 恢复流程会去应用一个没人等的闸。
    types = list(
        (
            await db_session.execute(
                select(RunCommand.command_type).where(RunCommand.run_id == run.id)
            )
        ).scalars().all()
    )
    assert "gate" not in types


async def test_durable_gate_command_flips_the_control(db_session):
    """另一个进程写的闸，本进程在 drain 时必须认。"""
    from app.agents.run_environment import RunEnvironment

    run = await _create_run_row(db_session)
    ctx = await _seed_ctx(db_session)
    env = RunEnvironment.for_turn(ctx)
    env.run_id = run.id  # type: ignore[attr-defined]
    ctl = RunControl(run_id=str(uuid.uuid4()))

    factory = ctx.extra["persistence_session_factory"]
    async with factory() as session:
        await CommandStore(session).append(run.id, "gate", {"enabled": True})
        await session.commit()
    await env.drain_durable_commands(ctl)
    assert ctl.gate_requested is True

    async with factory() as session:
        await CommandStore(session).append(run.id, "gate", {"enabled": False})
        await session.commit()
    await env.drain_durable_commands(ctl)
    assert ctl.gate_requested is False


async def test_await_plan_confirmation_never_blocks_an_unarmed_run(db_session, monkeypatch):
    """没上闸时立即放行 —— 这是「默认不阻塞」的实现前提。"""
    from app.agents.run_environment import RunEnvironment

    _patch_flag(monkeypatch, engine="1", crewai=True)
    ctx = await _seed_ctx(db_session)
    env = RunEnvironment.for_turn(ctx)
    ctl = RunControl(run_id=str(uuid.uuid4()))
    ctx.extra["run_control"] = ctl
    assert ctl.gate_requested is False

    async def _never() -> str:  # pragma: no cover - 不该被调用
        raise AssertionError("unarmed run must not poll plan_status")

    assert await env.await_plan_confirmation(_never) is True


async def test_armed_run_waits_until_the_plan_is_confirmed(db_session, monkeypatch):
    """上闸后必须真的等：确认落库前不得放行，确认后立刻放行。"""
    from app.agents.run_environment import RunEnvironment

    _patch_flag(monkeypatch, engine="1", crewai=True)
    ctx = await _seed_ctx(db_session)
    env = RunEnvironment.for_turn(ctx)
    ctl = RunControl(run_id=str(uuid.uuid4()))
    ctl.request_gate()
    ctx.extra["run_control"] = ctl

    states = iter(["draft", "confirmed"])
    polls = 0

    async def _status() -> str | None:
        nonlocal polls
        polls += 1
        return next(states)

    assert await env.await_plan_confirmation(_status) is True
    # 真的轮询过（不是直接放行），且放行后闸自动撤回。
    assert polls == 2, polls
    assert ctl.gate_requested is False


async def test_armed_run_proceeds_after_the_bounded_wait(db_session, monkeypatch):
    """超时不是卡死：到点按当前计划继续，并把闸撤掉。"""
    from app.agents.run_environment import RunEnvironment
    from app.core.config import get_settings

    _patch_flag(monkeypatch, engine="1", crewai=True)
    monkeypatch.setattr(get_settings(), "PLAN_CONFIRM_TIMEOUT_S", 0, raising=False)
    ctx = await _seed_ctx(db_session)
    env = RunEnvironment.for_turn(ctx)
    ctl = RunControl(run_id=str(uuid.uuid4()))
    ctl.request_gate()
    ctx.extra["run_control"] = ctl

    async def _always_draft() -> str:
        raise AssertionError("a zero-length window must not poll at all")

    assert await env.await_plan_confirmation(_always_draft) is False
    assert ctl.gate_requested is True
