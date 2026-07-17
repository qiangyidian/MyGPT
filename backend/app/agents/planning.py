"""Planning primitives: intent classification, plan construction, summarization.

Phase 2 keeps these deterministic and testable:
  * ``classify_intent`` is rule-based (keyword signals) so it works without an
    LLM and is stable in tests. An LLM-based classifier can refine this later.
  * ``build_plan`` returns a short structured plan per intent — emitted as the
    ``plan_created`` event so the frontend shows the execution trace.
  * ``summarize_history`` calls the provider's non-streaming ``chat`` to roll
    older messages into a summary (falls back to a heuristic if the provider
    can't produce one), which ``state_store`` then persists.
"""
from __future__ import annotations

import logging
from typing import Any

from app.providers.base import ChatOptions, ModelProvider

logger = logging.getLogger(__name__)

# Keyword signals (lowercased substring match) for rule-based intent routing.
_RESEARCH_HINTS = (
    "研究", "调研", "对比", "比较", "分析", "综述", "总结", "权衡",
    "research", "compare", "analyze", "investigate", "deep dive",
)
_ACTION_HINTS = (
    "发送", "创建", "删除", "修改", "执行", "运行", "更新", "插入",
    "send", "create", "delete", "update", "run", "execute", "insert", "drop",
)
_KNOWLEDGE_HINTS = (
    "搜索", "查找", "查询", "什么是", "搜一下", "查一下",
    "search", "find", "look up", "what is",
)

Intent = str  # "chat" | "knowledge" | "deep_research" | "action"


def classify_intent(text: str) -> Intent:
    """Rule-based intent classification. Deterministic + testable."""
    if not text:
        return "chat"
    t = text.lower()
    if any(h in t for h in _ACTION_HINTS):
        return "action"
    if any(h in t for h in _RESEARCH_HINTS):
        return "deep_research"
    if any(h in t for h in _KNOWLEDGE_HINTS):
        return "knowledge"
    return "chat"


def build_plan(intent: Intent, user_goal: str) -> tuple[str, list[dict[str, Any]]]:
    """Return (summary, steps) for the ``plan_created`` event."""
    goal = (user_goal or "").strip()
    if intent == "deep_research":
        return (
            "先检索资料，再交叉核对来源，最后生成带引用的汇总",
            [
                {"id": "1", "title": "检索相关资料"},
                {"id": "2", "title": "核对来源与差异"},
                {"id": "3", "title": "生成带引用的汇总"},
            ],
        )
    if intent == "action":
        return (
            "确认操作意图，执行操作（高风险需确认），反馈结果",
            [
                {"id": "1", "title": "确认操作意图与参数"},
                {"id": "2", "title": "执行操作（需人工确认）"},
                {"id": "3", "title": "反馈执行结果"},
            ],
        )
    if intent == "knowledge":
        return (
            "检索知识库并组织回答",
            [
                {"id": "1", "title": "检索知识库"},
                {"id": "2", "title": "组织回答"},
            ],
        )
    return "直接回答用户", [{"id": "1", "title": "回答用户"}]


def extract_goal(text: str) -> str:
    """Derive a concise user goal from the latest user message."""
    t = (text or "").strip().replace("\n", " ")
    return t[:200]


# --------------------------------------------------------------------------- #
# Rolling summary
# --------------------------------------------------------------------------- #
_SUMMARY_SYSTEM = (
    "You are a conversation summarizer. Summarize the conversation so far in "
    "<= 300 words, preserving: the user's goal, established facts, decisions "
    "made, open questions, and any constraints. Do not invent details."
)


def should_summarize(total_tokens: int, max_tokens: int) -> bool:
    """True when the prompt is using >70% of its budget — time to roll up."""
    return max_tokens > 0 and total_tokens > max_tokens * 0.7


async def summarize_history(
    provider: ModelProvider,
    messages: list[dict[str, Any]],
    *,
    keep_recent: int = 6,
) -> str:
    """Summarize older messages into a compact string.

    Keeps the ``keep_recent`` most recent messages intact; the older prefix is
    sent to the model for summarization. Falls back to a heuristic join if the
    provider call fails (so RAG/chat never hard-depends on summarization).
    """
    if len(messages) <= keep_recent:
        return ""

    older = messages[:-keep_recent]
    transcript = _flatten(older)
    if not transcript.strip():
        return ""

    try:
        result = await provider.chat(
            [{"role": "system", "content": _SUMMARY_SYSTEM},
             {"role": "user", "content": transcript}],
            ChatOptions(temperature=0.2, max_tokens=400),
        )
        summary = (result.content or "").strip()
        if summary:
            return summary
    except Exception as exc:  # noqa: BLE001 — summarization is best-effort
        logger.warning("history summarization failed, using heuristic: %s", exc)

    # Heuristic fallback: truncate the flattened older transcript.
    return transcript[:1200] + ("…" if len(transcript) > 1200 else "")


def _flatten(messages: list[dict[str, Any]]) -> str:
    parts: list[str] = []
    for m in messages:
        role = m.get("role", "?")
        content = m.get("content")
        if isinstance(content, list):
            content = " ".join(
                p.get("text", "") for p in content if isinstance(p, dict)
            )
        if content:
            parts.append(f"{role}: {content}")
    return "\n".join(parts)
