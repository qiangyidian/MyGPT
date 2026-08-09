"""Repro: uploading a real .docx (Word) errors. Compare against a tiny .txt
(which the existing suite shows working) and a large .txt (to isolate
size/streaming vs docx-specific)."""
from __future__ import annotations

import io

import pytest

from tests.conftest import auth_headers
from tests.test_attachments import _file, _make_conv


async def test_upload_real_docx(client):
    from docx import Document

    doc = Document()
    doc.add_paragraph("word " * 20000)  # realistic-size docx (~tens of KB)
    buf = io.BytesIO()
    doc.save(buf)
    data = buf.getvalue()
    h = auth_headers()
    cid = await _make_conv(client, h)
    r = await client.post(
        "/api/chat-attachments",
        data={"conversation_id": str(cid)},
        files=_file(
            "report.docx",
            data,
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        ),
        headers=h,
    )
    assert r.status_code == 201, f"DOCX UPLOAD FAILED: {r.status_code} {r.text[:800]}"


async def test_upload_large_txt(client):
    h = auth_headers()
    cid = await _make_conv(client, h)
    r = await client.post(
        "/api/chat-attachments",
        data={"conversation_id": str(cid)},
        files=_file("big.txt", b"a" * 300000, "text/plain"),
        headers=h,
    )
    assert r.status_code == 201, f"LARGE TXT UPLOAD FAILED: {r.status_code} {r.text[:800]}"
