"""Chat router: SSE streaming chat + regenerate.

Consumes the ChatService singleton whose ``stream(db, user, request)`` yields event
dicts shaped ``{"event": <name>, "data": {...}}``. The router is responsible only
for HTTP concerns: turning events into SSE frames, detecting client disconnect, and
short-circuiting ownership checks with a real 404 before the stream opens (after
which status codes are too late). All persistence and the agent/tool loop live in
ChatService.
"""
from __future__ import annotations

import asyncio
import json
import uuid
from typing import Any, AsyncIterator

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import StreamingResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.deps import get_current_user
from app.db import get_db
from app.models import Conversation, User
from app.schemas import ChatRequest

router = APIRouter(prefix="/api/chat", tags=["chat"])

NOT_FOUND = status.HTTP_404_NOT_FOUND


def _sse(event: str, data: Any) -> str:
    """Format one SSE frame: ``event: <name>\\n\\ndata: <json>\\n\\n``.

    JSON is serialized compactly on a single line so each frame is one record."""
    return f"event: {event}\ndata: {json.dumps(data, default=str, ensure_ascii=False)}\n\n"


def _get_chat_service() -> Any:
    """Resolve the ChatService singleton lazily so this module imports cleanly."""
    from app.services.chat_service import chat_service  # module-level singleton

    return chat_service


async def _event_generator(
    request: Request,
    chat_service: Any,
    db: AsyncSession,
    user: User,
    payload: ChatRequest,
) -> AsyncIterator[str]:
    """Bridge ChatService.stream into SSE frames with a heartbeat.

    The chat stream runs in a producer task feeding a queue; the consumer races
    ``queue.get`` against a heartbeat timer so a long agent run keeps the
    connection alive (proxies/CDNs otherwise drop idle streams). On client
    disconnect the producer is cancelled — ChatService sees CancelledError and
    persists whatever partial content it has.
    """
    queue: asyncio.Queue = asyncio.Queue()
    sentinel = object()

    async def _producer() -> None:
        try:
            async for event in chat_service.stream(db=db, user=user, request=payload):
                await queue.put(event)
        except HTTPException:
            await queue.put({"event": "error", "data": {"code": "chat_error", "message": "chat failed"}})
        except Exception as exc:  # noqa: BLE001 — never kill the stream silently
            await queue.put({"event": "error", "data": {"code": "internal_error", "message": str(exc)}})
        finally:
            await queue.put(sentinel)

    heartbeat = max(5, get_settings().SSE_HEARTBEAT_SECONDS)
    task = asyncio.create_task(_producer())
    try:
        while True:
            if await request.is_disconnected():
                break
            try:
                item = await asyncio.wait_for(queue.get(), timeout=heartbeat)
            except asyncio.TimeoutError:
                # SSE comment frame: keeps the connection alive, ignored by clients.
                yield ": keepalive\n\n"
                continue
            if item is sentinel:
                return
            etype = item.get("event", "message")
            body = item.get("data", {})
            yield _sse(etype, body)
            if etype in ("done", "error"):
                return
    finally:
        if not task.done():
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass


async def _assert_owned(db: AsyncSession, conv_id: uuid.UUID, user: User) -> None:
    """404 (not 403) on foreign conversations to avoid leaking existence."""
    conv = (
        await db.execute(select(Conversation).where(Conversation.id == conv_id))
    ).scalars().first()
    if conv is None:
        raise HTTPException(NOT_FOUND, "Conversation not found")
    if conv.user_id != user.id and user.role != "admin":
        raise HTTPException(NOT_FOUND, "Conversation not found")


@router.post("/stream")
async def chat_stream(
    payload: ChatRequest,
    request: Request,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> StreamingResponse:
    if payload.conversation_id is not None:
        await _assert_owned(db, payload.conversation_id, user)

    chat_service = _get_chat_service()
    generator = _event_generator(request, chat_service, db, user, payload)
    return StreamingResponse(
        generator,
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",  # disable proxy buffering (nginx)
        },
    )


@router.post("/regenerate/{conversation_id}")
async def regenerate(
    conversation_id: uuid.UUID,
    request: Request,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> StreamingResponse:
    """Re-run the assistant turn for the last user message in a conversation."""
    await _assert_owned(db, conversation_id, user)
    chat_service = _get_chat_service()
    payload = ChatRequest(conversation_id=conversation_id, regenerate=True)
    generator = _event_generator(request, chat_service, db, user, payload)
    return StreamingResponse(
        generator,
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )
