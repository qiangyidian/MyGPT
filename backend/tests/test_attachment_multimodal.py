"""Multimodal / vision attachment path: image OCR, image_url content parts,
model-aware inline budget, and the smart-hybrid inline branch.

These exercise the new image understanding + injection seams end-to-end at the
service layer (no live model). OCR runs for real via rapidocr; its first call
in a session pays a one-time model load, cached thereafter.
"""
from __future__ import annotations

import io
import uuid
from types import SimpleNamespace

from fastapi import UploadFile

from app.core.storage import get_storage
from app.models import ChatAttachment, Conversation
from app.services import attachment_service
from app.services.chat_service import _attach_image_parts, _inline_attachment_budget

_SEEDED = uuid.UUID("00000000-0000-0000-0000-000000000001")


def _png_bytes(color: str = "red", size=(40, 40)) -> bytes:
    from PIL import Image  # lazy

    buf = io.BytesIO()
    Image.new("RGB", size, color).save(buf, "PNG")
    return buf.getvalue()


async def _make_image_attachment(db_session, filename: str = "t.png") -> ChatAttachment:
    """Store a real PNG + a ready ChatAttachment row (no background parse spawned)."""
    conv = Conversation(user_id=_SEEDED, title="mm")
    db_session.add(conv)
    await db_session.flush()
    data = _png_bytes()
    key = await get_storage().save(
        UploadFile(filename=filename, file=io.BytesIO(data)),
        _SEEDED, allowed_extensions={".png", ".jpg", ".jpeg", ".webp"},
    )
    att = ChatAttachment(
        user_id=_SEEDED, conversation_id=conv.id, filename=filename,
        original_filename=filename, mime_type="image/png", size_bytes=len(data),
        storage_key=key, status="ready", parse_status="ready", is_temporary=True,
    )
    db_session.add(att)
    await db_session.commit()
    await db_session.refresh(att)
    return att


# ---------------------------------------------------------------------------
# Multimodal message construction
# ---------------------------------------------------------------------------
def test_attach_image_parts_builds_multimodal():
    msgs = [{"role": "system", "content": "s"}, {"role": "user", "content": "hi"}]
    _attach_image_parts(msgs, [{"data_url": "data:image/png;base64,AAA"}])
    content = msgs[-1]["content"]
    assert isinstance(content, list)
    assert content[0] == {"type": "text", "text": "hi"}
    assert content[1]["type"] == "image_url"
    assert content[1]["image_url"]["url"].startswith("data:image/png;base64,")


def test_attach_image_parts_skips_when_no_user_message():
    msgs = [{"role": "system", "content": "s"}]
    _attach_image_parts(msgs, [{"data_url": "data:image/png;base64,AAA"}])
    # No user message → no mutation, no crash.
    assert msgs[0]["content"] == "s"


def test_inline_budget_scales_with_context():
    small = SimpleNamespace(max_context_tokens=4096)
    large = SimpleNamespace(max_context_tokens=128000)
    bs = _inline_attachment_budget(small)
    bl = _inline_attachment_budget(large)
    assert 2000 <= bs < bl


# ---------------------------------------------------------------------------
# Image → data URL (vision path) + image OCR preview
# ---------------------------------------------------------------------------
async def test_collect_image_parts_builds_data_url(db_session):
    att = await _make_image_attachment(db_session)
    try:
        parts = await attachment_service.collect_image_parts(
            db_session, _SEEDED, att.conversation_id, [att.id]
        )
        assert len(parts) == 1
        assert parts[0]["data_url"].startswith("data:image/")
        assert parts[0]["mime_type"] in ("image/png", "image/jpeg")
    finally:
        await get_storage().delete(att.storage_key)


async def test_image_extract_returns_image_preview(db_session):
    """_extract on an image runs OCR (best-effort) and reports kind=image."""
    att = await _make_image_attachment(db_session)
    try:
        text, preview = await attachment_service._extract(att.storage_key, att.original_filename)
        assert isinstance(text, str)
        assert preview["kind"] == "image"
        assert preview["parser_used"] == "ocr"
    finally:
        await get_storage().delete(att.storage_key)


# ---------------------------------------------------------------------------
# Smart-hybrid: small docs are inlined verbatim (oversized RAG is best-effort
# and covered by attachment_rag graceful-degradation, environment-dependent).
# ---------------------------------------------------------------------------
async def test_smart_attachment_text_inlines_small_doc(db_session):
    conv = Conversation(user_id=_SEEDED, title="smart")
    db_session.add(conv)
    await db_session.flush()
    att = ChatAttachment(
        user_id=_SEEDED, conversation_id=conv.id, filename="n.txt",
        original_filename="n.txt", mime_type="text/plain", size_bytes=10,
        storage_key="/tmp/n.txt", status="ready", parse_status="ready",
        extracted_text="short doc body",
    )
    db_session.add(att)
    await db_session.commit()
    await db_session.refresh(att)
    text = await attachment_service.smart_attachment_text(
        db_session, _SEEDED, conv.id, [att.id], "any question", inline_budget=8000
    )
    assert "short doc body" in text
    assert "n.txt" in text


# ---------------------------------------------------------------------------
# Cross-turn re-hydration: the model keeps seeing a bound file on FOLLOW-UP
# turns (current + history user messages get the text spliced into the
# provider dicts; persisted content stays clean for the UI).
# ---------------------------------------------------------------------------
async def test_history_attachment_texts_and_message_dicts(db_session):
    from app.models import Message
    from app.services.chat_service import _messages_to_dicts

    conv = Conversation(user_id=_SEEDED, title="cross-turn")
    db_session.add(conv)
    await db_session.flush()
    m1 = Message(conversation_id=conv.id, role="user", content="总结这个文件")
    a1 = Message(conversation_id=conv.id, role="assistant", content="好的")
    m2 = Message(conversation_id=conv.id, role="user", content="追问第二句")
    db_session.add_all([m1, a1, m2])
    await db_session.flush()
    att = ChatAttachment(
        user_id=_SEEDED, conversation_id=conv.id, message_id=m1.id,
        filename="n.txt", original_filename="n.txt", mime_type="text/plain",
        size_bytes=10, storage_key="/tmp/n.txt", status="ready",
        parse_status="ready", extracted_text="short doc body",
    )
    db_session.add(att)
    await db_session.commit()

    mapping = await attachment_service.history_attachment_texts(
        db_session, _SEEDED, conv.id, [m1.id, m2.id], "any question",
        inline_budget=8000,
    )
    assert set(mapping.keys()) == {m1.id}, (
        f"only the bound message should map: {mapping}"
    )
    assert "short doc body" in mapping[m1.id]

    dicts = _messages_to_dicts(None, [m1, a1, m2], attachment_text_by_id=mapping)
    assert dicts[0]["role"] == "user"
    assert "[附件内容]" in dicts[0]["content"] and "short doc body" in dicts[0]["content"]
    # Persisted content stays clean — the UI bubble never shows spliced text.
    assert dicts[0]["content"].startswith("总结这个文件")
    assert dicts[1]["content"] == "好的"
    assert dicts[2]["content"] == "追问第二句"
