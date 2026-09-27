"""写-审-改的「审 → 是否返修」判定（B13）。

引擎的状态机是 execute → verify → (bounded) replan：:class:`Verifier` 产出
一个 :class:`~app.agents.workflow.schemas.VerifierResult`，然后
:class:`~app.agents.workflow.engine.WorkflowEngine` 要么收工、要么返修。
之前它只有**一条**内联判据（``replans >= current.max_replans``），于是：

  * 审阅结论（``findings``）算出来就丢了 —— 返修的那一步拿到的提示词和第一遍
    一字不差，「改」环节等于没接上「审」。
  * 返修次数没进预算账：``BudgetGuard.enter_replan()`` 是全仓权威闸门，但引擎
    从没调过它，所以 ``max_replan_count`` 对引擎路径完全无效。
  * 结论也没出现在 run 的 trace / metadata 里，事后无法解释「为什么改了两轮」。

本模块把这三件事里**可判定**的那部分收成一个纯函数：输入 verdict + 当前轮次
+ 两份预算余量，输出一个决定（收工 / 返修 / 因何而停）。引擎只负责执行这个
决定，不再自带策略。判定规则本身不是新发明 —— 它就是引擎原先内联的那一条，
外加把 ``BudgetGuard`` 早已声明、只是没人调的 replan 预算接上。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from app.agents.workflow.schemas import (
    StepObservation,
    VerificationVerdict,
    VerifierResult,
)

#: 决定的种类。``revise`` 之外全是不再返修的收工理由。
ACTION_ACCEPT = "accept"
ACTION_REVISE = "revise"
ACTION_TERMINATE = "terminate"
ACTION_PLAN_LIMIT = "plan_limit_reached"
ACTION_BUDGET_LIMIT = "replan_budget_exhausted"

#: 面向用户的收工理由（trace 与 run.output 里会看到）。
_MESSAGES: dict[str, str] = {
    ACTION_ACCEPT: "验收通过",
    ACTION_REVISE: "审阅发现问题，返修相关步骤",
    ACTION_TERMINATE: "审阅判定为不可接受，终止本轮",
    ACTION_PLAN_LIMIT: "计划允许的返修轮次已用完",
    ACTION_BUDGET_LIMIT: "运行预算中的返修次数已用完",
}


@dataclass(frozen=True, slots=True)
class ReviewVerdict:
    """一次审阅的结构化结论 —— 引擎里 ``VerifierResult`` 的判定视图。

    与 ``VerifierResult`` 分开的理由：判定只关心「过没过、改哪几步、第几轮」，
    把轮次和预算余量混进 schema 会让持久化格式跟着膨胀。
    """

    passed: bool
    revise_step_ids: tuple[str, ...] = ()
    findings: tuple[str, ...] = ()
    round_index: int = 0
    #: 硬失败（审阅判定这轮不该再救）
    fatal: bool = False

    @classmethod
    def from_verifier_result(
        cls, verdict: VerifierResult, *, round_index: int = 0
    ) -> ReviewVerdict:
        # ``VerificationVerdict`` 是 str-Enum：3.11 起 ``str(member)`` 会带上
        # 类名，只有 ``.value`` 才是 "pass"/"revise"/"fail"。
        value = getattr(verdict.verdict, "value", verdict.verdict)
        str_value = str(value)
        return cls(
            passed=str_value == VerificationVerdict.pass_.value,
            revise_step_ids=tuple(verdict.revise_step_ids or ()),
            findings=tuple(str(f) for f in (verdict.findings or ())),
            round_index=round_index,
            fatal=str_value == VerificationVerdict.fail.value,
        )


@dataclass(frozen=True, slots=True)
class RevisionDecision:
    """纯判定的结果：引擎接下来该做什么。"""

    action: str
    #: 需要返修的步骤 id（``action == revise`` 时非空）
    revise_step_ids: tuple[str, ...] = ()
    #: 交给返修步骤的审阅意见（**必须**跟着一起进下一轮，否则「改」无据可依）
    findings: tuple[str, ...] = ()
    round_index: int = 0
    #: 是否要消耗一个预算返修额度
    consumes_budget: bool = False
    reason: str = ""
    #: 计划自己的上限（plan.max_replans）与预算余量，一起进 trace
    plan_max_replans: int = 0
    replans_used: int = 0

    @property
    def should_revise(self) -> bool:
        return self.action == ACTION_REVISE

    @property
    def stops(self) -> bool:
        """这条决定是否终结本轮（``revise`` 之外都终结）。"""
        return self.action != ACTION_REVISE

    def as_trace(self) -> dict[str, Any]:
        """进 trace / run.metadata 的自述（JSON 友好）。"""
        return {
            "action": self.action,
            "reason": self.reason,
            "round": self.round_index,
            "revise_step_ids": list(self.revise_step_ids),
            "findings": list(self.findings),
            "plan_max_replans": self.plan_max_replans,
            "replans_used": self.replans_used,
            "budget_consumed": self.consumes_budget,
        }


@dataclass(slots=True)
class ReplanBudget:
    """两份返修额度的读数：计划自己的上限 + 运行预算的余量。

    ``plan`` 上限是模板/规划器写进 ``Plan.max_replans`` 的；``guard_remaining``
    来自 ``BudgetGuard``（None = 这一路没有预算闸门，只按计划上限走）。
    """

    plan_max: int = 0
    replans_used: int = 0
    guard_remaining: int | None = None

    def guard_allows(self) -> bool:
        return self.guard_remaining is None or self.guard_remaining > 0

    def plan_allows(self) -> bool:
        return self.replans_used < self.plan_max


def decide_revision(
    verdict: ReviewVerdict, budget: ReplanBudget
) -> RevisionDecision:
    """审阅结论 + 额度读数 → 下一步动作。无副作用、无 IO。

    判定顺序是刻意的：**先**认硬失败（再多的额度也不该救一个被判 fail 的
    结论），**再**按计划上限收口（这是引擎原有的那条判据，保持优先，避免
    行为改变），最后才看预算余量 —— 预算是外部闸门，不该盖过计划意图。
    """
    if verdict.fatal:
        return RevisionDecision(
            action=ACTION_TERMINATE,
            round_index=verdict.round_index,
            reason=_MESSAGES[ACTION_TERMINATE],
            findings=verdict.findings,
            plan_max_replans=budget.plan_max,
            replans_used=budget.replans_used,
        )
    if verdict.passed:
        return RevisionDecision(
            action=ACTION_ACCEPT,
            round_index=verdict.round_index,
            reason=_MESSAGES[ACTION_ACCEPT],
            plan_max_replans=budget.plan_max,
            replans_used=budget.replans_used,
        )
    plan_max = max(0, int(budget.plan_max))
    if not budget.plan_allows():
        return RevisionDecision(
            action=ACTION_PLAN_LIMIT,
            round_index=verdict.round_index,
            reason=_MESSAGES[ACTION_PLAN_LIMIT],
            findings=verdict.findings,
            plan_max_replans=plan_max,
            replans_used=budget.replans_used,
        )
    if not budget.guard_allows():
        return RevisionDecision(
            action=ACTION_BUDGET_LIMIT,
            round_index=verdict.round_index,
            reason=_MESSAGES[ACTION_BUDGET_LIMIT],
            findings=verdict.findings,
            plan_max_replans=plan_max,
            replans_used=budget.replans_used,
        )
    if not verdict.revise_step_ids:
        # 说了要改却没点名改哪步：没什么可返修的，按收工处理，
        # 比盲重跑整张计划便宜，也不会把已验收的产物冲掉。
        return RevisionDecision(
            action=ACTION_ACCEPT,
            round_index=verdict.round_index,
            reason="审阅要求返修但未指定步骤，按当前产出收工",
            findings=verdict.findings,
            plan_max_replans=plan_max,
            replans_used=budget.replans_used,
        )
    return RevisionDecision(
        action=ACTION_REVISE,
        revise_step_ids=verdict.revise_step_ids,
        findings=verdict.findings,
        round_index=verdict.round_index + 1,
        consumes_budget=True,
        reason=_MESSAGES[ACTION_REVISE],
        plan_max_replans=plan_max,
        replans_used=budget.replans_used + 1,
    )


def replan_budget_for(
    plan: Any, guard: Any, *, replans_used: int
) -> ReplanBudget:
    """从 plan + guard 读出两份额度。guard 缺失/无 limits 时留 ``None``。

    ``guard_remaining`` 是**读数**，不做任何累加 —— 累加归
    ``BudgetGuard.enter_replan()``，由引擎在真的返修之前调一次。
    """
    remaining: int | None = None
    limits = getattr(guard, "limits", None)
    if limits is not None:
        max_replans = int(getattr(limits, "max_replan_count", 0) or 0)
        remaining = max(0, max_replans - int(getattr(guard, "replans", 0) or 0))
    return ReplanBudget(
        plan_max=int(getattr(plan, "max_replans", 0) or 0),
        replans_used=int(replans_used or 0),
        guard_remaining=remaining,
    )


def findings_for_steps(
    decision: RevisionDecision, observations: dict[str, StepObservation]
) -> tuple[str, ...]:
    """把审阅意见整理成给返修步骤用的文本。

    observations 目前只用来确认「哪些步真的跑过」；保留这个入参是为了让
    未来的 reviewer 能把「上一步产出了什么」一并带上，而不必再改签名。
    """
    findings = [f.strip() for f in decision.findings if str(f).strip()]
    return tuple(findings)


@dataclass(slots=True)
class ReviewTrace:
    """一轮 run 的审阅台账：每条 verdict 与它导致的决定。"""

    entries: list[dict[str, Any]] = field(default_factory=list)

    def record(
        self, verdict: VerifierResult, decision: RevisionDecision, *, round_index: int
    ) -> None:
        self.entries.append(
            {
                "round": round_index,
                "verdict": str(
                    getattr(verdict.verdict, "value", verdict.verdict)
                ),
                "findings": list(verdict.findings or ()),
                "revise_step_ids": list(verdict.revise_step_ids or ()),
                "decision": decision.as_trace(),
            }
        )

    def as_list(self) -> list[dict[str, Any]]:
        return list(self.entries)
