"""Context fragments: typed intent-context assembly (the "give the model context"
layer, modeled on Codex's core/src/context/). Pure + deterministic — no LLM."""
from __future__ import annotations

from app.agents.context_fragments import (
    ContextFragment,
    IntentContextInput,
    assemble_context_fragments,
    fragment_names,
    recognized_intent_fragment,
    render_fragments,
)
from app.agents.schemas import IntentDecision


def test_mode_fragment_carries_selected_mode():
    frags = assemble_context_fragments(IntentContextInput(mode="deep_research", user_content="x"))
    mode = next(f for f in frags if f.name == "mode")
    assert "deep_research" in mode.body


def test_deliverable_seed_reflects_code_request():
    frags = assemble_context_fragments(IntentContextInput(user_content="用 Python 写一个贪吃蛇游戏"))
    seed = next(f for f in frags if f.name == "deliverable_seed")
    assert "code" in seed.body
    # It is explicitly framed as a hint the classifier may override.
    assert "参考" in seed.body


def test_environment_fragment_lists_kb_and_attachments():
    frags = assemble_context_fragments(
        IntentContextInput(
            kb_names=("产品手册", "FAQ"),
            attachment_descriptors=("sales.csv (csv)",),
        )
    )
    env = next(f for f in frags if f.name == "environment")
    assert "产品手册" in env.body and "FAQ" in env.body and "sales.csv" in env.body


def test_environment_fragment_when_empty():
    frags = assemble_context_fragments(IntentContextInput())
    env = next(f for f in frags if f.name == "environment")
    assert "无绑定" in env.body


def test_conversation_gist_uses_last_user_turns_and_caps():
    messages = [
        {"role": "user", "content": "第一句"},
        {"role": "assistant", "content": "答"},
        {"role": "user", "content": "第二句"},
        {"role": "user", "content": "第三句"},
        {"role": "user", "content": "第四句"},
    ]
    frags = assemble_context_fragments(IntentContextInput(messages=messages))
    gist = next(f for f in frags if f.name == "conversation_gist")
    # Only the most recent 3 user turns are kept -> the oldest is dropped.
    assert "第一句" not in gist.body
    assert "第二句" in gist.body and "第三句" in gist.body and "第四句" in gist.body


def test_assembler_drops_empty_fragments():
    # user_instructions defaults to "" -> its fragment must be dropped.
    frags = assemble_context_fragments(IntentContextInput(user_content="写个脚本"))
    names = fragment_names(frags)
    assert "user_instructions" not in names
    # But the others are present.
    assert "mode" in names and "deliverable_seed" in names


def test_render_fragments_emits_tagged_blocks():
    frags = assemble_context_fragments(
        IntentContextInput(mode="auto", user_content="写贪吃蛇", kb_names=("KB1",))
    )
    rendered = render_fragments(frags)
    assert "<current_mode>" in rendered and "</current_mode>" in rendered
    assert "<environment>" in rendered
    assert "<recognized_intent>" not in rendered  # not assembled here


def test_fragment_render_empty_body_returns_empty_string():
    frag = ContextFragment(name="x", tag="x", body="   ")
    assert frag.render() == ""


def test_recognized_intent_fragment_renders_verdict():
    decision = IntentDecision(
        route="native", deliverable_kind="code", confidence=0.93, rationale="写代码请求"
    )
    body = recognized_intent_fragment(decision).render()
    assert "route=native" in body
    assert "deliverable_kind=code" in body
    assert "0.93" in body
    assert "写代码请求" in body
    assert "<recognized_intent>" in body
