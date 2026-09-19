"""孤儿上传清扫不得删除仍被引用的文件。

``LocalStorage.save()`` 返回**绝对路径**，而 ChatAttachment / Artifact /
Document 三张表存的就是这个绝对路径。历史上本模块拿「相对路径」去比「绝对路径」
的引用集合，条件永不成立 → 每 6 小时把所有上传文件当孤儿删掉。这里钉住：
被任何一张表引用的文件必须存活，只有真正的孤儿才被回收。
"""
from __future__ import annotations

import io
import os
import time
import uuid

import pytest
from fastapi import UploadFile


from app.core.config import get_settings
from app.core.storage import LocalStorage
from app.models.artifact import Artifact
from app.models.chat_attachment import ChatAttachment
from app.models.conversation import Conversation
from app.models.document import Document
from app.models.knowledge_base import KnowledgeBase
from app.models.user import User
from app.services.retention import sweep_orphan_uploads
from tests.conftest import TestSessionLocal


async def _seed_user(db_session) -> User:
    suffix = uuid.uuid4().hex[:10]
    user = User(
        email=f"orphan-{suffix}@example.com",
        username=f"orphan-{suffix}",
        password_hash="not-a-real-hash",
        role="user",
        is_active=True,
    )
    db_session.add(user)
    await db_session.commit()
    await db_session.refresh(user)
    return user


async def _store(db_session, user: User, *, kind: str, with_row: bool = True) -> str:
    """Save a real file into LocalStorage; ``with_row`` decides if a row claims it."""
    storage = LocalStorage(base_dir=get_settings().STORAGE_DIR)
    key = await storage.save(
        UploadFile(filename=f"{kind}.txt", file=io.BytesIO(b"payload")),
        user.id,
        allowed_extensions={".txt"},
    )
    if not with_row:
        return key
    if kind == "attachment":
        conv = Conversation(user_id=user.id, title=f"conv-{uuid.uuid4().hex[:8]}")
        db_session.add(conv)
        await db_session.flush()
        db_session.add(
            ChatAttachment(
                user_id=user.id,
                conversation_id=conv.id,
                filename=f"{kind}.txt",
                original_filename=f"{kind}.txt",
                mime_type="text/plain",
                size_bytes=7,
                storage_key=key,
                status="ready",
                parse_status="ready",
                is_temporary=True,
            )
        )
    elif kind == "artifact":
        db_session.add(
            Artifact(
                owner_id=user.id,
                checksum="0" * 64,
                size=7,
                media_type="text/plain",
                storage_key=key,
                filename=f"{kind}.txt",
                source="upload",
            )
        )
    else:
        kb = KnowledgeBase(user_id=user.id, name=f"kb-{uuid.uuid4().hex[:8]}")
        db_session.add(kb)
        await db_session.flush()
        db_session.add(
            Document(
                knowledge_base_id=kb.id,
                filename=f"{kind}.txt",
                file_path=key,
                file_type=".txt",
                file_size=7,
                status="indexed",
                chunk_count=0,
            )
        )
    await db_session.commit()
    return key


def _age(path: str, *, seconds: float = 48 * 3600) -> None:
    old = time.time() - seconds
    os.utime(path, (old, old))


@pytest.fixture
def storage_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(get_settings(), "STORAGE_DIR", str(tmp_path))
    return tmp_path


async def test_referenced_files_survive_the_sweep(db_session, storage_dir):
    user = await _seed_user(db_session)
    kept = [
        await _store(db_session, user, kind="attachment"),
        await _store(db_session, user, kind="artifact"),
        await _store(db_session, user, kind="document"),
    ]
    orphan = await _store(db_session, user, kind="orphan", with_row=False)
    for path in [*kept, orphan]:
        _age(path)

    removed = await sweep_orphan_uploads(TestSessionLocal)

    assert removed == 1
    for path in kept:
        assert os.path.exists(path), f"{path} was deleted while still referenced"
    assert not os.path.exists(orphan)


async def test_fresh_files_are_not_touched(db_session, storage_dir):
    user = await _seed_user(db_session)
    referenced = await _store(db_session, user, kind="document")
    unreferenced = await _store(db_session, user, kind="pending", with_row=False)

    removed = await sweep_orphan_uploads(TestSessionLocal)

    assert removed == 0
    assert os.path.exists(referenced)
    assert os.path.exists(unreferenced)


async def test_sweep_skips_when_reference_set_is_empty(db_session, storage_dir):
    """引用集合为空（新装库/查询无果）时宁可不删，也绝不做批量删除。

    用空的引用查询结果来构造这一形态 —— 不去删全库的引用行，测试库是跨用例
    共享的一条连接，动整表会污染别的用例。
    """
    user = await _seed_user(db_session)
    stray = await _store(db_session, user, kind="stray", with_row=False)
    _age(stray)

    class _NoRows:
        def scalars(self):
            return self

        def all(self):
            return []

    class _EmptySession:
        async def execute(self, *_a, **_kw):
            return _NoRows()

    class _EmptyFactory:
        def __call__(self):
            return self

        async def __aenter__(self):
            return _EmptySession()

        async def __aexit__(self, *_exc):
            return False

    removed = await sweep_orphan_uploads(_EmptyFactory())

    assert removed == 0
    assert os.path.exists(stray)
