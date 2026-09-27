"""`ingest_claim_version`：被接管的摄取任务不能再用旧结果写回。

队列原有的归属判断是 ``ingest_claimed_by == worker_id``。这个名字不是身份：
同一台机器重启后 worker_id 可以完全一样，而 ``ingest_attempts`` 会被 ``enqueue``
归零，于是「我领的那一次」和「现在这一次」可能拿着同样的名字和同样的计数 ——
卡死的第一次恢复后照样能把接管者已经落库的状态改写成自己的结论（indexed /
failed / 重试时间全被覆盖）。这几条测试盯的就是这个 ABA：领取时随条件 UPDATE
一起 +1 的版本号，是写回的唯一通行证。

（这些断言打在真 SQLite 上：整套机制就是一串条件 UPDATE，正确性等于「两个竞争者、
一个赢家」，用 mock 测等于什么都没测。）
"""
from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import select, update

from app.models import Document, KnowledgeBase
from app.services import document_service
from app.services.ingestion_queue import (
    Claim,
    IngestionOutcome,
    claim,
    claim_job,
    enqueue,
    finish,
    renew,
)

SEEDED_USER = uuid.UUID("00000000-0000-0000-0000-000000000001")


def _now() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


def _settings(**over) -> SimpleNamespace:
    base = {
        "INGEST_LEASE_TTL_SECONDS": 300,
        "INGEST_LEASE_RENEW_SECONDS": 60,
        "INGEST_MAX_ATTEMPTS": 3,
        "INGEST_BACKOFF_BASE_SECONDS": 30,
        "INGEST_POLL_INTERVAL_SECONDS": 0.05,
    }
    base.update(over)
    return SimpleNamespace(**base)


async def _doc(db, *, status: str = "pending", name: str = "claim") -> Document:
    kb = KnowledgeBase(user_id=SEEDED_USER, name=f"{name}-kb-{uuid.uuid4().hex[:6]}")
    db.add(kb)
    await db.flush()
    doc = Document(
        knowledge_base_id=kb.id,
        filename=f"{name}.txt",
        file_path=f"/tmp/{name}-{uuid.uuid4().hex[:6]}.txt",
        file_type=".txt",
        status=status,
    )
    db.add(doc)
    await db.commit()
    return doc


async def _row(db, document_id) -> Document:
    return (
        await db.execute(
            select(Document)
            .where(Document.id == document_id)
            .execution_options(populate_existing=True)
        )
    ).scalar_one()


async def _expire_lease(db, document_id) -> None:
    await db.execute(
        update(Document)
        .where(Document.id == document_id)
        .values(ingest_lease_expires_at=_now() - timedelta(seconds=1))
    )
    await db.commit()


@pytest.fixture(autouse=True)
async def _park_foreign_documents(db_session):
    """``claim_job`` 是全局的，而测试库跨文件共享：把别人的行先停到取不到的地方。"""
    await db_session.execute(
        update(Document)
        .where(Document.status.in_(("pending", "parsing", "chunking", "embedding")))
        .values(ingest_next_retry_at=datetime.now(UTC) + timedelta(days=3650))
    )
    await db_session.commit()
    yield


# --------------------------------------------------------------------------- #
# 领取：版本号的来源
# --------------------------------------------------------------------------- #
async def test_claim_hands_back_a_monotonic_version(db_session):
    doc = await _doc(db_session)
    first = await claim_job(db_session, worker_id="w1", settings=_settings())
    assert isinstance(first, Claim)
    assert first.document_id == doc.id
    assert first.version == (await _row(db_session, doc.id)).ingest_claim_version == 1

    await _expire_lease(db_session, doc.id)
    second = await claim_job(db_session, worker_id="w2", settings=_settings())
    assert second.version == 2
    assert second.version != first.version

    # 老接口只回 id，形状保持不变（历史调用方与既有测试仍可用）。
    await _expire_lease(db_session, doc.id)
    assert await claim(db_session, worker_id="w3", settings=_settings()) == doc.id
    assert (await _row(db_session, doc.id)).ingest_claim_version == 3


# --------------------------------------------------------------------------- #
# 僵尸写回
# --------------------------------------------------------------------------- #
async def test_zombie_writeback_is_dropped(db_session):
    """卡死的第一次恢复后写回：必须整条丢弃，接管者的状态一个字都不改。"""
    doc = await _doc(db_session)
    zombie = await claim_job(db_session, worker_id="w1", settings=_settings())
    await _expire_lease(db_session, doc.id)

    takeover = await claim_job(db_session, worker_id="w2", settings=_settings())
    # 接管者那一轮跑完了：终态由流水线写，finish 只负责收尾。
    await db_session.execute(
        update(Document)
        .where(Document.id == doc.id)
        .values(status="indexed", chunk_count=42)
    )
    await db_session.commit()
    assert (
        await finish(
            db_session,
            doc.id,
            worker_id="w2",
            outcome=IngestionOutcome(ok=True),
            settings=_settings(),
            claim_version=takeover.version,
        )
        == "done"
    )

    verdict = await finish(
        db_session,
        doc.id,
        worker_id="w1",
        outcome=IngestionOutcome(ok=False, error="慢到过期的 provider", retryable=True),
        settings=_settings(),
        claim_version=zombie.version,
    )
    assert verdict == "lost"
    row = await _row(db_session, doc.id)
    assert (row.status, row.chunk_count, row.error_message) == ("indexed", 42, None)
    assert row.ingest_claimed_by is None


