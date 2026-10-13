"""回合门禁的验收测试（风险分档 + 流式/缓冲的接线）。

这一层不改检索质量，它改的是**用户什么时候看到答案**——而「拒答」这个动作能不能成立
完全取决于此：缓冲档下答案在校验通过前从不交给前端，流式档下已经吐出去的 token 收不
回来。所以测试的重点是事件流的形状：哪些帧被扣住、``validation`` 帧出现在哪一帧之前。
"""
from __future__ import annotations

import uuid


from app.agents.schemas import AgentEvent
from app.core.config import get_settings
from app.rag.contracts import new_trace_id
from app.services.answer_gate import (
    REFUSAL_HIGH_RISK,
    REFUSAL_INSUFFICIENT_EVIDENCE,
    GateResult,
    context_budget_for,
    open_turn_gate,
    stream_gated,
)

SEEDED_USER = uuid.UUID("00000000-0000-0000-0000-000000000001")


class _Cfg:
    """一份够用的模型配置替身（只喂 capabilities_from_config）。"""

    max_context_tokens = 32768
    max_tokens = 4096
    output_token_parameter = "max_tokens"
    supports_tools = False
    supports_parallel_tools = False
    supports_vision = False
    supports_audio_input = False
    model_name = "mock"


def _outcome(answer: str, status: str = "answered", action: str = "return_answer"):
    """一个真实的 ``GateResult``（用真的 PostprocessOutcome，不用桩）。

    ``GateResult.event`` 会读 ``outcome.validation`` —— 给它一个桩就等于把这一层的
    真实形状替换掉了，测出来的事件流能不能用也就没人知道。
    """
    from app.rag.contracts import ValidationResult
    from app.rag.postprocess import PostprocessOutcome

    validation = ValidationResult(
        request_id="req-1", trace_id="tr-1", status="pass", action=action  # type: ignore[arg-type]
    )
    return GateResult(
        answer=answer,
        status=status,  # type: ignore[arg-type]
        outcome=PostprocessOutcome(
            answer=answer, status=status, claims=(), validation=validation  # type: ignore[arg-type]
        ),
        refused=status in ("refused", "insufficient_evidence"),
    )


async def _collect(events, *, gate, finalize):
    """``events`` 既可以是列表也可以是异步生成器。"""
    async def _source():
        if hasattr(events, "__aiter__"):
            async for item in events:
                yield item
        else:
            for item in events:
                yield item

    return [e async for e in stream_gated(_source(), gate=gate, finalize=finalize)]


def _tok(text: str) -> AgentEvent:
    return AgentEvent(kind="token", data={"delta": text})


def _done() -> AgentEvent:
    return AgentEvent(kind="done", data={"finish_reason": "stop", "usage": {}})


# --------------------------------------------------------------------------- #
# 开门
# --------------------------------------------------------------------------- #
async def test_an_ordinary_question_streams():
    gate = await open_turn_gate(question="知识库怎么上传文档", cfg=_Cfg(), kb_ids=["kb"])
    assert gate.buffered is False
    assert gate.risk.is_high_risk is False


async def test_a_high_risk_question_with_a_kb_is_buffered():
    gate = await open_turn_gate(question="这次的调薪方案怎么定", cfg=_Cfg(), kb_ids=["kb"])
    assert gate.buffered is True


async def test_a_high_risk_question_without_a_kb_still_streams():
    """没有知识库就没有引用要校验，扣住答案只是白白增加首字延迟。"""
    gate = await open_turn_gate(question="这次的调薪方案怎么定", cfg=_Cfg(), kb_ids=[])
    assert gate.buffered is False
    assert gate.risk.is_high_risk is True


async def test_buffering_can_be_switched_off(monkeypatch):
    monkeypatch.setattr(get_settings(), "RAG_HIGH_RISK_BUFFER", False)
    gate = await open_turn_gate(question="调薪方案", cfg=_Cfg(), kb_ids=["kb"])
    assert gate.buffered is False


