"""Typed context fragments — the "give the model context to self-judge intent" layer.

Modeled on Codex's ``core/src/context/`` design: each piece of intent-relevant
state becomes a small, focused :class:`ContextFragment` with a stable tag and a
``render()`` that emits a tagged block. The :func:`assemble_context_fragments`
collector runs a registry of fragment builders over an :class:`IntentContextInput`
and returns the non-empty fragments in a fixed order.

Why fragments (not a single prompt string): each fragment has one job, is
unit-testable in isolation, and new ones register by appending a builder to
``_FRAGMENT_BUILDERS`` — the path to Codex-scale (~30 fragments + diffing) is
just "add builders", with no change to the consumers.

Consumers:
  * :class:`~app.agents.intent_service.IntentService` — feeds the rendered
    fragments to the classifier call so it judges intent from real context.
  * the main turn's system prompt — the recognized-intent block (built from the
    classifier output) is appended so the answering model sees what was understood.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable

from app.agents.planning import deliverable_kind


# --------------------------------------------------------------------------- #
# Fragment value object + input
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class ContextFragment:
    """One typed piece of context. ``tag`` is the stable block name the model
    sees (e.g. ``<current_mode>``); ``body`` is the human/agent-readable content.

    The ``tag`` doubles as the fragment's *identity*: the open/close markers it
    produces let later passes detect whether a section's rendered block already
    lives in retained history (used by world-state diffing + compaction retention).
    """

    name: str   # internal id, e.g. "mode" — used in telemetry/fragment lists
    tag: str    # prompt block tag, e.g. "current_mode"
    body: str

    def render(self) -> str:
        """Emit a tagged block, or "" if the body is empty (skipped on render)."""
        body = (self.body or "").strip()
        if not body:
            return ""
        return f"<{self.tag}>\n{body}\n</{self.tag}>"

    def markers(self) -> tuple[str, str]:
        """The stable (open, close) markers for this fragment's rendered block."""
        return (f"<{self.tag}>", f"</{self.tag}>")

    @staticmethod
    def contains_tag(text: str, tag: str) -> bool:
        """True if ``text`` contains a rendered fragment block with this tag.

        Used to recognize a previously-injected fragment inside retained history
        (so diffing/retention can find it without opaque ids).
        """
        return f"<{tag}>" in (text or "")


@dataclass
class IntentContextInput:
    """Primitive inputs available at routing time (before AgentTurnContext).

    Kept deliberately free of ORM/DB objects so the assembler is pure and
    unit-testable. Callers (chat_service) populate the cheap fields they have.
    """

    mode: str = "auto"
    user_content: str = ""
    # Knowledge-base *names* the user bound this turn (empty = none).
    kb_names: tuple[str, ...] = ()
    # Attachment descriptors, e.g. ("sales.csv (csv)", "notes.pdf (pdf)").
    attachment_descriptors: tuple[str, ...] = ()
    # Recent trimmed history (OpenAI message dicts) for a cheap conversation gist.
    messages: list[dict] = field(default_factory=list)
    # Optional project/user-level instructions (AGENTS.md-style). Empty by default.
    user_instructions: str = ""


# --------------------------------------------------------------------------- #
# Fragment builders — each returns a ContextFragment (body may be "").
# --------------------------------------------------------------------------- #
def _mode_fragment(inp: IntentContextInput) -> ContextFragment:
    mode = (inp.mode or "auto").strip().lower() or "auto"
    return ContextFragment(
        name="mode",
        tag="current_mode",
        body=f"用户当前选择的模式：{mode}",
    )


def _deliverable_seed_fragment(inp: IntentContextInput) -> ContextFragment:
    """A rule-based *hint* of the deliverable kind — a SEED for the classifier,
    never the final answer (the model is). Cheap and deterministic."""
    kind = deliverable_kind(inp.user_content)
    return ContextFragment(
        name="deliverable_seed",
        tag="deliverable_seed",
        body=f"规则初判的交付类型（仅参考，可被推翻）：{kind}",
    )


