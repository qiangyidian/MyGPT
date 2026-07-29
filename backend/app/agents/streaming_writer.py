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


class StreamingWriterExecutor:
    """Wraps a base executor; streams the writer stage, delegates the rest.

    Implemented as a duck-typed :class:`StageExecutor` (the protocol is
    runtime_checkable and structural) so it slots into the existing executor
    factory without changing the ``StageExecutor`` signature.
    """

    def __init__(self, base: StageExecutor, *, max_tokens: int = 1024) -> None:
        self._base = base
        self._max_tokens = max_tokens

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
        question = (
            getattr(task, "description", "") or getattr(stage_ctx, "user_content", "") or ""
        ).strip()
        verified = (context or "").strip()

        user_prompt = (
            f"用户问题：\n{question}\n\n"
            f"已验证内容：\n{verified or '（无已验证内容，请如实说明缺口）'}\n\n"
            "请基于以上已验证内容生成最终回答。"
        )
        messages = [
            {"role": "system", "content": _SYSTEM},
            {"role": "user", "content": user_prompt},
        ]
        options = ChatOptions(temperature=0.5, max_tokens=self._max_tokens)

        stage_ctx.set_stage(agent_id=agent_id, task_id=getattr(task, "id", "") or "writer")

        accumulated: list[str] = []
        finish_reason = "stop"
        try:
            async for delta in provider.stream_chat(messages, options):
                # Cooperative cancel between chunks (client disconnect / stop).
                cancel_evt = getattr(stage_ctx, "cancel_event", None)
                if cancel_evt is not None and cancel_evt.is_set():
                    finish_reason = "cancelled"
                    break
                # Phase 2: honor a user-initiated pause mid-stream.
                ctl = get_run_control(stage_ctx.run_id)
                if ctl is not None:
                    while ctl.is_paused() and not ctl.cancel.is_set():
                        await asyncio.sleep(0.1)
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
