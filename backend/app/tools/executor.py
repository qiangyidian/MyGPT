"""Backward-compatible tool-call batch execution.

Historically this module ran tools directly. It is now a thin wrapper over
:class:`~app.agents.gateway.tool_gateway.ToolGateway` so there is exactly one
hardened execution path (permission, approval gate, timeout, audit,
truncation). The agent loop itself calls the gateway directly via the native
runtime; this function remains for any non-run caller that wants the same
guarantees with ``run_id=None`` (dangerous tools block rather than create an
approval row, and no ``AgentStep`` audit row is written).

Given a list of ToolCallDef (id + name + raw JSON arguments), it returns a list
of OpenAI-format tool messages for the next model round. Failed calls yield an
error string as content; one failure never aborts the batch.
"""
from __future__ import annotations

import json
import logging
import uuid
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.agents.gateway.tool_gateway import ToolGateway
from app.models.user import User
from app.providers.base import ToolCallDef

logger = logging.getLogger(__name__)


async def execute_tool_calls(
    db: AsyncSession,
    conversation_id: uuid.UUID,
    assistant_message_id: uuid.UUID | None,
    tool_calls: list[ToolCallDef],
    *,
    user: User | None = None,
) -> list[dict[str, Any]]:
    """Execute each tool call via the gateway and return OpenAI-style tool messages.

    Args:
        db: Async DB session used both to read (tools may query) and to persist
            ToolCall rows. The caller owns the transaction boundary.
        conversation_id: Conversation the calls belong to.
        assistant_message_id: The assistant Message that emitted the tool_calls.
        tool_calls: Provider-emitted tool call definitions.
        user: Optional user for permission checks.

    Returns:
        List of ``{"role": "tool", "tool_call_id": ..., "name": ..., "content": ...}``
        dicts, one per input call.
    """
    gateway = ToolGateway(
        db,
        conversation_id=conversation_id,
        assistant_message_id=assistant_message_id,
        run_id=None,  # legacy path: no agent run, no approval rows
        user=user,
    )

    messages: list[dict[str, Any]] = []
    for call in tool_calls:
        try:
            args = json.loads(call.arguments) if call.arguments else {}
            if not isinstance(args, dict):
                args = {"_value": args}
        except json.JSONDecodeError:
            args = {}
        execution = await gateway.execute(
            tool_call_id=call.id,
            tool_name=call.name,
            arguments=args,
        )
        messages.append(execution.to_openai_tool_message())

    return messages


__all__ = ["execute_tool_calls"]