def _environment_fragment(inp: IntentContextInput) -> ContextFragment:
    parts: list[str] = []
    if inp.kb_names:
        parts.append("已绑定知识库：" + "、".join(inp.kb_names))
    if inp.attachment_descriptors:
        parts.append("附件：" + "、".join(inp.attachment_descriptors))
    if not parts:
        parts.append("无绑定的知识库或附件")
    return ContextFragment(
        name="environment",
        tag="environment",
        body="；".join(parts),
    )


def _conversation_gist_fragment(inp: IntentContextInput) -> ContextFragment:
    """A cheap, no-LLM gist of the recent back-and-forth so the classifier can
    tell a follow-up ("那再写个测试") from a fresh request. Uses only the last
    few user turns, capped."""
    user_turns: list[str] = []
    for m in inp.messages:
        if (m.get("role") == "user") and isinstance(m.get("content"), str):
            text = m["content"].strip().replace("\n", " ")
            if text:
                user_turns.append(text[:120])
    # Keep only the most recent few so the gist stays small.
    user_turns = user_turns[-3:]
    if not user_turns:
        return ContextFragment(name="conversation_gist", tag="conversation_gist", body="")
    return ContextFragment(
        name="conversation_gist",
        tag="conversation_gist",
        body="近期用户消息（用于判断是否是追问）：\n- " + "\n- ".join(user_turns),
    )


def _user_instructions_fragment(inp: IntentContextInput) -> ContextFragment:
    return ContextFragment(
        name="user_instructions",
        tag="user_instructions",
        body=(inp.user_instructions or "").strip(),
    )


# Ordered registry. Append a builder here to add a fragment — no other change.
_FRAGMENT_BUILDERS: tuple[Callable[[IntentContextInput], ContextFragment], ...] = (
    _mode_fragment,
    _deliverable_seed_fragment,
    _environment_fragment,
    _conversation_gist_fragment,
    _user_instructions_fragment,
)


# --------------------------------------------------------------------------- #
# Assembly
# --------------------------------------------------------------------------- #
def assemble_context_fragments(inp: IntentContextInput) -> list[ContextFragment]:
    """Run every registered builder over ``inp`` and return the non-empty ones.

    Empty-body fragments are dropped so the classifier prompt stays compact and
    only carries real signal (codex's diffing idea in miniature).
    """
    out: list[ContextFragment] = []
    for build in _FRAGMENT_BUILDERS:
        frag = build(inp)
        if frag.body.strip():
            out.append(frag)
    return out


def render_fragments(fragments: list[ContextFragment]) -> str:
    """Render a fragment list as ``\\n\\n``-joined tagged blocks (empty-aware)."""
    rendered = [f.render() for f in fragments]
    return "\n\n".join(r for r in rendered if r)


def fragment_names(fragments: list[ContextFragment]) -> list[str]:
    """The ``name`` of each fragment — for telemetry / the intent_recognized event."""
    return [f.name for f in fragments]


def recognized_intent_fragment(intent_decision: object) -> ContextFragment:
    """Render the classifier's verdict as a fragment the answering model sees.

    This is what makes the model "self-judge intent" (codex's core idea): instead
    of a hidden keyword router deciding the pipeline, the recognized intent is
    surfaced to the model so it organizes the answer accordingly.
    """
    body = (
        f"系统识别到的意图：route={getattr(intent_decision, 'route', 'native')}, "
        f"deliverable_kind={getattr(intent_decision, 'deliverable_kind', 'factual')}, "
        f"confidence={float(getattr(intent_decision, 'confidence', 0.0)):.2f}\n"
        f"理由：{getattr(intent_decision, 'rationale', '') or '（未给出）'}\n"
        "请据此组织回答：代码请求请直接产出完整可运行代码；研究类请求请带来源编号。"
    )
    return ContextFragment(name="recognized_intent", tag="recognized_intent", body=body)
