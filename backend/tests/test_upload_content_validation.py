"""上传的「真实类型」校验 + 流式大小上限（知识库路径）。

回归的是：知识库上传只看扩展名白名单，``app/core/storage.py`` 也只看后缀；大小
检查发生在文件已经落盘之后，而且 ``UploadFile.size`` 为 ``None`` 时直接跳过。
现在聊天附件与知识库共用 :mod:`app.core.file_signatures` 的魔数表，字节上限在
写盘过程中累计、超限即中断并删除已落盘文件。

断言都按自己的主键/路径过滤：测试库是 StaticPool 单连接，别的用例留下的数据在同
一个进程里可见（每个用例都新建自己的 user 目录，所以 glob 结果只包含本次上传）。
"""
from __future__ import annotations

import io
import os
import uuid

import pytest
from fastapi import UploadFile
from sqlalchemy import select
from starlette.datastructures import Headers

from app.core.config import get_settings
from app.core.exceptions import AppException
from app.core.file_signatures import (
    FileSignatureError,
    check_file_signature,
    verified_size,
)
from app.core.storage import LocalStorage, UploadTooLargeError, get_storage
from app.models import Document, KnowledgeBase, User
from app.services import document_service
from tests.conftest import auth_headers

_SEEDED = uuid.UUID("00000000-0000-0000-0000-000000000001")
_REAL_PDF = b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n"


def _upload(filename: str, data: bytes, content_type: str | None = None) -> UploadFile:
    """A real in-memory upload; ``content_type`` rides in the headers, as FastAPI does."""
    headers = Headers({"content-type": content_type}) if content_type else None
    return UploadFile(io.BytesIO(data), filename=filename, size=len(data), headers=headers)


class _Cancelled(BaseException):
    """Stands in for the ``CancelledError`` a real mid-body disconnect raises.

    A genuine ``CancelledError`` would cancel *this* test's task instead of being
    delivered to ``pytest.raises``, so the fake plays the same role: it is not an
    ``Exception``, which is exactly the case the cleanup branch must cover.
    """

    def __init__(self) -> None:
        super().__init__("client hung up mid-upload")


class _DyingUpload:
    """An upload whose stream dies after the first chunk (client hung up)."""

    def __init__(self, filename: str, chunks: list[bytes]) -> None:
        self.filename = filename
        self._chunks = chunks
        self._i = 0

    async def seek(self, offset: int) -> None:
        self._i = 0

    async def read(self, size: int) -> bytes:
        if self._i >= len(self._chunks):
            raise _Cancelled()
        chunk = self._chunks[self._i]
        self._i += 1
        return chunk


# --------------------------------------------------------------------------- #
# 共用的魔数检查
# --------------------------------------------------------------------------- #
def test_check_file_signature_accepts_matching_bytes(tmp_path):
    p = tmp_path / "a.pdf"
    p.write_bytes(_REAL_PDF)
    check_file_signature(str(p), ".pdf")  # must not raise


def test_check_file_signature_rejects_mismatched_bytes(tmp_path):
    p = tmp_path / "evil.pdf"
    p.write_bytes(b"MZ\x90\x00 this is not a pdf")
    with pytest.raises(FileSignatureError) as exc:
        check_file_signature(str(p), ".pdf")
    assert exc.value.reason == "mismatch"
    assert "不一致" in exc.value.message


def test_check_file_signature_reports_unreadable():
    with pytest.raises(FileSignatureError) as exc:
        check_file_signature(str(uuid.uuid4()), ".pdf")
    assert exc.value.reason == "unreadable"


def test_check_file_signature_skips_types_without_a_rule(tmp_path):
    """文本类没有可靠魔数；未知扩展名由上层白名单拦，这里不重复判决。"""
    p = tmp_path / "n.txt"
    p.write_bytes(b"hello")
    check_file_signature(str(p), ".txt")
    check_file_signature(str(p), ".unknown")


