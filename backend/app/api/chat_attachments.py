"""Chat attachments router: upload / get / download / delete / save-to-KB.

Per-conversation file uploads bound to messages on send. All endpoints are
ownership-scoped: a user can only touch attachments in their own conversations.
The stored path is never exposed — clients reference attachments by id and
fetch bytes through the authenticated ``/content`` endpoint.
"""
from __future__ import annotations

import uuid

from fastapi import (
    APIRouter,
    Depends,
    File,
    Form,
    HTTPException,
    Query,
    UploadFile,
    status,
)
from fastapi.responses import Response
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import get_current_user
from app.core.exceptions import AppException
from app.db import get_db
from app.models import Conversation, User
from app.schemas.chat_attachment import (
    AttachmentTextOut,
    ChatAttachmentOut,
    SaveToKbRequest,
)
from app.services import attachment_service

router = APIRouter(prefix="/api/chat-attachments", tags=["chat-attachments"])

NOT_FOUND = status.HTTP_404_NOT_FOUND


async def _assert_conversation_owned(
    db: AsyncSession, conversation_id: uuid.UUID, user: User
) -> Conversation:
    conv = (
        await db.execute(select(Conversation).where(Conversation.id == conversation_id))
    ).scalars().first()
    if conv is None or (conv.user_id != user.id and user.role != "admin"):
        raise HTTPException(NOT_FOUND, "Conversation not found")
    return conv


@router.post("", response_model=ChatAttachmentOut, status_code=status.HTTP_201_CREATED)
async def upload_attachment(
    conversation_id: uuid.UUID = Form(...),
    file: UploadFile = File(...),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> ChatAttachmentOut:
    await _assert_conversation_owned(db, conversation_id, user)
    att = await attachment_service.upload(
        db, user=user, conversation_id=conversation_id, upload_file=file
    )
    return ChatAttachmentOut.model_validate(att)


@router.get("", response_model=list[ChatAttachmentOut])
async def list_attachments(
    conversation_id: uuid.UUID = Query(...),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> list[ChatAttachmentOut]:
    """List attachments for a conversation (used to restore cards on refresh)."""
    await _assert_conversation_owned(db, conversation_id, user)
    rows = await attachment_service.list_for_conversation(db, conversation_id, user.id)
    return [ChatAttachmentOut.model_validate(a) for a in rows]


@router.get("/{attachment_id}", response_model=ChatAttachmentOut)
async def get_attachment(
    attachment_id: uuid.UUID,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> ChatAttachmentOut:
    att = await attachment_service.get_owned(db, attachment_id, user.id)
    return ChatAttachmentOut.model_validate(att)


@router.get("/{attachment_id}/content")
async def download_attachment_content(
    attachment_id: uuid.UUID,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> Response:
    """Authenticated byte download. Filename in Content-Disposition is sanitized."""
    att = await attachment_service.get_owned(db, attachment_id, user.id)
    data = await attachment_service.open_bytes(att)
    safe_name = att.original_filename.replace('"', "").replace("\r", "").replace("\n", "")
    return Response(
        content=data,
        media_type=att.mime_type or "application/octet-stream",
        headers={
            "Content-Disposition": f'attachment; filename="{safe_name}"',
            "Cache-Control": "private, no-store",
        },
    )


@router.get("/{attachment_id}/text", response_model=AttachmentTextOut)
async def get_attachment_text(
    attachment_id: uuid.UUID,
    max_chars: int = Query(20000, ge=500, le=100000),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> AttachmentTextOut:
    """Parsed-text preview for the attachment-preview dialog.

    Serves the stored ``extracted_text`` (produced by the background parse:
    document parsers / image OCR). Capped server-side; ``truncated`` tells the
    client the full text is longer (download or save-to-KB for everything).
    """
    att = await attachment_service.get_owned(db, attachment_id, user.id)
    full = (att.extracted_text or "").strip()
    total = len(full)
    return AttachmentTextOut(
        id=att.id,
        filename=att.original_filename,
        mime_type=att.mime_type or "",
        parse_status=att.parse_status or "",
        preview_metadata=att.preview_metadata,
        text=full[:max_chars],
        truncated=total > max_chars,
        total_chars=total,
    )


@router.delete("/{attachment_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_attachment(
    attachment_id: uuid.UUID,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> None:
    await attachment_service.delete(db, attachment_id, user.id)


@router.post("/{attachment_id}/save-to-kb", response_model=ChatAttachmentOut)
async def save_attachment_to_kb(
    attachment_id: uuid.UUID,
    payload: SaveToKbRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> ChatAttachmentOut:
    try:
        att = await attachment_service.save_to_kb(db, attachment_id, user.id, payload.knowledge_base_id)
    except AppException as exc:  # propagate as the uniform envelope
        raise HTTPException(exc.status_code, exc.message)
    return ChatAttachmentOut.model_validate(att)
