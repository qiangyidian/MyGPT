"""批次 C ③：上传白名单 ↔ 解析器同源（条目 19）。

两侧各有一份扩展名清单时必然漂移：解析器能读的类型被挡在门外（用户传不了，
功能等于没有），解析器读不了的类型被放行（文件落库、计费、显示成一行记录，
然后索引阶段死在「不支持的文件类型」，文档永久停在 failed —— 这条路用户没有
任何补救手段）。

现在允许集合 = ``ALLOWED_UPLOAD_EXT`` ∩ 解析器注册表，且前端从
``GET /api/upload-capabilities`` 读同一份答案，不再自己抄一份。
"""
from __future__ import annotations

import uuid

import pytest
from sqlalchemy import select

from app.core.config import Settings, get_settings
from app.core.file_signatures import FileSignatureError, check_file_signature
from app.models import Document, KnowledgeBase
from app.rag.parsers import SUPPORTED_EXTS
from tests.conftest import auth_headers

_SEEDED = uuid.UUID("00000000-0000-0000-0000-000000000001")

# Ship default for the allow-list, read off the model rather than the live
# settings object: a developer's local ``.env`` is allowed to pin a narrower
# list, and these assertions are about what a fresh deployment gets.
DEFAULT_UPLOAD_EXT: str = Settings.model_fields["ALLOWED_UPLOAD_EXT"].default


@pytest.fixture
def default_allowlist(monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "ALLOWED_UPLOAD_EXT", DEFAULT_UPLOAD_EXT)
    return settings


# --------------------------------------------------------------------------- #
# 不变式
# --------------------------------------------------------------------------- #
def test_effective_allowlist_never_offers_an_unparseable_type():
    """允许集合必须是解析器注册表的子集 —— 这是本条目唯一需要长期守住的性质。"""
    allowed = get_settings().allowed_extensions
    assert allowed, "白名单不能是空的（交集算错了才会这样）"
    assert allowed <= SUPPORTED_EXTS, sorted(allowed - SUPPORTED_EXTS)


def test_default_config_does_not_lock_out_a_parser():
    """反向：默认配置不该挡住注册表里任何能读的格式。

    新加解析器却忘了配白名单，正是这次修复要防的那类漂移。
    """
    configured = {e.strip().lower() for e in DEFAULT_UPLOAD_EXT.split(",") if e.strip()}
    assert configured >= SUPPORTED_EXTS, sorted(SUPPORTED_EXTS - configured)


def test_widened_default_covers_the_previously_blocked_formats(default_allowlist):
    """这些类型解析器一直支持，只是没进白名单。"""
    allowed = default_allowlist.allowed_extensions
    for ext in (".pptx", ".markdown", ".log", ".json", ".html", ".epub", ".rtf"):
        assert ext in allowed, ext


def test_configured_but_unparseable_ext_is_dropped_and_reported(monkeypatch, caplog):
    from app.core import config as config_module

    monkeypatch.setattr(config_module, "_warned_unparseable_exts", set())
    settings = get_settings()
    original = settings.ALLOWED_UPLOAD_EXT
    monkeypatch.setattr(settings, "ALLOWED_UPLOAD_EXT", ".pdf,.exe,.pdf")
    try:
        with caplog.at_level("WARNING"):
            allowed = settings.allowed_extensions
        assert allowed == {".pdf"}
        assert ".exe" in caplog.text
        # 只报一次：属性在每次上传时都会被读。
        caplog.clear()
        assert settings.allowed_extensions == {".pdf"}
        assert ".exe" not in caplog.text
    finally:
        monkeypatch.setattr(settings, "ALLOWED_UPLOAD_EXT", original)


def test_allowlist_tolerates_entries_without_a_leading_dot(monkeypatch):
    settings = get_settings()
    original = settings.ALLOWED_UPLOAD_EXT
    monkeypatch.setattr(settings, "ALLOWED_UPLOAD_EXT", "pdf, .DOCX ")
    try:
        assert settings.allowed_extensions == {".pdf", ".docx"}
    finally:
        monkeypatch.setattr(settings, "ALLOWED_UPLOAD_EXT", original)


# --------------------------------------------------------------------------- #
# 魔数表要跟上新放的行
# --------------------------------------------------------------------------- #
def _write(tmp_path, name: str, head: bytes) -> str:
    path = tmp_path / name
    path.write_bytes(head)
    return str(path)


def test_newly_allowed_binary_formats_still_need_their_container(tmp_path):
    """.epub/.rtf 进了白名单，就得和别的办公格式一样过魔数校验。"""
    check_file_signature(_write(tmp_path, "a.epub", b"PK\x03\x04mimetype"), ".epub")
    check_file_signature(_write(tmp_path, "a.rtf", b"{\\rtf1\\ansi"), ".rtf")
    for name, head, ext in (
        ("bad.epub", b"MZ\x90\x00not a book", ".epub"),
        ("bad.rtf", b"%PDF-1.4", ".rtf"),
    ):
        with pytest.raises(FileSignatureError) as exc:
            check_file_signature(_write(tmp_path, name, head), ext)
        assert exc.value.reason == "mismatch"


def test_text_formats_have_no_magic_and_are_not_invented_ones(tmp_path):
    """HTML/日志没有可靠的魔数，按扩展名放行（与 .txt/.md 同一口径）。"""
    for ext in (".html", ".htm", ".log", ".markdown"):
        check_file_signature(_write(tmp_path, f"x{ext}", b"<whatever>"), ext)


# --------------------------------------------------------------------------- #
# 路由层
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


async def test_capabilities_endpoint_matches_the_enforced_rule(client):
    r = await client.get("/api/upload-capabilities", headers=auth_headers())
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["allowed_extensions"] == sorted(get_settings().allowed_extensions)
    assert body["max_upload_mb"] == get_settings().MAX_UPLOAD_MB
    # 契约：交给 <input accept> 用的，必须是带点的扩展名。
    assert all(e.startswith(".") for e in body["allowed_extensions"])


async def test_endpoint_accepts_a_format_that_parse_but_was_blocked(
    client, db_session, no_background_index, default_allowlist
):
    kb = await _make_kb(db_session)
    r = await client.post(
        f"/api/knowledge-bases/{kb.id}/documents",
        files={"file": ("app.log", b"line one\nline two\n", "text/plain")},
        headers=auth_headers(),
    )
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["file_type"] == ".log"
    doc = await db_session.get(Document, uuid.UUID(body["id"]))
    assert doc is not None
    await _delete_file(db_session, doc)


async def test_endpoint_still_rejects_what_no_parser_can_read(
    client, db_session, no_background_index
):
    kb = await _make_kb(db_session)
    r = await client.post(
        f"/api/knowledge-bases/{kb.id}/documents",
        files={"file": ("tool.exe", b"MZ\x90\x00", "application/octet-stream")},
        headers=auth_headers(),
    )
    assert r.status_code == 400, r.text
    assert await _docs_of(db_session, kb.id) == []


async def _docs_of(db_session, kb_id) -> list:
    return list(
        (
            await db_session.execute(
                select(Document.id).where(Document.knowledge_base_id == kb_id)
            )
        ).scalars().all()
    )


async def _delete_file(db_session, doc: Document) -> None:
    from app.core.storage import get_storage

    if doc.file_path:
        await get_storage().delete(doc.file_path)