def test_legacy_office_requires_a_real_container(tmp_path):
    """KB 白名单里的 .doc/.xls 由 LibreOffice/xlrd 解析，容器不对必炸，先拒。"""
    xls = tmp_path / "sheet.xls"
    xls.write_bytes(b"a,b,c\n1,2,3\n")  # 一份改了名字的 CSV
    with pytest.raises(FileSignatureError):
        check_file_signature(str(xls), ".xls")

    doc = tmp_path / "old.doc"
    doc.write_bytes(b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" + b"\x00" * 32)
    check_file_signature(str(doc), ".doc")  # OLE2 容器：放过


def test_attachment_path_and_kb_share_one_signature_table():
    """两条上传路径必须吃同一张表，否则又会各自漂移。"""
    from app.core.file_signatures import EXT_RULES
    from app.services import attachment_service

    assert attachment_service._EXT_RULES is EXT_RULES
    assert EXT_RULES[".pdf"][1] == (b"%PDF",)
    # The attachment sniffer is still the same behaviour, just delegated.
    with pytest.raises(AppException) as exc:
        attachment_service._verify_signature(str(uuid.uuid4()), ".pdf")
    assert exc.value.code == "attachment_unreadable"


# --------------------------------------------------------------------------- #
# storage：流式累计 + 超限/中断都清场
# --------------------------------------------------------------------------- #
async def test_storage_save_aborts_over_cap_and_leaves_no_file(tmp_path):
    storage = LocalStorage(base_dir=tmp_path)
    user_id = uuid.uuid4()
    big = b"a" * (3 * 1024 * 1024)

    with pytest.raises(UploadTooLargeError) as exc:
        await storage.save(
            _upload("big.txt", big), user_id, allowed_extensions={".txt"}, max_bytes=1024
        )
    assert exc.value.limit_bytes == 1024
    # Nothing on disk: the partial object is removed the moment the cap is crossed.
    assert list((tmp_path / str(user_id)).glob("*")) == []


async def test_storage_save_keeps_files_under_the_cap(tmp_path):
    storage = LocalStorage(base_dir=tmp_path)
    user_id = uuid.uuid4()
    key = await storage.save(
        _upload("ok.txt", b"hello"), user_id, allowed_extensions={".txt"}, max_bytes=1024
    )
    assert os.path.exists(key)
    assert verified_size(key) == 5


async def test_storage_save_removes_partial_file_when_the_stream_dies(tmp_path):
    """Cancelled / failed mid-body must not leave an orphan object either."""
    storage = LocalStorage(base_dir=tmp_path)
    user_id = uuid.uuid4()
    dying = _DyingUpload("half.txt", [b"x" * 4096])

    with pytest.raises(_Cancelled):
        await storage.save(dying, user_id, allowed_extensions={".txt"}, max_bytes=10**6)

    assert list((tmp_path / str(user_id)).glob("*")) == []


# --------------------------------------------------------------------------- #
# document_service.upload：类型 + 大小 + 真实 file_size
# --------------------------------------------------------------------------- #
async def _seed_kb(db_session) -> tuple[User, KnowledgeBase]:
    suffix = uuid.uuid4().hex[:10]
    user = User(
        email=f"up-{suffix}@example.com",
        username=f"up-{suffix}",
        password_hash="x",
        role="user",
        is_active=True,
    )
    db_session.add(user)
    await db_session.commit()
    kb = KnowledgeBase(user_id=user.id, name=f"kb-{suffix}")
    db_session.add(kb)
    await db_session.commit()
    await db_session.refresh(kb)
    await db_session.refresh(user)
    return user, kb


async def _docs_of(db_session, kb_id) -> list[Document]:
    return list(
        (
            await db_session.execute(select(Document).where(Document.knowledge_base_id == kb_id))
        ).scalars().all()
    )


async def test_kb_upload_rejects_content_that_is_not_its_extension(db_session):
    user, kb = await _seed_kb(db_session)
    with pytest.raises(AppException) as exc:
        await document_service.upload(
            db_session,
            kb,
            user,
            _upload("invoice.pdf", b"<script>alert(1)</script>", "application/pdf"),
        )
    assert exc.value.status_code == 400
    assert exc.value.code == "document_signature_mismatch"
    assert "不一致" in exc.value.message

    # Rejected bytes must not stay on disk, and no row may exist.
    left = list((get_storage().base_dir / str(user.id)).glob("*.pdf"))
    assert left == []
    assert await _docs_of(db_session, kb.id) == []


async def test_kb_upload_records_the_real_size(db_session):
    """``Document.file_size`` 以前恒为 0，UI 与限额都读到「空文件」。"""
    user, kb = await _seed_kb(db_session)
    data = b"# Title\n\nbody text line\n" * 40
    doc = await document_service.upload(db_session, kb, user, _upload("note.md", data))
    try:
        assert doc.file_size == len(data)
        assert doc.file_size > 0
        assert os.path.exists(doc.file_path)
        assert doc.id is not None
    finally:
        await get_storage().delete(doc.file_path)


async def test_kb_upload_enforces_the_cap_while_streaming(db_session, monkeypatch):
    """上限在写盘途中生效，而且不采信客户端自报的 size（可以为 None / 谎报）。"""
    monkeypatch.setattr(get_settings(), "MAX_UPLOAD_MB", 1, raising=False)
    user, kb = await _seed_kb(db_session)
    lying = UploadFile(
        io.BytesIO(b"a" * (2 * 1024 * 1024)),
        filename="note.md",
        size=10,  # declares 10 bytes for a 2 MiB body
    )
    with pytest.raises(AppException) as exc:
        await document_service.upload(db_session, kb, user, lying)
    assert exc.value.status_code == 413
    assert exc.value.code == "document_too_large"
    assert list((get_storage().base_dir / str(user.id)).glob("*.md")) == []


async def test_kb_upload_of_disallowed_extension_is_a_400_not_a_500(db_session):
    """附件提升为知识库文档时，扩展名不在 KB 白名单：以前抛 ValueError → 500。"""
    user, kb = await _seed_kb(db_session)
    with pytest.raises(AppException) as exc:
        await document_service.upload(
            db_session, kb, user, _upload("shot.png", b"\x89PNG\r\n\x1a\n", "image/png")
        )
    assert exc.value.status_code == 400
    assert exc.value.code == "document_type_not_allowed"


# --------------------------------------------------------------------------- #
# 路由层：/api/knowledge-bases/{kb_id}/documents
# --------------------------------------------------------------------------- #
@pytest.fixture
def no_background_index(monkeypatch):
    """端点测试里摘掉「叫醒本进程 worker」这一步。

    上传不再排后台任务，而是把 documents 行入队（见 app.services.ingestion_queue），
    所以已经没有后台任务可停；但 ``notify_ingestion_worker`` 会立刻叫醒本进程的领取
    循环 —— 测试环境里若装了 worker，它就会真去索引这个文件。入队本身保留：那正是
    上传端点要断言的行为。
    """
    monkeypatch.setattr("app.api.documents.notify_ingestion_worker", lambda: False)


async def _make_kb(db_session) -> KnowledgeBase:
    kb = KnowledgeBase(user_id=_SEEDED, name=f"kb-{uuid.uuid4().hex[:8]}")
    db_session.add(kb)
    await db_session.commit()
    await db_session.refresh(kb)
    return kb


async def test_upload_endpoint_accepts_honest_file_and_reports_size(
    client, db_session, no_background_index
):
    kb = await _make_kb(db_session)
    r = await client.post(
        f"/api/knowledge-bases/{kb.id}/documents",
        files={"file": ("honest.pdf", _REAL_PDF, "application/pdf")},
        headers=auth_headers(),
    )
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["file_size"] == len(_REAL_PDF)
    assert body["file_type"] == ".pdf"

    path = (
        await db_session.execute(
            select(Document.file_path).where(Document.id == uuid.UUID(body["id"]))
        )
    ).scalar_one()
    await get_storage().delete(path)


async def test_upload_endpoint_rejects_spoofed_extension(client, db_session, no_background_index):
    kb = await _make_kb(db_session)
    r = await client.post(
        f"/api/knowledge-bases/{kb.id}/documents",
        files={"file": ("fake.pdf", b"MZ\x90\x00executables", "application/pdf")},
        headers=auth_headers(),
    )
    assert r.status_code == 400, r.text
    assert r.json()["code"] == "document_signature_mismatch"
    assert await _docs_of(db_session, kb.id) == []
