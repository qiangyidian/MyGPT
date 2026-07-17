"""Execute a batch of LLM tool calls, persist the results, and return OpenAI messages.

Given the list of ToolCallDef produced by a model provider (id + name + raw JSON
arguments), this:

  1. Resolves each tool via get_default_registry().get(name).
  2. Parses the raw argument JSON defensively.
  3. Runs the tool, json.dumps-ing the result.
  4. Persists one ToolCall row per call (status success/error, with result/error).
  5. Returns a list of OpenAI-format tool messages for the next model round.

Each tool is executed in its own try/except so one failure never aborts the batch —
a failed tool simply becomes an error-status row and a tool message describing it.
"""
from __future__ import annotations

import json
import logging
import uuid
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.models.tool_call import ToolCall
from app.providers.base import ToolCallDef
from app.tools.base import ToolError
from app.tools.registry_init import get_default_registry

logger = logging.getLogger(__name__)

# Cap persisted/displayed tool output so a runaway tool can't blow up the row or the
# next model prompt.
_MAX_RESULT_CHARS = 8000


async def execute_tool_calls(
    db: AsyncSession,
    conversation_id: uuid.UUID,
    assistant_message_id: uuid.UUID | None,
    tool_calls: list[ToolCallDef],
) -> list[dict[str, Any]]:
    """Execute each tool call and return OpenAI-style tool messages.

    Args:
        db: Async DB session used both to read (tools may query) and to persist
            ToolCall rows. The session is flushed (not committed) here; the caller
            owns the transaction boundary.
        conversation_id: Conversation the calls belong to.
        assistant_message_id: The assistant Message that emitted the tool_calls
            (may be None if not yet persisted).
        tool_calls: Provider-emitted tool call definitions.

    Returns:
        List of ``{"role": "tool", "tool_call_id": ..., "name": ..., "content": ...}``
        dicts, one per input call (failed calls yield an error string as content).
    """
    registry = get_default_registry()
    messages: list[dict[str, Any]] = []

    for call in tool_calls:
        tool_message, tool_call_row = await _execute_one(
            db, conversation_id, assistant_message_id, registry, call
        )
        db.add(tool_call_row)
        messages.append(tool_message)

    try:
        await db.flush()
    except Exception:
        # Persistence failure must not poison the in-memory results we already built;
        # roll back just this batch and surface the error in logs.
        logger.exception("Failed to flush tool_call rows for conversation %s", conversation_id)
        await db.rollback()

    return messages


async def _execute_one(
    db: AsyncSession,
    conversation_id: uuid.UUID,
    assistant_message_id: uuid.UUID | None,
    registry: Any,
    call: ToolCallDef,
) -> tuple[dict[str, Any], ToolCall]:
    """Run a single tool call. Always returns (message, row); never raises."""
    # Parse the raw argument JSON the model produced. Bad JSON -> empty args.
    parsed_args: dict[str, Any]
    try:
        parsed_args = json.loads(call.arguments) if call.arguments else {}
        if not isinstance(parsed_args, dict):
            parsed_args = {"_value": parsed_args}
    except json.JSONDecodeError as exc:
        parsed_args = {}
        return _error_out(
            conversation_id, assistant_message_id, call,
            arguments=parsed_args,
            error=f"invalid arguments JSON: {exc}",
        )

    # Resolve the tool. Unknown name -> error row, not an exception.
    try:
        tool = registry.get(call.name)
    except ToolError as exc:
        return _error_out(
            conversation_id, assistant_message_id, call,
            arguments=parsed_args,
            error=str(exc),
        )

    # Run it, catching everything so the batch keeps going.
    try:
        result = await tool.run(**parsed_args)
    except ToolError as exc:
        return _error_out(
            conversation_id, assistant_message_id, call,
            arguments=parsed_args,
            error=f"tool error: {exc}",
        )
    except Exception as exc:  # noqa: BLE001 — isolate per-tool failures
        logger.exception("Tool %s raised unexpectedly", call.name)
        return _error_out(
            conversation_id, assistant_message_id, call,
            arguments=parsed_args,
            error=f"{type(exc).__name__}: {exc}",
        )

    # Serialise + truncate the result for both the persisted row and the model.
    content = _stringify(result)
    truncated = content[:_MAX_RESULT_CHARS]
    row = ToolCall(
        conversation_id=conversation_id,
        message_id=assistant_message_id,
        tool_name=call.name,
        arguments=parsed_args,
        result={"content": truncated, "truncated": len(content) > _MAX_RESULT_CHARS},
        status="success",
        error_message=None,
    )
    message = {
        "role": "tool",
        "tool_call_id": call.id,
        "name": call.name,
        "content": truncated,
    }
    return message, row


def _error_out(
    conversation_id: uuid.UUID,
    assistant_message_id: uuid.UUID | None,
    call: ToolCallDef,
    *,
    arguments: dict[str, Any],
    error: str,
) -> tuple[dict[str, Any], ToolCall]:
    """Build an error tool message + error-status row."""
    row = ToolCall(
        conversation_id=conversation_id,
        message_id=assistant_message_id,
        tool_name=call.name,
        arguments=arguments,
        result=None,
        status="error",
        error_message=error,
    )
    message = {
        "role": "tool",
        "tool_call_id": call.id,
        "name": call.name,
        "content": json.dumps({"error": error}, ensure_ascii=False),
    }
    return message, row


def _stringify(result: Any) -> str:
    """Best-effort: dict/list -> compact JSON; everything else -> str()."""
    if isinstance(result, str):
        return result
    try:
        return json.dumps(result, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        return str(result)


__all__ = ["execute_tool_calls"]
