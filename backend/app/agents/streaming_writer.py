"""StreamingWriterExecutor — makes the CrewAI Writer stage emit real
token-by-token SSE deltas instead of one bulk token event.

Why this exists: the legacy path ran the Writer via ``Agent.aexecute_task``
(blocking) and re-emitted the whole final string as a single ``token`` event
(see ``crewai_runtime._run_multi_agent``). That defeated streaming for the
multi-agent path even though the Native runtime already streamed. This
executor calls ``provider.stream_chat`` directly for the writer agent and
forwards each delta as an ``ev_token`` through the shared StageContext queue,
so tokens interleave with the writer's ``agent_status:running`` event in real
time — genuine streaming, never a chunked-string fake.

Contract with the runtime
-------------------------
* Only the writer stage streams; every other ``agent_id`` delegates to the
  wrapped base executor (``CrewAIStageExecutor``), so evidence/analyst
  behaviour is unchanged.
* The assistant message content is mutated incrementally (like the Native
  runtime does), so a mid-stream cancel still leaves partial text durable.
* ``StageResult.raw`` is set to ``""`` after streaming and
  ``stage_ctx.writer_streamed`` is set, so the runtime's post-loop collector
  neither re-emits a duplicate token nor overwrites the streamed content.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any

from app.agents.runtime.stage_executor import StageExecutor, StageResult
from app.agents.run_controls import get as get_run_control
from app.agents.schemas import ev_token
from app.agents.stage_context import StageContext
from app.providers.base import ChatOptions, ProviderError

logger = logging.getLogger(__name__)

# Agent ids that should stream their output (the final answer stage).
_WRITER_AGENT_IDS = {"writer"}

_SYSTEM = (
    "你是研究团队的撰稿人。请严格基于分析师已验证的内容回答用户问题，"
    "并保留 [source N] 来源编号。\n"
    "要求：\n"
    "1) 只使用下方已验证内容，不补充未经验证的事实；\n"
    "2) 不暴露内部推理过程，直接给出结论与依据；\n"
    "3) 使用用户的语言回答，按需要组织格式与篇幅；\n"
    "4) 若已验证内容不足以回答，明确说明缺口。"
)

# Writer prompt for a CODE deliverable (research-then-code requests). The
# research-prose prompt would truncate code and demand per-line citations; this
# one prioritizes complete, runnable code over narration.
_CODE_SYSTEM = (
    "你是团队的交付工程师。请基于分析师已验证的研究结论，直接产出完整、可运行的代码交付物。\n"
    "要求：\n"
    "1) 先输出完整代码（代码块必须闭合），再给必要的运行说明；\n"
    "2) 允许进行合理的代码结构组织；不要求每一行代码都有来源引用；\n"
    "3) 仅对外部事实或第三方 API 约束引用来源编号 [source N]，代码本身无需逐行引用；\n"
    "4) 不要只写“我来帮你实现”之类的开场白后结束；\n"
    "5) 若输出预算（max_tokens）紧张，优先保证代码完整性，减少解释性文字；\n"
    "6) 使用用户的语言。"
)


class StreamingWriterExecutor:
    """Wraps a base executor; streams the writer stage, delegates the rest.

    Implemented as a duck-typed :class:`StageExecutor` (the protocol is
    runtime_checkable and structural) so it slots into the existing executor
    factory without changing the ``StageExecutor`` signature.
    """

    def __init__(self, base: StageExecutor) -> None:
        self._base = base

    async def execute(
        self,
        *,
        agent_id: str,
        agent: Any,
        task: Any,
        context: str | None,
        stage_ctx: StageContext,
    ) -> StageResult:
        if agent_id in _WRITER_AGENT_IDS and self._can_stream(stage_ctx):
            return await self._stream_writer(agent_id, task, context, stage_ctx)
        return await self._base.execute(
            agent_id=agent_id,
            agent=agent,
            task=task,
            context=context,
            stage_ctx=stage_ctx,
        )

    @staticmethod
    def _can_stream(stage_ctx: StageContext) -> bool:
        provider = getattr(stage_ctx, "provider", None)
        return provider is not None and getattr(stage_ctx, "assistant_msg", None) is not None

    async def _stream_writer(
        self,
        agent_id: str,
        task: Any,
        context: str | None,
        stage_ctx: StageContext,
    ) -> StageResult:
        provider = stage_ctx.provider
        assistant_msg = stage_ctx.assistant_msg
        # The user's literal request is the authoritative question; the CrewAI
        # task.description is only a synthesized framing fallback (it used to be
        # preferred, which mangled code-generation prompts).
        question = (
            getattr(stage_ctx, "user_content", "") or getattr(task, "description", "") or ""
        ).strip()
        verified = (context or "").strip()

        # Intent-driven prompt: the Writer must shape its answer to what the user
        # actually asked, not always "base the answer on verified content".
        # deliverable_kind is the intent signal (code vs research/document). For a
        # code request the research framing makes the Writer echo the Analyst's
        # architecture prose instead of writing the program — so the user prompt
        # follows the detected intent.
        from app.agents.planning import deliverable_kind

        kind = deliverable_kind(getattr(stage_ctx, "user_content", "") or question)
        system_prompt = _CODE_SYSTEM if kind == "code" else _SYSTEM
        if kind == "code":
            # Code intent: write the program. The Analyst's research is reference
            # only — never restate it as prose (that is what produced
            # "architecture-only" answers).
            user_prompt = (
                f"用户问题：\n{question}\n\n"
                f"（可选参考）研究结论：\n{verified or '（无）'}\n\n"
                "请针对用户问题直接产出完整、可运行的代码交付物；"
                "上面的研究结论仅作参考，不要复述，除非涉及外部事实或第三方 API 约束。"
            )
        else:
            # Research / document intent: answer from verified evidence, cite it.
            user_prompt = (
                f"用户问题：\n{question}\n\n"
                f"已验证内容：\n{verified or '（无已验证内容，请如实说明缺口）'}\n\n"
                "请基于以上已验证内容生成最终回答，并保留来源编号。"
            )
        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ]
        # Do NOT cap the Writer's output (max_tokens=None). A capped budget — even
        # the user's ModelConfig.max_tokens — truncated long code answers
        # mid-stream (finish_reason=length): a multi-agent "编写贪吃蛇游戏" request
        # spent the budget on the Analyst's architecture preamble and the game
        # code never reached the user. None omits max_tokens so the endpoint
        # streams until the model naturally stops.
        options = ChatOptions(temperature=0.5, max_tokens=None)

        stage_ctx.set_stage(agent_id=agent_id, task_id=getattr(task, "id", "") or "writer")

        # Some OpenAI-compatible gateways (e.g. the GLM proxy) occasionally
        # return an EMPTY stream for an otherwise-valid prompt (a transient
        # blank). provider.stream_chat does NOT error on empty — it just yields
        # no content — so without a retry the writer would finish "green" with a
        # blank answer. Retry once so a transient blank doesn't cost the answer.
        accumulated: list[str] = []
        finish_reason = "stop"
        for attempt in (1, 2):
            accumulated = []
            finish_reason = "stop"
            try:
                async for delta in provider.stream_chat(messages, options):
                    ctl = get_run_control(stage_ctx.run_id)
                    # Cooperative cancel between chunks: the Stop button / cancel
                    # API sets ctl.cancel (the real signal). cancel_event is a
                    # defensive secondary check. Honor either -> cancelled.
                    cancel_evt = getattr(stage_ctx, "cancel_event", None)
                    if (ctl is not None and ctl.cancel.is_set()) or (
                        cancel_evt is not None and cancel_evt.is_set()
                    ):
                        finish_reason = "cancelled"
                        break
                    # Phase 2: honor a user-initiated pause mid-stream.
                    if ctl is not None:
                        while ctl.is_paused() and not ctl.cancel.is_set():
                            await asyncio.sleep(0.1)
                        if ctl.cancel.is_set():
                            finish_reason = "cancelled"
                            break
                    if delta.content:
                        accumulated.append(delta.content)
                        # Mutate incrementally so partial content survives cancel.
                        assistant_msg.content = "".join(accumulated)
                        stage_ctx.emit(ev_token(delta=delta.content))
                    if delta.finish_reason:
                        finish_reason = delta.finish_reason
            except asyncio.CancelledError:
                assistant_msg.content = "".join(accumulated)
                stage_ctx.writer_streamed = True
                raise
            except ProviderError:
                logger.warning("writer stream provider error", exc_info=True)
                raise
            except Exception:
                logger.exception("writer streaming failed")
                raise
            # Done if we got content, or the user cancelled.
            if accumulated or finish_reason == "cancelled":
                break
            if attempt == 1:
                logger.info("writer stream returned empty; retrying once")

        stage_ctx.writer_streamed = True
        full = "".join(accumulated)
        assistant_msg.content = full
        # raw="" → runtime must NOT re-emit a bulk token. output_summary feeds
        # the activity feed / agent node card.
        return StageResult(
            agent_id=agent_id,
            raw="",
            output_summary=(full.strip().replace("\n", " ")[:160] or "（空回答）"),
            structured={"finish_reason": finish_reason, "streamed": True},
        )