async def test_sampling_can_be_injected_instead_of_rolled():
    yes = await open_turn_gate(question="x", cfg=_Cfg(), kb_ids=[], sampled=True)
    no = await open_turn_gate(question="x", cfg=_Cfg(), kb_ids=[], sampled=False)
    assert yes.grounding_sampled is True and no.grounding_sampled is False


async def test_the_trace_id_can_be_supplied_so_it_matches_the_retrieval_trace():
    tid = new_trace_id()
    gate = await open_turn_gate(question="x", cfg=_Cfg(), kb_ids=[], trace_id=tid)
    assert gate.trace_id == tid


# --------------------------------------------------------------------------- #
# 生成前拒答
# --------------------------------------------------------------------------- #
async def test_nothing_is_refused_before_retrieval_has_run():
    gate = await open_turn_gate(question="x", cfg=_Cfg(), kb_ids=["kb"], kb_explicit=True)
    assert gate.pre_generation_refusal() is None


async def test_an_empty_retrieval_on_an_explicit_kb_refuses():
    gate = await open_turn_gate(question="x", cfg=_Cfg(), kb_ids=["kb"], kb_explicit=True)
    gate.rag_attempted = True
    gate.retrieval_insufficient = True
    assert gate.pre_generation_refusal() == REFUSAL_INSUFFICIENT_EVIDENCE


async def test_an_empty_retrieval_on_an_implicit_kb_is_allowed_to_answer():
    gate = await open_turn_gate(question="x", cfg=_Cfg(), kb_ids=["kb"], kb_explicit=False)
    gate.rag_attempted = True
    gate.retrieval_insufficient = True
    assert gate.pre_generation_refusal() is None


async def test_an_empty_retrieval_on_a_high_risk_turn_refuses():
    gate = await open_turn_gate(question="调薪方案怎么定", cfg=_Cfg(), kb_ids=["kb"])
    gate.rag_attempted = True
    gate.retrieval_insufficient = True
    assert gate.pre_generation_refusal() == REFUSAL_HIGH_RISK


async def test_a_healthy_retrieval_never_refuses():
    gate = await open_turn_gate(question="x", cfg=_Cfg(), kb_ids=["kb"], kb_explicit=True)
    gate.rag_attempted = True
    gate.retrieval_insufficient = False
    assert gate.pre_generation_refusal() is None


# --------------------------------------------------------------------------- #
# 事件流
# --------------------------------------------------------------------------- #
async def test_streaming_forwards_tokens_and_puts_validation_before_done():
    gate = await open_turn_gate(question="知识库怎么用", cfg=_Cfg(), kb_ids=["kb"])

    async def finalize(answer, finish, usage):
        assert answer == "你好世界"  # 累计到的正文必须是模型真正产出的
        return _outcome(answer)

    events = await _collect(
        [_tok("你好"), _tok("世界"), _done()], gate=gate, finalize=finalize
    )
    kinds = [e.kind for e in events]
    assert kinds == ["token", "token", "validation", "done"]
    assert gate.result is not None


async def test_buffering_withholds_tokens_until_validation_passes():
    gate = await open_turn_gate(question="这次的调薪方案怎么定", cfg=_Cfg(), kb_ids=["kb"])
    assert gate.buffered is True

    async def finalize(answer, finish, usage):
        assert answer == "完整答案"
        return _outcome("完整答案")

    events = await _collect(
        [_tok("完整"), _tok("答案"), _done()], gate=gate, finalize=finalize
    )
    kinds = [e.kind for e in events]
    # 没有任何逐字 token 帧先于 validation —— 这正是「拒答」能成立的前提。
    assert kinds == ["validation", "token", "done"]
    assert events[1].data["delta"] == "完整答案"
    assert events[1].data["replace"] is True


async def test_buffering_emits_the_refusal_instead_of_the_answer():
    gate = await open_turn_gate(question="这次的调薪方案怎么定", cfg=_Cfg(), kb_ids=["kb"])

    async def finalize(answer, finish, usage):
        return _outcome(REFUSAL_HIGH_RISK, status="refused")

    events = await _collect([_tok("不该被看到的答案"), _done()], gate=gate, finalize=finalize)
    assert [e.kind for e in events] == ["validation", "token", "done"]
    assert events[1].data["delta"] == REFUSAL_HIGH_RISK
    assert "不该被看到" not in events[1].data["delta"]


