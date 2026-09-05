"""Chat attachments: upload validation, ownership isolation, parse, delete, bind."""
from __future__ import annotations

import io
import uuid
from types import SimpleNamespace

import pytest
from fastapi import UploadFile

from app.core.exceptions import AppException
from app.models import ChatAttachment, Conversation
from app.services import attachment_service
from tests.conftest import auth_headers

_SEEDED = uuid.UUID("00000000-0000-0000-0000-000000000001")


async def _make_conv(client, h) -> str:
    return (await client.post("/api/conversations", json={"title": "a"}, headers=h)).json()["id"]


def _file(name: str, data: bytes, ctype: str):
    return {"file": (name, data, ctype)}


async def test_upload_txt_creates_row(client):
    h = auth_headers()
    cid = await _make_conv(client, h)
    r = await client.post(
        "/api/chat-attachments",
        data={"conversation_id": str(cid)},
        files=_file("note.txt", b"hello world", "text/plain"),
        headers=h,
    )
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["original_filename"] == "note.txt"
    assert body["status"] == "uploaded"
    # Listed for the conversation (refresh-restore path).
    lst = (await client.get(f"/api/chat-attachments?conversation_id={cid}", headers=h)).json()
    assert any(a["id"] == body["id"] for a in lst)


async def test_upload_mime_mismatch_rejected(client):
    h = auth_headers()
    cid = await _make_conv(client, h)
    r = await client.post(
        "/api/chat-attachments",
        data={"conversation_id": str(cid)},
        files=_file("a.txt", b"hi", "image/png"),
        headers=h,
    )
    assert r.status_code == 400


async def test_upload_signature_mismatch_rejected(client):
    h = auth_headers()
    cid = await _make_conv(client, h)
    # Declared PDF, but bytes are plain text -> magic-byte mismatch.
    r = await client.post(
        "/api/chat-attachments",
        data={"conversation_id": str(cid)},
        files=_file("a.pdf", b"not a pdf at all", "application/pdf"),
        headers=h,
    )
    assert r.status_code == 400


async def test_upload_disallowed_extension_rejected(client):
    h = auth_headers()
    cid = await _make_conv(client, h)
    r = await client.post(
        "/api/chat-attachments",
        data={"conversation_id": str(cid)},
        files=_file("evil.exe", b"MZ", "application/octet-stream"),
        headers=h,
    )
    assert r.status_code == 400


async def test_upload_filename_traversal_sanitized(client):
    h = auth_headers()
    cid = await _make_conv(client, h)
    r = await client.post(
        "/api/chat-attachments",
        data={"conversation_id": str(cid)},
        files=_file("../../etc/passwd.txt", b"x", "text/plain"),
        headers=h,
    )
    assert r.status_code == 201, r.text
    body = r.json()
    # Basename only; no path components survive.
    assert body["original_filename"] == "passwd.txt"
    assert ".." not in body["filename"]


async def test_cross_user_cannot_access(client):
    h1 = auth_headers()
    cid = await _make_conv(client, h1)
    up = (await client.post(
        "/api/chat-attachments",
        data={"conversation_id": str(cid)},
        files=_file("n.txt", b"x", "text/plain"),
        headers=h1,
    )).json()
    reg = await client.post(
        "/api/auth/register",
        json={"email": "att-other@example.com", "username": "att-other", "password": "Passw0rd!"},
    )
    h2 = {"Authorization": f"Bearer {reg.json()['access_token']}"}
    assert (await client.get(f"/api/chat-attachments/{up['id']}", headers=h2)).status_code == 404
    assert (await client.get(f"/api/chat-attachments/{up['id']}/content", headers=h2)).status_code == 404


async def test_delete_attachment(client):
    h = auth_headers()
    cid = await _make_conv(client, h)
    up = (await client.post(
        "/api/chat-attachments",
        data={"conversation_id": str(cid)},
        files=_file("n.txt", b"x", "text/plain"),
        headers=h,
    )).json()
    assert (await client.delete(f"/api/chat-attachments/{up['id']}", headers=h)).status_code == 204
    # Gone afterward.
    assert (await client.get(f"/api/chat-attachments/{up['id']}", headers=h)).status_code == 404


