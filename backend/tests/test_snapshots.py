"""Golden-snapshot tests for prompt/context assembly (Codex insta pattern).

These lock the rendered shape of fragments + diffing so any change to prompt
assembly is caught and must be reviewed (delete the .snap or SNAP_UPDATE=1).
"""
from __future__ import annotations

from app.agents.answer_format import answer_format_fragment
from app.agents.behavior_fragments import (
    mode_behavior_fragment,
    multi_agent_mode_fragment,
    personality_fragment,
    realtime_delegation_fragment,
)
from app.agents.context_fragments import (
    IntentContextInput,
    assemble_context_fragments,
    render_fragments,
)
from app.agents.schemas import IntentDecision
from app.agents.context_fragments import recognized_intent_fragment

from tests._snapshot import assert_snapshot


def test_snapshot_answer_format_guide():
    assert_snapshot("answer_format_guide", answer_format_fragment().render())


def test_snapshot_mode_behaviors():
    from app.agents.behavior_fragments import _MODE_BEHAVIORS  # noqa
    out = "\n\n".join(
        f"### {mode}\n{mode_behavior_fragment(mode).render()}"
        for mode in ("auto", "search", "deep_research", "create", "data_analysis", "debate")
    )
    assert_snapshot("mode_behaviors", out)


def test_snapshot_recognized_intent_block():
    d = IntentDecision(route="native", deliverable_kind="code", confidence=0.95, rationale="写代码请求")
    assert_snapshot("recognized_intent_code", recognized_intent_fragment(d).render())


def test_snapshot_full_fragment_assembly():
    frags = assemble_context_fragments(
        IntentContextInput(
            mode="deep_research",
            user_content="用 Python 写一个贪吃蛇游戏",
            kb_names=("产品手册",),
            attachment_descriptors=("data.csv (csv)",),
            messages=[{"role": "user", "content": "先写个测试"}],
        )
    )
    assert_snapshot("full_assembly", render_fragments(frags))


def test_snapshot_multi_agent_mode_and_delegation():
    out = "\n\n".join([
        multi_agent_mode_fragment("explicit").render(),
        multi_agent_mode_fragment("proactive").render(),
        personality_fragment("简洁、技术、少客套").render(),
        realtime_delegation_fragment(user_input="继续重构", transcript_delta="user: 上一步改了 X").render(),
    ])
    assert_snapshot("behavior_extras", out)