async def test_a_streamed_answer_that_gets_refused_is_replaced_not_appended():
    """流式档下原文已经在屏幕上了，拒答只能顶掉它。

    这一帧必须是**替换**语义：前端的校验结论帧已经把那句拒答文案渲染过一遍，追加
    就会让用户读到两遍同样的话。
    """
    gate = await open_turn_gate(question="知识库怎么用", cfg=_Cfg(), kb_ids=["kb"])
    assert gate.buffered is False

    async def finalize(answer, finish, usage):
        return _outcome(REFUSAL_HIGH_RISK, status="refused")

    events = await _collect([_tok("原文"), _done()], gate=gate, finalize=finalize)
    assert [e.kind for e in events] == ["token", "validation", "token", "done"]
    assert events[0].data == {"delta": "原文"}  # 原样转发，没有 replace
    assert events[2].data["delta"] == REFUSAL_HIGH_RISK
    assert events[2].data["replace"] is True


async def test_a_clean_streamed_answer_sends_no_replacement_frame():
    """没被改写的回合不该多发一帧：多出来的帧会让「原文 + 整段」变成重复文本。"""
    gate = await open_turn_gate(question="知识库怎么用", cfg=_Cfg(), kb_ids=["kb"])

    async def finalize(answer, finish, usage):
        return _outcome(answer)

    events = await _collect([_tok("原"), _tok("样"), _done()], gate=gate, finalize=finalize)
    assert [e.kind for e in events] == ["token", "token", "validation", "done"]


async def test_intermediate_events_are_forwarded_even_when_buffering():
    """用户仍然要看得到进度；被扣住的只有答案正文。"""
    gate = await open_turn_gate(question="调薪方案", cfg=_Cfg(), kb_ids=["kb"])

    async def finalize(answer, finish, usage):
        return _outcome("答案")

    events = await _collect(
        [
            AgentEvent(kind="step_started", data={"stepId": "s1", "title": "检索"}),
            _tok("答"),
            _done(),
        ],
        gate=gate,
        finalize=finalize,
    )
    assert events[0].kind == "step_started"


async def test_without_a_gate_the_stream_is_untouched():
    seen: list[tuple] = []

    async def finalize(answer, finish, usage):
        seen.append((answer, finish, usage))

    events = await _collect([_tok("原"), _tok("样"), _done()], gate=None, finalize=finalize)
    assert [e.kind for e in events] == ["token", "token", "done"]
    assert seen[0][0] == "原样"


async def test_the_finish_reason_and_usage_reach_the_gate():
    gate = await open_turn_gate(question="x", cfg=_Cfg(), kb_ids=[])
    captured: dict = {}

    async def finalize(answer, finish, usage):
        captured.update(finish=finish, usage=usage)

    async def source():
        yield _tok("a")
        yield AgentEvent(
            kind="done", data={"finish_reason": "length", "usage": {"input_tokens": 7}}
        )

    await _collect(source(), gate=gate, finalize=finalize)
    assert captured["finish"] == "length"
    assert captured["usage"] == {"input_tokens": 7}


# --------------------------------------------------------------------------- #
# 预算
# --------------------------------------------------------------------------- #
def test_the_budget_comes_from_the_generation_model_window():
    b = context_budget_for(_Cfg(), reserved_system=0, reserved_query=0)
    assert b.context_window == 32768
    assert b.hard_ceiling == get_settings().RAG_CONTEXT_TOKENS
    assert b.budget <= b.hard_ceiling


def test_a_tiny_window_lowers_the_budget_below_the_operator_ceiling():
    class _Tiny(_Cfg):
        max_context_tokens = 8192

    b = context_budget_for(_Tiny(), reserved_system=0, reserved_query=0)
    assert b.budget < get_settings().RAG_CONTEXT_TOKENS
