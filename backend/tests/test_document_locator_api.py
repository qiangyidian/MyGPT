"""定位端点的鉴权、错误码与缓存语义。

本文件只测"端点这一层"的语义：谁不能看、看不了时给什么码、缓存对不对。
定位算法本身在 ``test_locator.py`` 里测。
"""
from __future__ import annotations

import uuid

import pymupdf

from app.models import Document, DocumentChunk, KnowledgeBase, User
from tests.conftest import auth_headers

SEEDED = uuid.UUID("00000000-0000-0000-0000-000000000001")


def _make_pdf(path) -> str:
    doc = pymupdf.open()
    page = doc.new_page()
    for i in range(4):
        page.insert_text((72, 72 + i * 16), f"第{i}行 内容 ABC-{i}", fontsize=10)
    doc.save(str(path))
    doc.close()
    return str(path)


async def _seed_doc(db, tmp_path, *, with_anchors: bool = True):
    """真 PDF + KB + Document + 一条 chunk。锚点取自真实解析结果。"""
    from app.rag.parsers import default_parser

    src = _make_pdf(tmp_path / "doc.pdf")
    kb = KnowledgeBase(user_id=SEEDED, name="loc-kb")
    db.add(kb)
    await db.flush()
    doc = Document(
        knowledge_base_id=kb.id,
        filename="doc.pdf",
        file_path=src,
        file_type=".pdf",
        file_size=1234,
        status="indexed",
        chunk_count=1,
    )
    db.add(doc)
    await db.flush()

    parsed = default_parser.parse(src, ".pdf")
    line = parsed.layout.pages[0].lines[1]
    meta: dict = {"page": 1}
    if with_anchors:
        meta["char_start"] = line.char_start
        meta["char_end"] = line.char_end
    chunk = DocumentChunk(
        document_id=doc.id,
        knowledge_base_id=kb.id,
        chunk_index=0,
        content=line.text,
        token_count=10,
        metadata_=meta,
    )
    db.add(chunk)
    await db.commit()
    return doc.id, chunk.id


async def _other_user(db) -> dict[str, str]:
    """第二个租户的鉴权头（本测试里不拥有任何文档）。"""
    from tests.conftest import get_access_token

    suffix = uuid.uuid4().hex[:8]
    other = User(
        email=f"other-loc-{suffix}@example.com",
        username=f"other-loc-{suffix}",
        password_hash="x",
        role="user",
        is_active=True,
    )
    db.add(other)
    await db.commit()
    return auth_headers(get_access_token(other.id))


async def test_locator_returns_page_image_with_normalised_boxes(client, db_session, tmp_path):
    doc_id, chunk_id = await _seed_doc(db_session, tmp_path)

    res = await client.get(
        f"/api/documents/{doc_id}/chunks/{chunk_id}/locator", headers=auth_headers()
    )

    assert res.status_code == 200
    body = res.json()
    assert body["kind"] == "page_image"
    assert body["page"] == 1 and body["total_pages"] == 1
    assert body["image_url"].endswith("/pages/1/image")
    assert len(body["boxes"]) == 1
    x0, y0, x1, y1 = body["boxes"][0]
    assert x0 >= 0 and x1 <= 1 and y0 >= 0 and y1 <= 1


async def test_locator_404_for_someone_elses_document(client, db_session, tmp_path):
    """别人的文档一律 404，不区分"不存在"与"不是你的"。"""
    doc_id, chunk_id = await _seed_doc(db_session, tmp_path)
    headers = await _other_user(db_session)

    res = await client.get(
        f"/api/documents/{doc_id}/chunks/{chunk_id}/locator", headers=headers
    )
    assert res.status_code == 404


async def test_locator_reports_no_locator_for_chunk_without_char_anchors(
    client, db_session, tmp_path
):
    """没有 char 偏移的 chunk 必须给机器可读的 no_locator，而不是空白 200。"""
    doc_id, chunk_id = await _seed_doc(db_session, tmp_path, with_anchors=False)

    res = await client.get(
        f"/api/documents/{doc_id}/chunks/{chunk_id}/locator", headers=auth_headers()
    )
    assert res.status_code == 404
    assert res.json()["code"] == "no_locator"


async def test_locator_404_for_unknown_chunk(client, db_session, tmp_path):
    doc_id, _ = await _seed_doc(db_session, tmp_path)
    res = await client.get(
        f"/api/documents/{doc_id}/chunks/{uuid.uuid4()}/locator", headers=auth_headers()
    )
    assert res.status_code == 404


async def test_page_image_sets_etag_and_answers_304(client, db_session, tmp_path):
    doc_id, _ = await _seed_doc(db_session, tmp_path)

    first = await client.get(f"/api/documents/{doc_id}/pages/1/image", headers=auth_headers())
    assert first.status_code == 200
    assert first.headers["content-type"] == "image/png"
    assert first.content[:4] == b"\x89PNG"
    etag = first.headers["etag"]
    assert etag

    second = await client.get(
        f"/api/documents/{doc_id}/pages/1/image",
        headers={**auth_headers(), "If-None-Match": etag},
    )
    assert second.status_code == 304


async def test_page_image_404_for_someone_elses_document(client, db_session, tmp_path):
    doc_id, _ = await _seed_doc(db_session, tmp_path)
    headers = await _other_user(db_session)
    res = await client.get(f"/api/documents/{doc_id}/pages/1/image", headers=headers)
    assert res.status_code == 404


async def test_page_image_rejects_out_of_range_page_and_dpi(client, db_session, tmp_path):
    doc_id, _ = await _seed_doc(db_session, tmp_path)
    headers = auth_headers()

    assert (
        await client.get(f"/api/documents/{doc_id}/pages/9999/image", headers=headers)
    ).status_code == 404
    assert (
        await client.get(
            f"/api/documents/{doc_id}/pages/1/image?dpi=9999", headers=headers
        )
    ).status_code == 422