async def test_takeover_can_still_write_its_own_result(db_session):
    """丢弃僵尸写回不等于把行锁死：接管者那一轮照常收尾。"""
    doc = await _doc(db_session)
    stale = await claim_job(db_session, worker_id="w1", settings=_settings())
    await _expire_lease(db_session, doc.id)
    current = await claim_job(db_session, worker_id="w2", settings=_settings())

    assert (
        await finish(
            db_session,
            doc.id,
            worker_id="w1",
            outcome=IngestionOutcome(ok=True),
            settings=_settings(),
            claim_version=stale.version,
        )
        == "lost"
    )
    assert (
        await finish(
            db_session,
            doc.id,
            worker_id="w2",
            outcome=IngestionOutcome(ok=False, error="嵌入服务超时"),
            settings=_settings(),
            claim_version=current.version,
        )
        == "retry"
    )
    row = await _row(db_session, doc.id)
    assert row.status == "pending" and "超时" in row.error_message


async def test_reset_attempts_does_not_replay_a_stale_claim(db_session):
    """``ingest_attempts`` 当不了通行证：它会回到同一个值。版本号不会。"""
    doc = await _doc(db_session)
    first = await claim_job(db_session, worker_id="w1", settings=_settings())
    assert (
        await finish(
            db_session,
            doc.id,
            worker_id="w1",
            outcome=IngestionOutcome(ok=False, error="临时故障"),
            settings=_settings(),
            claim_version=first.version,
        )
        == "retry"
    )
    # 人工重新入队：attempt 计数被清零，名字也还是同一个 worker。
    assert await enqueue(db_session, doc.id, reset_attempts=True) is True
    await db_session.execute(
        update(Document)
        .where(Document.id == doc.id)
        .values(ingest_next_retry_at=None)
    )
    await db_session.commit()

    again = await claim_job(db_session, worker_id="w1", settings=_settings())
    row = await _row(db_session, doc.id)
    assert row.ingest_attempts == 1  # 与第一次领取完全同形 —— ABA 的温床
    assert again.version == first.version + 1

    assert (
        await finish(
            db_session,
            doc.id,
            worker_id="w1",
            outcome=IngestionOutcome(ok=True),
            settings=_settings(),
            claim_version=first.version,
        )
        == "lost"
    )
    assert (await _row(db_session, doc.id)).status == "parsing"


# --------------------------------------------------------------------------- #
# 续租 + 流水线自己的进度写回
# --------------------------------------------------------------------------- #
async def test_renew_refuses_a_stale_claim(db_session):
    doc = await _doc(db_session)
    stale = await claim_job(db_session, worker_id="w1", settings=_settings())
    await _expire_lease(db_session, doc.id)
    current = await claim_job(db_session, worker_id="w1", settings=_settings())

    assert (
        await renew(
            db_session,
            doc.id,
            worker_id="w1",
            settings=_settings(),
            claim_version=stale.version,
        )
        is False
    )
    assert (
        await renew(
            db_session,
            doc.id,
            worker_id="w1",
            settings=_settings(),
            claim_version=current.version,
        )
        is True
    )


async def test_pipeline_progress_write_is_guarded(db_session):
    """进度写回（parsing/chunking/…）走同一张通行证，而不只是收尾那一次。"""
    doc = await _doc(db_session)
    mine = await claim_job(db_session, worker_id="w1", settings=_settings())
    loaded = await db_session.get(Document, doc.id)

    await document_service._write_progress(
        db_session, loaded, mine.version, status="chunking"
    )
    assert (await _row(db_session, doc.id)).status == "chunking"

    with pytest.raises(document_service._ClaimLost):
        await document_service._write_progress(
            db_session, loaded, mine.version - 1, status="indexed", chunk_count=7
        )
    row = await _row(db_session, doc.id)
    assert (row.status, row.chunk_count) == ("chunking", 0)


async def test_pipeline_aborts_a_run_that_was_taken_over(db_session, tmp_path):
    """僵尸 worker 跑 index_document：第一步写回就发现自己不再是领取者，立刻收手。"""
    body = tmp_path / "body.txt"
    body.write_text("足够长的中文正文，可以真的被切分。" * 20, encoding="utf-8")
    doc = await _doc(db_session, name="zombie-run")
    doc.file_path = str(body)
    await db_session.commit()

    stale = await claim_job(db_session, worker_id="w1", settings=_settings())
    await _expire_lease(db_session, doc.id)
    current = await claim_job(db_session, worker_id="w2", settings=_settings())

    outcome = await document_service.index_document(
        db_session, doc.id, claim_version=stale.version
    )
    assert outcome.ok is False and outcome.retryable is False
    assert "接管" in outcome.error

    row = await _row(db_session, doc.id)
    assert (row.status, row.ingest_claimed_by) == ("parsing", "w2")
    assert row.ingest_claim_version == current.version
    # 没有留下半截分块：接管者的那一轮不受影响。
    assert row.chunk_count == 0
