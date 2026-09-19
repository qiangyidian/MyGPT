"""按 plan 里的 Step 现场构造 stage —— 让 LLM 规划器的产出真的能跑。

模板 plan 的 step id 与 crew builder 的 agent_id 一一对应，所以
:class:`~app.agents.workflow.executor.StageAdapterExecutor` 直接查表就行。但
LLM 规划器（``AGENT_LLM_PLANNER``）的职责恰恰是**提出模板里没有的步骤**，那些
id 在 stage 表里查不到 —— 旧行为是 execute 时 KeyError，被引擎当成永久失败，
于是「开规划器」等于「多 Agent 轮次随机全挂」。

这里补齐缺的那一环：用一个与 crew builder 同形状的工厂，把 Step 自带的
``role`` / ``task_description`` 变成一对 CrewAI ``Agent`` + ``Task``。工具按
``step.tool_allowlist`` 收窄（最小权限：没声明就没有），超时给一个保守默认值，
避免动态步骤反而比模板步骤更不受约束。
"""
from __future__ import annotations

from typing import Any

from app.agents.crews.stage import StageSpec

# 动态步骤没有模板可抄，超时取一个保守默认；``Step.timeout_seconds`` 显式给了
# 就尊重它（引擎侧还会再与总预算取 min）。
DEFAULT_DYNAMIC_TIMEOUT_S = 120.0

# 规划器只写了 role/goal 的骨架时用的通用兜底描述。措辞保持与模板 agent 同
# 一类（角色 + 不臆造），因为这是模型唯一能看到的自我约束说明。
_GENERIC_BACKSTORY = (
    "You are one step inside a planned multi-agent workflow. You do ONLY the "
    "work assigned to you in your task description, using evidence given to you "
    "in the context. You never fabricate sources or facts, and you do not "
    "expand scope beyond your assigned step."
)


def filter_tools(tools: list[Any], allowlist: list[str] | None) -> list[Any]:
    """Narrow a tool list to ``allowlist`` by tool name.

    An empty/missing allowlist means the step declared nothing, so it gets
    **nothing** — a planner-invented step must not inherit the run's full tool
    set by omission. Unnamed entries are dropped rather than guessed at.
    """
    wanted = {str(name) for name in (allowlist or []) if str(name).strip()}
    if not wanted:
        return []
    kept: list[Any] = []
    for tool in tools or []:
        name = getattr(tool, "name", None) or getattr(tool, "__name__", None)
        if name is None:
            continue
        if str(name) in wanted:
            kept.append(tool)
    return kept


def build_dynamic_stage(
    *,
    step: Any,
    llm: Any,
    tools: list[Any],
    question: str,
) -> StageSpec:
    """Build one :class:`StageSpec` from a plan :class:`Step`.

    ``step.dependencies`` are not translated into CrewAI task wiring on purpose:
    the engine feeds upstream outputs in as the task ``context`` string (same as
    it does for template stages), so re-declaring them here would double the
    handoff.
    """
    from crewai import Agent, Task

    role = (getattr(step, "role", "") or getattr(step, "name", "") or "specialist").strip()
    name = (getattr(step, "name", "") or getattr(step, "id", "step")).strip()
    description = (getattr(step, "task_description", "") or "").strip()
    goal = (
        description
        or f"Complete the planned step {name!r} for the user's request."
    )
    step_tools = filter_tools(tools, getattr(step, "tool_allowlist", None))

    agent = Agent(
        role=role,
        goal=goal[:500],
        backstory=_GENERIC_BACKSTORY,
        llm=llm,
        tools=step_tools or None,
        allow_delegation=False,
        verbose=False,
    )
    task = Task(
        description=(
            f"{goal}\n\nOriginal user request: {(question or '').strip()}\n\n"
            "Answer with the deliverable only; do not restate the plan."
        ),
        expected_output=(
            f"The concrete result of the planned step {name!r}, "
            "self-contained enough to be handed to downstream steps."
        ),
        agent=agent,
    )
    spec = StageSpec(
        agent_id=str(getattr(step, "id", name)),
        agent=agent,
        task=task,
        depends_on=[str(d) for d in (getattr(step, "dependencies", None) or [])],
    )
    return spec
