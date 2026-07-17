"""Chat router: SSE streaming chat + regenerate.

Consumes the ChatService singleton whose ``stream(db, user, request)`` yields event
dicts shaped ``{"event": <name>, "data": {...}}``. The router is responsible only
for HTTP concerns: turning events into SSE frames, detecting client disconnect, and
short-circuiting ownership checks with a real 404 before the stream opens (after
which status codes are too late). All persistence and the agent/tool loop live in
ChatService.
"""
from __future__ import annotations

import json
import uuid
from typing import Any, AsyncIterator

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import StreamingResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

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
    """Bridge ChatService.stream into SSE frames, bailing out on client disconnect."""
    try:
        async for event in chat_service.stream(db=db, user=user, request=payload):
            # Stop producing frames once the client is gone (stop/regenerate).
            if await request.is_disconnected():
                break

            etype = event.get("event", "message")
            body = event.get("data", {})
            yield _sse(etype, body)

            if etype == "done":
                return
            if etype == "error":
                return
    except HTTPException:
        # Once we are streaming we can no longer return a 4xx; surface as an error event.
        yield _sse("error", {"code": "chat_error", "message": "chat failed"})
        return
    except Exception as exc:  # noqa: BLE001 — never let an exception kill the stream silently
        yield _sse("error", {"code": "internal_error", "message": str(exc)})
        return


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
