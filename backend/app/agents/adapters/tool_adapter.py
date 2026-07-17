"""Adapt app tools into CrewAI ``BaseTool`` wrappers.

Each adapter delegates to :class:`~app.agents.gateway.tool_gateway.ToolGateway`,
so a CrewAI agent's tool calls go through the *same* permission / approval /
audit / truncation path as the native runtime. Because CrewAI invokes ``_run``
from its own execution context (often a worker thread), each call opens a
**fresh** :class:`~app.db.AsyncSessionLocal` — the gateway never shares a
session across threads. Audit rows are linked back to the run via stored ids.
"""
from __future__ import annotations

import asyncio
import json
import uuid
from typing import Any

from app.agents.gateway.tool_gateway import ToolGateway
from app.agents.schemas import ToolExecution
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

    CrewAI may call ``_run`` from inside ``kickoff_async`` (a running loop) or
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
) -> ToolExecution:
    """Open a fresh session, run the tool through the gateway, commit, return."""
    from sqlalchemy import select

    from app.db import AsyncSessionLocal
    from app.models.user import User

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
        )
        exec_ = await gw.execute(
            tool_call_id=str(uuid.uuid4()),
            tool_name=tool_name,
            arguments=kwargs,
        )
        await db.commit()
        return exec_


def _format_for_crewai(exec_: ToolExecution) -> str:
    """Render a ToolExecution as the string CrewAI feeds back to the agent."""
    if exec_.ok:
        r = exec_.result
        if isinstance(r, dict):
            return json.dumps(r, ensure_ascii=False, default=str)
        return str(r) if r is not None else ""
    return json.dumps({"error": exec_.error or "tool failed"}, ensure_ascii=False)


def build_crewai_tool(
    source: AppBaseTool,
    *,
    conversation_id,
    message_id,
    run_id,
    user_id,
) -> Any:
    """Construct a CrewAI ``BaseTool`` wrapping an app tool."""
    from pydantic import PrivateAttr

    from crewai.tools import BaseTool

    schema = _build_args_schema(source)
    tool_name = source.name
    conv_id, msg_id, rid, uid = conversation_id, message_id, run_id, user_id

    class _Adapter(BaseTool):
        # name/description/args_schema are BaseTool fields; set as class defaults
        # (the bare-name clash on the right side is avoided by aliasing above).
        name: str = source.name
        description: str = source.description
        args_schema: type = schema

        _conversation_id = PrivateAttr(default=conv_id)
        _message_id = PrivateAttr(default=msg_id)
        _run_id = PrivateAttr(default=rid)
        _user_id = PrivateAttr(default=uid)

        def _run(self, **kwargs: Any) -> str:
            exec_ = _bridge_async(
                _execute_via_gateway(
                    tool_name, kwargs,
                    self._conversation_id, self._message_id,
                    self._run_id, self._user_id,
                )
            )
            return _format_for_crewai(exec_)

    return _Adapter()