async def test_resolve_bind_cross_user_forbidden(db_session):
    """Binding attachment_ids on send must reject ids the user doesn't own."""
    conv = Conversation(user_id=_SEEDED, title="bind test")
    db_session.add(conv)
    await db_session.flush()
    att = ChatAttachment(
        user_id=_SEEDED, conversation_id=conv.id, filename="x.txt",
        original_filename="x.txt", mime_type="text/plain", size_bytes=1,
        storage_key="/tmp/x.txt", status="ready", parse_status="ready",
        extracted_text="hello",
    )
    db_session.add(att)
    await db_session.commit()
    await db_session.refresh(att)

    other = uuid.UUID(int=2)
    mid = uuid.uuid4()
    with pytest.raises(AppException) as ei:
        await attachment_service.resolve_and_bind_attachments(
            db_session, other, conv.id, mid, [att.id]
        )
    assert ei.value.status_code == 403


async def test_extract_parses_text(db_session):
    """The parse path returns extracted text for a stored text file."""
    conv = Conversation(user_id=_SEEDED, title="extract test")
    db_session.add(conv)
    await db_session.flush()
    uf = UploadFile(filename="note.txt", file=io.BytesIO(b"the quick brown fox"))
    att = await attachment_service.upload(
        db_session, user=SimpleNamespace(id=_SEEDED), conversation_id=conv.id, upload_file=uf
    )
    text, preview = await attachment_service._extract(att.storage_key, att.original_filename)
    assert "the quick brown fox" in text
    assert preview.get("chars", 0) > 0
    # Clean up the stored file.
    from app.core.storage import get_storage
    await get_storage().delete(att.storage_key)


# --------------------------------------------------------------------------- #
# GET /{id}/text — parsed-text preview for the document preview dialog
# --------------------------------------------------------------------------- #
async def test_attachment_text_endpoint_serves_parsed_text(client, monkeypatch):
    """The /text preview endpoint returns the stored extracted_text, capped."""
    import app.services.attachment_service as svc

    captured = {}

    class _FakeAtt:
        id = uuid.uuid4()
        original_filename = "note.txt"
        mime_type = "text/plain"
        parse_status = "ready"
        preview_metadata = {"chars": 20, "kind": "text"}
        extracted_text = "parsed content for preview"

    async def _fake_get_owned(db, att_id, user_id):
        captured["att_id"] = att_id
        return _FakeAtt()

    monkeypatch.setattr(svc, "get_owned", _fake_get_owned)
    # The router imported the symbol at module load — patch there too.
    import app.api.chat_attachments as router_mod

    monkeypatch.setattr(router_mod.attachment_service, "get_owned", _fake_get_owned)

    r = await client.get(f"/api/chat-attachments/{_FakeAtt.id}/text", headers=auth_headers())
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["text"] == "parsed content for preview"
    assert body["truncated"] is False
    assert body["total_chars"] == len("parsed content for preview")
    assert body["preview_metadata"]["chars"] == 20


async def test_attachment_text_endpoint_truncates(client, monkeypatch):
    import app.api.chat_attachments as router_mod

    class _FakeAtt:
        id = uuid.uuid4()
        original_filename = "big.pdf"
        mime_type = "application/pdf"
        parse_status = "ready"
        preview_metadata = None
        extracted_text = "x" * 1500

    async def _fake_get_owned(db, att_id, user_id):
        return _FakeAtt()

    monkeypatch.setattr(router_mod.attachment_service, "get_owned", _fake_get_owned)

    r = await client.get(
        f"/api/chat-attachments/{_FakeAtt.id}/text?max_chars=1000", headers=auth_headers()
    )
    assert r.status_code == 200
    body = r.json()
    assert len(body["text"]) == 1000
    assert body["truncated"] is True
    assert body["total_chars"] == 1500
