"""上下文 Token 预算（RAG-09）。

文档对预算的算法写得很具体：「预算以模型总输入窗口减去 System/策略、用户 Query、
工具定义、预留输出 Token 后计算；Context Pack 不超过剩余可用 Token 的 80%」。

在这之前预算是**一个绝对数**（``RAG_CONTEXT_TOKENS=6000``）。绝对数的问题是它和模型
无关：同一个 6000 在 8k 窗口的模型上会挤掉输出、在 200k 窗口的模型上则白白浪费 97%
的可检索空间。所以改成「按窗口算」——但**保留绝对数作为兜底**：调用方拿不到窗口信息
时（未知模型、自定义端点没填 ``max_context_tokens``）必须有一条明确的退路，而不是
猜一个窗口。

纯函数，无 IO，``ratio`` 与各项预留都可注入，因此每种边界都能钉在单测里。
"""
from __future__ import annotations

from dataclasses import dataclass

__all__ = ["BudgetBreakdown", "CONTEXT_BUDGET_RATIO", "MIN_CONTEXT_BUDGET", "compute_context_budget"]

#: Context Pack 只能占「剩余可用预算」的这个比例，留出安全余量（文档 RAG-09）。
CONTEXT_BUDGET_RATIO = 0.8

#: 预算低于这个值就没有意义了：装不下一个完整分片，只会让上下文变成半句话。
MIN_CONTEXT_BUDGET = 256


@dataclass(frozen=True)
class BudgetBreakdown:
    """预算拆解。留痕是为了回答「这轮为什么只塞了 3 个分片」这种问题。"""

    context_window: int
    reserved_system: int
    reserved_query: int
    reserved_output: int
    reserved_tools: int
    available: int
    budget: int
    #: 绝对兜底生效时为 True（拿不到窗口信息）。
    fallback_used: bool = False
    #: 运维成本上限（``RAG_CONTEXT_TOKENS``），0 = 不设。
    hard_ceiling: int = 0

    def to_metadata(self) -> dict[str, int | bool]:
        return {
            "context_window": self.context_window,
            "reserved_system": self.reserved_system,
            "reserved_query": self.reserved_query,
            "reserved_output": self.reserved_output,
            "reserved_tools": self.reserved_tools,
            "available": self.available,
            "context_budget": self.budget,
            "hard_ceiling": self.hard_ceiling,
            "fallback_used": self.fallback_used,
        }


def compute_context_budget(
    *,
    context_window: int | None,
    reserved_system: int = 0,
    reserved_query: int = 0,
    reserved_output: int = 0,
    reserved_tools: int = 0,
    absolute_fallback: int = 0,
    hard_ceiling: int = 0,
    ratio: float = CONTEXT_BUDGET_RATIO,
) -> BudgetBreakdown:
    """算出本轮 Context Pack 的 Token 上限。

    两种情形：

    * ``context_window`` 未知（非正数）→ 直接走 ``absolute_fallback``（``0`` 表示不设
      预算，即历史行为）。
    * ``context_window`` 已知 → 取「剩余窗口的 80%」，再与 ``hard_ceiling`` 取小。

    ``hard_ceiling`` **不是可选装饰**。只有窗口推导时，一个 131k 窗口的模型会算出约
    100k 的上下文预算——于是「这台模型窗口大」就变成了「这轮可以塞 100k 上下文」，
    成本随之失控，而这跟检索质量毫无关系。运维用 ``RAG_CONTEXT_TOKENS`` 表达的是
    「我最多为上下文付这么多 token」的成本约束，它与窗口约束是**两个独立的限制**，
    取小才是两者同时成立。反过来说，窗口约束仍然是必需的：小窗口模型上 6000 的
    上下文加上输出会直接撑爆窗口。
    """
    reserves = {
        "system": max(0, int(reserved_system or 0)),
        "query": max(0, int(reserved_query or 0)),
        "output": max(0, int(reserved_output or 0)),
        "tools": max(0, int(reserved_tools or 0)),
    }
    total_reserved = sum(reserves.values())
    ceiling = max(0, int(hard_ceiling or 0))

    if not context_window or int(context_window) <= 0:
        return BudgetBreakdown(
            context_window=0,
            reserved_system=reserves["system"],
            reserved_query=reserves["query"],
            reserved_output=reserves["output"],
            reserved_tools=reserves["tools"],
            available=0,
            budget=max(0, int(absolute_fallback or 0)),
            fallback_used=True,
        )

    window = int(context_window)
    available = max(0, window - total_reserved)
    budget = int(available * max(0.0, min(1.0, ratio)))
    if ceiling > 0:
        budget = min(budget, ceiling)
    if 0 < budget < MIN_CONTEXT_BUDGET:
        # 装不下一个分片的预算只会产出半句话的上下文，不如退到绝对兜底（可能是 0 =
        # 不设预算），让下游按分片数与相关度闸门自己收口。
        budget = max(0, int(absolute_fallback or 0))
    return BudgetBreakdown(
        context_window=window,
        reserved_system=reserves["system"],
        reserved_query=reserves["query"],
        reserved_output=reserves["output"],
        reserved_tools=reserves["tools"],
        available=available,
        budget=budget,
        fallback_used=False,
        hard_ceiling=ceiling,
    )
