"""Adapt app tools into CrewAI ``BaseTool`` wrappers.

Each adapter delegates to :class:`~app.agents.gateway.tool_gateway.ToolGateway`,
so a CrewAI agent's tool calls go through the *same* permission / approval /
audit / truncation path as the native runtime. Because CrewAI invokes ``_run``
from its own execution context (often a worker thread), each call opens a
**fresh** :class:`~app.db.AsyncSessionLocal` — the gateway never shares a
session across threads. Audit rows are linked back to the run via stored ids.

Real-time tool events + agent attribution (multi-agent visualization):
  * A shared :class:`~app.agents.stage_context.StageContext` is captured at
    adapter-build time. The executor sets ``stage_ctx.agent_id`` before each
    stage; the adapter reads it inside ``_run``.
  * Around each tool call the adapter emits ``tool_call``/``tool_result`` events
    tagged with the current ``agent_id``/``task_id`` via ``stage_ctx.emit``
    (which is thread-safe — it forwards to the main-loop queue).
  * The gateway persists ``agent_id``/``task_id`` on the ``AgentStep`` row.
"""
from __future__ import annotations

import asyncio
import json
import uuid
from typing import Any

from app.agents.gateway.tool_gateway import ToolGateway
from app.agents.schemas import BudgetExceeded, ToolExecution, ev_tool_call, ev_tool_result
from app.agents.stage_context import StageContext
from app.tools.base import BaseTool as AppBaseTool


def _map_type(t: str) -> type:
    return {"string": str, "integer": int, "number": float, "boolean": bool}.get(t, Any)


def _build_args_schema(source: AppBaseTool):
    """Build a Pydantic model from the source tool's ToolParameter list."""
    from pydantic import BaseModel, Field, create_model

    fields: dict[str, Any] = {}
    for p in source.parameters:
        py_type = _map_type(p.type)
        if p.required:
            fields[p.name] = (py_type, Field(..., description=p.description))
        else:
            default = p.default if p.default is not None else None
            fields[p.name] = (py_type, Field(default=default, description=p.description))
    if not fields:
        return BaseModel
    return create_model(f"{source.name}Args", **fields)


def _bridge_async(coro):
    """Run a coroutine from sync ``_run`` whether or not an event loop is running.

    CrewAI may call ``_run`` from inside ``aexecute_task`` (a running loop) or
    from a sync ``kickoff``. The former is handled by running the coro in a
    worker thread with its own loop, so we never recurse into a running loop.
    """
    try:
        asyncio.get_running_loop()
        running = True
    except RuntimeError:
        running = False

    if not running:
        return asyncio.run(coro)

    import concurrent.futures

    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as ex:
        future = ex.submit(asyncio.run, coro)
        return future.result()


async def _execute_via_gateway(
    tool_name: str,
    kwargs: dict[str, Any],
    conversation_id,
    message_id,
    run_id,
    user_id,
    agent_id: str,
    task_id: str,
    tool_call_id: str,
    budget_guard=None,
    max_result_chars: int = 8_000,
) -> ToolExecution:
    """Open a fresh session, run the tool through the gateway, commit, return."""
    from sqlalchemy import select

    from app.db import AsyncSessionLocal
    from app.models.user import User

    async def run() -> ToolExecution:
        async with AsyncSessionLocal() as db:
            user = None
            if user_id is not None:
                user = (
                    await db.execute(select(User).where(User.id == user_id))
                ).scalar_one_or_none()
            gw = ToolGateway(
                db,
                conversation_id=conversation_id,
                assistant_message_id=message_id,
                run_id=run_id,
                user=user,
                max_result_chars=max_result_chars,
            )
            gw.set_attribution(agent_id=agent_id, task_id=task_id)
            exec_ = await gw.execute(
                tool_call_id=tool_call_id,
                tool_name=tool_name,
                arguments=kwargs,
                agent_id=agent_id,
                task_id=task_id,
            )
            await db.commit()
            return exec_

    if budget_guard is None:
        return await run()
    budget_guard.check()
    try:
        async with asyncio.timeout(budget_guard.remaining_seconds):
            execution = await run()
    except TimeoutError as exc:
        raise BudgetExceeded(
            f"time budget ({budget_guard.limits.max_runtime_seconds}s) exceeded"
        ) from exc
    budget_guard.check()
    return execution


def _format_for_crewai(exec_: ToolExecution) -> str:
    """Render a ToolExecution as the string CrewAI feeds back to the agent."""
    if exec_.ok:
        return str(exec_.to_openai_tool_message().get("content") or "")
    return json.dumps({"error": exec_.error or "tool failed"}, ensure_ascii=False)


