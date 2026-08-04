"""Behavioral context fragments: personality, per-mode behavior, remaining budget.

Each is a :class:`~app.agents.context_fragments.ContextFragment` so it composes
with the rest of the fragment system and is diffable across turns (world_state).

Inspired by Codex:
  * ``personality_spec`` — a user-tunable persona injected per-conversation
    (Codex's ``/personality``), decoupled from the base system prompt.
  * ``mode_behavior`` — each user-facing mode carries a short behavioral
    directive, so a mode shapes *how* the model works, not just which runtime
    runs (Codex's default/plan/execute/pair_programming templates).
  * ``remaining_budget`` — surface the leftover step/token budget so the model
    self-rations before a hard cutoff (Codex's RolloutBudgetContext).
"""
from __future__ import annotations

from app.agents.context_fragments import ContextFragment

# Per-mode behavioral directives. Adapted from Codex's collaboration templates to
# MyGPT's user-facing modes (auto/search/deep_research/create/data_analysis/debate).
_MODE_BEHAVIORS: dict[str, str] = {
    "auto": (
        "默认模式：直接回答用户问题，需要时调用工具；先给出结论再补必要细节；"
        "不要过度展开或堆砌选项；代码请求直接产出完整可运行代码。"
    ),
    "search": (
        "检索模式：以网络检索为主，给出带来源编号的答案；多来源交叉印证；"
        "区分事实与推测。"
    ),
    "deep_research": (
        "研究模式：多步检索 + 交叉核对 + 带引用的深入调研；先分解子问题再逐一求证；"
        "明确标注证据缺口与不确定性。"
    ),
    "create": (
        "创作模式：长文写作 / 改写 / 总结；不联网；聚焦交付物本身；"
        "按用户要求的体裁与篇幅组织。"
    ),
    "data_analysis": (
        "数据分析模式：分析附件 / 数据；必要时使用 python 沙箱计算；"
        "先给结论与关键数字，再给方法与图表。"
    ),
    "debate": (
        "辩论模式：结构化正方 / 反方 / 裁判；各方论点需有依据；最后由裁判给出综合判断。"
    ),
}
_DEFAULT_MODE_BEHAVIOR = _MODE_BEHAVIORS["auto"]


def mode_behavior_fragment(mode: str) -> ContextFragment:
    """The behavioral directive for the user's selected mode."""
    body = _MODE_BEHAVIORS.get((mode or "auto").strip().lower(), _DEFAULT_MODE_BEHAVIOR)
    return ContextFragment(name="mode_behavior", tag="mode_behavior", body=body)


def personality_fragment(spec: str) -> ContextFragment:
    """A user-requested communication style, applied to subsequent messages.

    Empty/blank spec -> empty body (the differ/assembler drops it).
    """
    body = (spec or "").strip()
    text = (
        f"用户已设定新的沟通风格，后续回答请遵循：\n{body}"
        if body
        else ""
    )
    return ContextFragment(name="personality_spec", tag="personality_spec", body=text)


def remaining_budget_fragment(
    *, remaining_tokens: int | None = None, remaining_steps: int | None = None
) -> ContextFragment:
    """Tell the model how much budget is left so it can self-ration.

    Both args optional; only present fields are mentioned. Empty when neither is
    set (so it's dropped from the prompt).
    """
    parts: list[str] = []
    if remaining_tokens is not None and remaining_tokens >= 0:
        parts.append(f"约 {remaining_tokens} token")
    if remaining_steps is not None and remaining_steps >= 0:
        parts.append(f"{remaining_steps} 步")
    if not parts:
        return ContextFragment(name="remaining_budget", tag="remaining_budget", body="")
    body = (
        "本轮剩余预算：" + " / ".join(parts) + "。若紧张，优先保证核心交付、"
        "减少冗余解释与不必要的工具调用。"
    )
    return ContextFragment(name="remaining_budget", tag="remaining_budget", body=body)
