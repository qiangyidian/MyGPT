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
import re
from dataclasses import dataclass
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


# ---- Code-generation detection (problem 7) -------------------------------- #
# A pure "generate code" request under deep_research should NOT be forced
# through the research Writer (which caps output and writes prose). These
# signals are intentionally CONSERVATIVE so a genuine research question that
# happens to mention "python" or "code" still goes to research.
_CODE_GEN_STRONG = (
    "写代码", "生成代码", "编写代码", "写一段代码", "写个代码",
    "实现一个", "实现完整", "实现一个完整",
    "完整项目", "完整程序", "完整代码", "完整的代码", "完整可运行",
    "写一个完整", "生成一个完整", "写个完整",
    "贪吃蛇", "俄罗斯方块", "扫雷", "五子棋", "2048", "小游戏",
    "写个脚本", "写一个脚本", "生成脚本",
    "write code", "generate code", "write a program", "write a script",
    "implement a", "build a complete",
)
# Soft tokens that *together* with a generation verb imply code.
_CODE_TOKENS = ("代码", "程序", "脚本", "函数", "pygame", "react", "vue")
_CODE_VERBS = ("生成", "写", "编写", "实现", "给", "来", "做", "修复", "重构")
# If present, the user explicitly wants research BEFORE code → keep deep_research.
_RESEARCH_THEN_CODE = (
    "先研究", "先调研", "先分析", "先对比", "最佳实践", "对比方案", "对比不同方案",
)


def looks_like_research_then_code(text: str) -> bool:
    """True when the user explicitly asks to research first, then produce code."""
    if not text:
        return False
    t = text.lower()
    return any(h in t for h in _RESEARCH_THEN_CODE)


def looks_like_code_request(text: str) -> bool:
    """True when a request is fundamentally 'generate code/a program', not research.

    Conservative: generic mentions of python/code do NOT qualify — there must be
    a strong generation signal OR a code token paired with a generation verb.
    """
    if not text:
        return False
    if looks_like_research_then_code(text):
        return False
    t = text.lower()
    if any(h in t for h in _CODE_GEN_STRONG):
        return True
    if any(tok in t for tok in _CODE_TOKENS) and any(v in t for v in _CODE_VERBS):
        return True
    return False


def deliverable_kind(text: str) -> str:
    """Classify the expected output: 'code' | 'document' | 'factual'."""
    if not text:
        return "factual"
    t = text.lower()
    if looks_like_code_request(text):
        return "code"
    # "research then code" → still a code deliverable (research runs, but the
    # final Writer must output complete code, not prose).
    if looks_like_research_then_code(text) and any(tok in t for tok in _CODE_TOKENS):
        return "code"
    if any(h in t for h in ("方案", "文档", "报告", "总结", "草稿", "draft", "report")):
        return "document"
    return "factual"


# --------------------------------------------------------------------------- #
# Multi-agent / debate detection
#
# Prevents "fake multi-agent": a request that asks for several real agents
# (e.g. an A-vs-B debate with a judge) must NOT be answered by a single model
# role-playing in text. These helpers decide when to escalate a request to the
# REAL multi-agent runtime. They are intentionally CONSERVATIVE: a generic
# "比较 A 和 B" with no agent/role/debate signal must NOT trigger, so plain
# comparisons still go to research, not debate.
# --------------------------------------------------------------------------- #
# Explicit signals that the user wants multiple real agents / roles.
_MULTI_AGENT_HINTS = (
    "多agent", "多个agent", "多智能体", "多个智能体", "多个代理", "多个角色",
    "分别扮演", "分别由", "启动多个", "多个子模型", "两个agent", "两个 agent",
    "multi-agent", "multi agent", "multiple agents", "several agents",
    "use multiple agents", "two agents",
)
# Explicit debate / adjudication signals.
_DEBATE_KEYWORDS = (
    "辩论", "辩论赛", "debate", "正方", "反方", "裁判", "judge",
    "pro and con", "pro/con", "supporting side",
)