def _blocked_execution(orig: ToolExecution, reason: str) -> ToolExecution:
    """Return a copy of ``orig`` marked blocked (ok=False) with the given reason.

    Used when a dangerous tool's approval is rejected / cancelled / timed out
    so the agent sees a clean error instead of a half-resolved result.
    """
    return ToolExecution(
        ok=False,
        tool_call_id=orig.tool_call_id,
        tool_name=orig.tool_name,
        arguments=orig.arguments,
        status="blocked",
        result=None,
        error=reason,
        approval_id=orig.approval_id,
        truncated=False,
        latency_ms=orig.latency_ms,
    )


def _result_preview(exec_: ToolExecution) -> Any:
    # Prefer the UNTRUNCATED result so the SSE tool_result → UI can parse the
    # full web_search/http_get JSON into 「来源」. The truncated 'content'
    # (capped at 8000 for the model context) used to be sent here, which cut the
    # JSON mid-array and broke frontend source extraction in 专家 (CrewAI) mode.
    if exec_.full_result is not None:
        return exec_.full_result
    if exec_.ok and isinstance(exec_.result, dict):
        return exec_.result.get("content")
    return None


def _execution_usage(exec_: ToolExecution | Any) -> dict[str, Any] | None:
    """Extract optional metering emitted by a tool implementation."""
    from app.agents.continuation import normalize_usage

    return normalize_usage(getattr(exec_, "usage", None))


def build_crewai_tool(
    source: AppBaseTool,
    *,
    conversation_id,
    message_id,
    run_id,
    user_id,
    stage_ctx: StageContext | None = None,
    budget_guard=None,
    max_result_chars: int = 8_000,
) -> Any:
    """Construct a CrewAI ``BaseTool`` wrapping an app tool.

    ``stage_ctx`` is required for the multi-agent path (real-time tool events +
    attribution). When ``None`` (legacy/test path) the adapter still works but
    emits no live events and attributes to the empty agent.
    """
    from pydantic import PrivateAttr

    from crewai.tools import BaseTool

    schema = _build_args_schema(source)
    tool_name = source.name
    conv_id, msg_id, rid, uid = conversation_id, message_id, run_id, user_id
    ctx = stage_ctx
    guard = budget_guard or (ctx.budget_guard if ctx is not None else None)
    output_limit = max_result_chars

    class _Adapter(BaseTool):
        name: str = source.name
        description: str = source.description
        args_schema: type = schema

        _conversation_id = PrivateAttr(default=conv_id)
        _message_id = PrivateAttr(default=msg_id)
        _run_id = PrivateAttr(default=rid)
        _user_id = PrivateAttr(default=uid)

        def _run(self, **kwargs: Any) -> str:
            agent_id = ctx.agent_id if ctx is not None else ""
            task_id = ctx.task_id if ctx is not None else ""
            call_id = str(uuid.uuid4())

            if ctx is not None:
                ctx.emit(ev_tool_call(
                    id=call_id, name=tool_name, arguments=kwargs,
                    agent_id=agent_id, task_id=task_id,
                ))
            elif guard is not None:
                guard.enter_tool_call()

            exec_ = _bridge_async(
                _execute_via_gateway(
                    tool_name, kwargs,
                    self._conversation_id, self._message_id,
                    self._run_id, self._user_id,
                    agent_id, task_id,
                    call_id, guard, output_limit,
                )
            )

            # Dangerous tool awaiting approval: pause for a human decision if a
            # bridge is wired (CrewAI multi-agent path). The worker thread
            # blocks here while the main loop emits waiting + awaits the
            # coordinator; on approve we re-run the gateway (which now finds
            # the approved ToolApproval and executes for real).
            bridge = ctx.approval_bridge if ctx is not None else None
            if exec_.status == "needs_approval" and exec_.approval_id is not None and bridge is not None:
                decision, reason = bridge.request_pause(
                    approval_id=exec_.approval_id,
                    agent_id=agent_id,
                    tool_name=tool_name,
                )
                if decision == "approved":
                    exec_ = _bridge_async(
                        _execute_via_gateway(
                            tool_name, kwargs,
                            self._conversation_id, self._message_id,
                            self._run_id, self._user_id,
                            agent_id, task_id,
                            call_id, guard, output_limit,
                        )
                    )
                else:
                    exec_ = _blocked_execution(exec_, reason or f"approval {decision}")

            if ctx is not None:
                ctx.emit(ev_tool_result(
                    id=call_id, name=tool_name, ok=exec_.ok,
                    result=_result_preview(exec_), error=exec_.error,
                    agent_id=agent_id, task_id=task_id,
                    usage=_execution_usage(exec_),
                ))
            elif guard is not None:
                usage = _execution_usage(exec_)
                if usage:
                    guard.add_usage(usage, usage_id=f"tool:{call_id}")
                guard.check()

            return _format_for_crewai(exec_)

    return _Adapter()
