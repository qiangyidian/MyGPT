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

from app.agents.continuation import (
    ContinuationBuffer,
    ContinuationPolicy,
    aggregate_usage,
)
from app.agents.run_controls import get as get_run_control
from app.agents.runtime.stage_executor import (
    StageExecutor,
    StageResult,
    admit_stage_dispatch,
)
from app.agents.schemas import BudgetExceeded, ev_token
from app.agents.stage_context import StageContext
from app.core.config import get_settings
from app.model_capabilities import capabilities_from_config
from app.providers.base import ChatOptions, ProviderError

logger = logging.getLogger(__name__)

_CONTINUE_INSTRUCTION = (
    "Continue exactly where the answer stopped. Do not repeat any text already given."
)

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


def _gate_writer_retry(guard: Any) -> float:
    guard.enter_step()
    guard.check()
    return guard.remaining_seconds


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
        fixed_user_prompt = _build_user_prompt(kind, question, "")
        admitted_context = admit_stage_dispatch(
            agent=None,
            task=task,
            context=verified,
            stage_ctx=stage_ctx,
            fixed_prompt_tokens=len(system_prompt) + len(fixed_user_prompt),
        )
        user_prompt = _build_user_prompt(kind, question, admitted_context or "")
        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ]
        capabilities = capabilities_from_config(stage_ctx.model_config)
        options = ChatOptions(
            temperature=0.5,
            max_tokens=capabilities.max_output_tokens,
            output_token_parameter=capabilities.output_token_parameter,
            retry_gate=(
                lambda _attempt: _gate_writer_retry(stage_ctx.budget_guard)
                if stage_ctx.budget_guard is not None
                else None
            ),
        )

        stage_ctx.set_stage(agent_id=agent_id, task_id=getattr(task, "id", "") or "writer")

        # Some compatible gateways occasionally return an empty stream. Each
        # logical answer round gets one bounded blank retry; length-truncated
        # rounds additionally get the separately bounded continuation policy.
        policy = ContinuationPolicy(
            max_rounds=get_settings().AUTO_CONTINUATION_MAX_ROUNDS
        )
        continuation_round = 0
        usage_rounds: list[dict[str, Any]] = []
        model_dispatches = 0
        full = assistant_msg.content or ""
        finish_reason = "stop"
        checkpoint: dict[str, Any] | None = None

        def record_failed_usage(current: dict[str, Any] | None) -> None:
            if current is not None:
                stage_ctx.record_usage(
                    f"model:{agent_id}:{model_dispatches}",
                    current,
                    model_usage=True,
                )

        async def persist_continuation(checkpoint: dict[str, Any]) -> None:
            persist_checkpoint = stage_ctx.persist_continuation_checkpoint
            if callable(persist_checkpoint):
                await persist_checkpoint(checkpoint)
                return
            from app.agents.db_mutation import db_mutation_scope
            from app.db import AsyncSessionLocal
            from app.services.chat_service import (
                _persist_continuation_checkpoint,
            )

            session_factory = stage_ctx.persistence_session_factory or AsyncSessionLocal
            async with db_mutation_scope(stage_ctx.persistence_lock):
                await _persist_continuation_checkpoint(
                    session_factory,
                    assistant_msg,
                    stage_ctx.run_id,
                    checkpoint,
                )

        async def persist_or_record_failure(checkpoint: dict[str, Any]) -> None:
            """Persist a checkpoint or retain usage that no StageResult can return."""
            try:
                await persist_continuation(checkpoint)
            except asyncio.CancelledError:
                record_failed_usage(None)
                raise
            except Exception:
                record_failed_usage(None)
                raise

        def cancellation_requested() -> bool:
            ctl = get_run_control(stage_ctx.run_id)
            cancel_evt = getattr(stage_ctx, "cancel_event", None)
            return bool(
                (ctl is not None and ctl.cancel.is_set())
                or (cancel_evt is not None and cancel_evt.is_set())
            )

        async def persist_cancelled_continuation() -> dict[str, Any]:
            checkpoint = {
                "round": continuation_round,
                "max_rounds": policy.max_rounds,
                "status": "cancelled",
            }
            assistant_msg.metadata_ = {
                **(assistant_msg.metadata_ or {}),
                "continuation": checkpoint,
            }
            await persist_or_record_failure(checkpoint)
            return checkpoint

        while True:
            if continuation_round and cancellation_requested():
                finish_reason = "cancelled"
                checkpoint = await persist_cancelled_continuation()
                break
            round_raw = ""
            for attempt in (1, 2):
                model_dispatches += 1
                raw_parts: list[str] = []
                novel_parts: list[str] = []
                round_usage: dict[str, Any] | None = None
                finish_reason = "stop"
                continuation_buffer = (
                    ContinuationBuffer(
                        full, comparison_window=policy.comparison_window
                    )
                    if continuation_round
                    else None
                )
                try:
                    guard = stage_ctx.budget_guard
                    if guard is not None:
                        guard.enter_step()
                        guard.check()
                    timeout = (
                        guard.remaining_seconds if guard is not None else None
                    )
                    async with asyncio.timeout(timeout):
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
                                raw_parts.append(delta.content)
                                novel = (
                                    continuation_buffer.feed(delta.content)
                                    if continuation_buffer is not None
                                    else delta.content
                                )
                                if novel:
                                    novel_parts.append(novel)
                                    full += novel
                                    assistant_msg.content = full
                                    stage_ctx.emit(ev_token(delta=novel))
                            if delta.finish_reason:
                                finish_reason = delta.finish_reason
                            if delta.usage:
                                round_usage = delta.usage
                    if guard is not None:
                        guard.check()
                except TimeoutError as exc:
                    if continuation_buffer is not None:
                        buffered = continuation_buffer.flush()
                        if buffered:
                            full += buffered
                            assistant_msg.content = full
                            stage_ctx.emit(ev_token(delta=buffered))
                    stage_ctx.writer_streamed = True
                    record_failed_usage(round_usage)
                    raise BudgetExceeded(
                        f"time budget ({guard.limits.max_runtime_seconds}s) exceeded"
                    ) from exc
                except asyncio.CancelledError:
                    if continuation_buffer is not None:
                        buffered = continuation_buffer.flush()
                        if buffered:
                            full += buffered
                            assistant_msg.content = full
                            stage_ctx.emit(ev_token(delta=buffered))
                    stage_ctx.writer_streamed = True
                    record_failed_usage(round_usage)
                    if continuation_round:
                        checkpoint = {
                            "round": continuation_round,
                            "max_rounds": policy.max_rounds,
                            "status": "cancelled",
                        }
                        assistant_msg.metadata_ = {
                            **(assistant_msg.metadata_ or {}),
                            "continuation": checkpoint,
                        }
                        await persist_or_record_failure(checkpoint)
                    raise
                except ProviderError:
                    if continuation_buffer is not None:
                        buffered = continuation_buffer.flush()
                        if buffered:
                            full += buffered
                            assistant_msg.content = full
                            stage_ctx.emit(ev_token(delta=buffered))
                    stage_ctx.writer_streamed = True
                    record_failed_usage(round_usage)
                    logger.warning("writer stream provider error", exc_info=True)
                    raise
                except Exception:
                    if continuation_buffer is not None:
                        buffered = continuation_buffer.flush()
                        if buffered:
                            full += buffered
                            assistant_msg.content = full
                            stage_ctx.emit(ev_token(delta=buffered))
                    stage_ctx.writer_streamed = True
                    record_failed_usage(round_usage)
                    logger.exception("writer streaming failed")
                    raise
                if continuation_buffer is not None:
                    buffered = continuation_buffer.flush()
                    if buffered:
                        novel_parts.append(buffered)
                        full += buffered
                        assistant_msg.content = full
                        stage_ctx.emit(ev_token(delta=buffered))
                if round_usage is not None:
                    usage_rounds.append(round_usage)
                    stage_ctx.record_usage(
                        f"model:{agent_id}:{model_dispatches}",
                        round_usage,
                        model_usage=True,
                    )
                    if stage_ctx.budget_guard is not None:
                        stage_ctx.budget_guard.check()
                round_raw = "".join(raw_parts)
                if round_raw or finish_reason == "cancelled":
                    break
                if attempt == 1:
                    logger.info("writer stream returned empty; retrying once")

            ctl = get_run_control(stage_ctx.run_id)
            cancel_evt = getattr(stage_ctx, "cancel_event", None)
            cancelled_before_followup = finish_reason == "length" and (
                (ctl is not None and ctl.cancel.is_set())
                or (cancel_evt is not None and cancel_evt.is_set())
            )
            if cancelled_before_followup:
                finish_reason = "cancelled"
                checkpoint = {
                    "round": continuation_round,
                    "max_rounds": policy.max_rounds,
                    "status": "cancelled",
                }
                assistant_msg.metadata_ = {
                    **(assistant_msg.metadata_ or {}),
                    "continuation": checkpoint,
                }
            if policy.should_continue(
                finish_reason,
                continuation_round,
                cancelled=finish_reason == "cancelled",
            ):
                messages.append({"role": "assistant", "content": round_raw})
                messages.append({"role": "user", "content": _CONTINUE_INSTRUCTION})
                continuation_round += 1
                checkpoint = {
                    "round": continuation_round,
                    "max_rounds": policy.max_rounds,
                    "status": "continuing",
                }
                assistant_msg.metadata_ = {
                    **(assistant_msg.metadata_ or {}),
                    "continuation": checkpoint,
                }
                try:
                    await persist_or_record_failure(checkpoint)
                except asyncio.CancelledError:
                    await persist_cancelled_continuation()
                    raise
                if cancellation_requested():
                    finish_reason = "cancelled"
                    checkpoint = await persist_cancelled_continuation()
                    break
                continue
            if continuation_round:
                checkpoint = {
                    "round": continuation_round,
                    "max_rounds": policy.max_rounds,
                    "status": (
                        "maxed"
                        if finish_reason == "length"
                        else "cancelled"
                        if finish_reason == "cancelled"
                        else "completed"
                    ),
                }
                assistant_msg.metadata_ = {
                    **(assistant_msg.metadata_ or {}),
                    "continuation": checkpoint,
                }
            if checkpoint is not None and checkpoint.get("status") in {
                "maxed",
                "completed",
                "cancelled",
            }:
                await persist_or_record_failure(checkpoint)
            break

        stage_ctx.writer_streamed = True
        assistant_msg.content = full
        # raw="" → runtime must NOT re-emit a bulk token. output_summary feeds
        # the activity feed / agent node card.
        structured: dict[str, Any] = {
            "finish_reason": finish_reason,
            "streamed": True,
        }
        usage = aggregate_usage(usage_rounds)
        if usage is not None:
            structured["usage"] = usage
        if checkpoint is not None:
            structured["continuation"] = checkpoint
        return StageResult(
            agent_id=agent_id,
            raw="",
            output_summary=(full.strip().replace("\n", " ")[:160] or "（空回答）"),
            structured=structured,
            usage=usage,
            usage_charged=True,
        )


def _build_user_prompt(kind: str, question: str, verified: str) -> str:
    if kind == "code":
        # Code intent: write the program. The Analyst's research is reference
        # only — never restate it as prose (that is what produced
        # "architecture-only" answers).
        return (
            f"用户问题：\n{question}\n\n"
            f"（可选参考）研究结论：\n{verified or '（无）'}\n\n"
            "请针对用户问题直接产出完整、可运行的代码交付物；"
            "上面的研究结论仅作参考，不要复述，除非涉及外部事实或第三方 API 约束。"
        )
    # Research / document intent: answer from verified evidence, cite it.
    return (
        f"用户问题：\n{question}\n\n"
        f"已验证内容：\n{verified or '（无已验证内容，请如实说明缺口）'}\n\n"
        "请基于以上已验证内容生成最终回答，并保留来源编号。"
    )