# Candidate token = a run of chars that are NOT whitespace, common punctuation,
# or the 和/与/跟 separators. Lets us split "A 和 B" / "微服务 与 单体架构" without
# a Chinese segmenter.
_SEP = r"(?:和|与|跟|and|or|还是)"
_TOKEN = r"[^\s,，。、；;：:（）()\"'“”‘’！!?？.]{2,}"
_VS_PATTERN = re.compile(rf"({_TOKEN})\s*(?:vs\.?|versus|对战|对决)\s*({_TOKEN})", re.IGNORECASE)
# "比较/对比/选择 A 和 B" (comparison context before the two candidates)
_AND_BEFORE = re.compile(
    rf"(?:比较|对比|评测|评估|权衡|选择|哪个更好|哪个更适合|区别)[\s\S]{{0,40}}?"
    rf"({_TOKEN})\s*{_SEP}\s*({_TOKEN})"
)
# "A 和 B ... 哪个/更好/比较/区别" (comparison context after the two candidates)
_AND_AFTER = re.compile(
    rf"({_TOKEN})\s*{_SEP}\s*({_TOKEN})[\s\S]{{0,30}}?(?:比较|对比|哪个|更好|区别|选择|优劣)"
)

_CONNECTORS = {"and", "or", "the", "a", "an", "of", "for", "in", "on", "to", "with"}


@dataclass
class DebateSubjects:
    """Two candidates identified for an A-vs-B debate (arbitrary, not hardcoded)."""

    side_a: str
    side_b: str
    criteria: str = ""


def _distinct_pair(a: str, b: str) -> "DebateSubjects | None":
    a, b = a.strip().strip(".,;:()。"), b.strip().strip(".,;:()。")
    if len(a) < 2 or len(b) < 2:
        return None
    if a.lower() == b.lower():
        return None
    if {a.lower(), b.lower()} & _CONNECTORS:
        return None
    return DebateSubjects(side_a=a, side_b=b)


def extract_debate_sides(text: str) -> DebateSubjects | None:
    """Best-effort extraction of two candidates for an A-vs-B debate.

    Returns None if no two distinct candidates can be found. Supports any pair
    (Python vs Go, React vs Vue, 微服务 vs 单体架构, PostgreSQL vs MySQL).
    """
    if not text:
        return None
    s = text.strip()
    for pattern in (_VS_PATTERN, _AND_BEFORE, _AND_AFTER):
        for m in pattern.finditer(s):
            pair = _distinct_pair(m.group(1), m.group(2))
            if pair is not None:
                return pair
    return None


def looks_like_multi_agent_request(text: str) -> bool:
    """True when the user EXPLICITLY asks for multiple real agents / roles.

    Space-tolerant: also checks a whitespace-collapsed copy so "多个 Agent"
    (with a space) matches the "多个agent" hint, just like "多个agent" (no space).
    """
    if not text:
        return False
    t = text.lower()
    t_nospace = re.sub(r"\s+", "", t)
    return (
        any(h in t or h in t_nospace for h in _MULTI_AGENT_HINTS)
        or any(h in t for h in _DEBATE_KEYWORDS)
    )


def looks_like_debate_request(text: str) -> bool:
    """A two-sided multi-agent debate: an explicit agent/role/debate signal AND
    two identifiable candidates (or an explicit debate keyword + candidates)."""
    if not text:
        return False
    sides = extract_debate_sides(text)
    if sides is None:
        return False
    t = text.lower()
    if any(k in t for k in _DEBATE_KEYWORDS):
        return True
    return looks_like_multi_agent_request(text)


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
# Casual / social question detection
#
# Used by ChatService to SKIP RAG retrieval so a knowledge base bound to a
# conversation never leaks into social chit-chat ("你好", "你是谁",
# "你都能干什么", "谢谢", …). The detector is intentionally CONSERVATIVE and
# rule-based: it only fires when the message is dominated by a casual phrase
# (a greeting / capability / thanks / help ask with little else). A real
# question that merely starts with "你好，请帮我…" is NOT casual here — the
# trailing content keeps it out, and the RAG relevance threshold is the
# secondary guard for anything that slips through.
# --------------------------------------------------------------------------- #
# Phrases that, when they dominate the message, mark it as casual. Lowercased;
# matching is done on a whitespace-collapsed, punctuation-stripped core.
_CASUAL_PHRASES = (
    # Greetings
    "你好", "您好", "嗨", "哈喽", "hey", "hi", "hello",
    "早上好", "早安", "早", "下午好", "晚上好", "晚安",
    "在吗", "在不在", "在么",
    # Thanks
    "谢谢", "多谢", "感谢", "谢啦", "辛苦了", "thanks", "thankyou", "thank you", "thx",
    # Capability / self-introduction
    "你是谁", "你叫什么", "你叫啥", "你叫什么名字",
    "你都能干什么", "你能做什么", "你能干什么", "你会什么", "你会做什么",
    "都能做什么", "都能干啥", "能做什么", "会什么",
    "介绍一下自己", "介绍一下你自己", "介绍下自己", "介绍下你自己", "自我介绍",
    "你是什么", "你怎么工作", "你是怎么工作的",
    "你能帮我做什么", "你能帮我干啥", "你会哪些", "你有哪些功能", "你有什么功能",
    "你能做哪些事", "whatcanyoudo", "what can you do", "who are you",
    # Help
    "帮助", "help", "怎么用", "怎么使用", "使用帮助", "使用说明",
    # Short affirmations
    "好的", "好的呀", "好", "嗯", "嗯嗯", "ok", "okay", "收到", "明白了", "知道了",
)
# Particles / punctuation tolerated around a casual phrase (so "你能做什么呢？"
# still counts). Stripped from the head/tail when comparing.
_CASUAL_FILLER = "呢啊呀哦吧嘛呐嘿啦哟咯哈!！?？。.,，、~～、 "
_CASUAL_LEAD = ("请", "帮我", "帮", "你能", "你可以", "能", "可以")


def _casual_core(s: str) -> str:
    """Collapse whitespace + strip casual filler particles from both ends."""
    return s.strip().strip(_CASUAL_FILLER).strip()


def should_recognize_intent(text: str) -> bool:
    """True when a turn is worth a model intent-classification call.

    Skips trivial / casual messages ("你好", "ok", very short turns) so we don't
    spend a classifier call on chit-chat — those stay on the keyword default
    (native). The bar is deliberately low: only obvious non-tasks are skipped.
    """
    t = (text or "").strip()
    if len(t) < 4:
        return False
    if is_casual_question(t):
        return False
    return True


def is_casual_question(text: str) -> bool:
    """True when ``text`` is social/capability chit-chat, not a real query.

    Conservative: returns True only if, after collapsing whitespace and
    stripping filler/leading polite verbs, the message EQUALS a casual phrase
    or STARTS WITH one with at most a tiny non-casual remainder. Also True for
    very short (≤2 meaningful chars) pure chit-chat. A real request that merely
    opens with a greeting is NOT casual.
    """
    if not text:
        return False
    raw = text.strip()
    if not raw:
        return False
    s = re.sub(r"\s+", "", raw).lower()
    core = _casual_core(s)
    if not core:
        return False

    # Strip a single leading polite verb (请/帮我/你能…) so "请介绍一下自己"
    # matches the bare phrase. We test BOTH the original core and the
    # prefix-stripped variant, because "你能做什么" is itself a phrase (matched
    # by the original) while "请介绍一下自己" only matches after the prefix is
    # removed.
    candidates = [core]
    for lead in _CASUAL_LEAD:
        if core.startswith(lead):
            trimmed = _casual_core(core[len(lead):])
            if trimmed and trimmed != core:
                candidates.append(trimmed)
            break

    for cand in candidates:
        for phrase in _CASUAL_PHRASES:
            if cand == phrase:
                return True
            if cand.startswith(phrase):
                remainder = _casual_core(cand[len(phrase):])
                # Casual only if nothing substantive follows the phrase.
                if len(remainder) <= 2:
                    return True

    # Very short pure chit-chat (≤2 meaningful chars), e.g. "嗯", "ok".
    meaningful = re.sub(rf"[{re.escape(_CASUAL_FILLER)}]", "", s)
    if 0 < len(meaningful) <= 2:
        return True
    return False


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
