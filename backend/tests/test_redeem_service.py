"""兑换码服务测试：生成、兑换、一次性、过期、作废、明文不落库。"""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, UTC

import pytest

from app.credits import normalize_code, hash_code
from app.models import RedeemCode
from app.services import credit_service, redeem_service

ADMIN = uuid.UUID("00000000-0000-0000-0000-0000000000aa")


async def _make_batch(db, count=3, credits=500, expires_at=None):
    return await redeem_service.create_batch(
        db,
        admin_id=ADMIN,
        name="测试批次",
        credits_per_code=credits,
        count=count,
        expires_at=expires_at,
    )


async def test_create_batch_returns_plaintext_once(db_session):
    batch, codes = await _make_batch(db_session, count=5, credits=500)
    assert len(codes) == 5
    assert len(set(codes)) == 5
    assert batch.id is not None

    # 库里只有哈希，没有明文
    rows = (await db_session.execute(
        RedeemCode.__table__.select().where(RedeemCode.batch_id == batch.id)
    )).all()
    stored = {row.code_hash for row in rows}
    assert stored == {hash_code(normalize_code(c)) for c in codes}
    for row in rows:
        assert row.status == "active"
        assert row.redeemed_by is None
        assert len(row.code_prefix) == 6

async def test_create_batch_rejects_count_over_cap(db_session):
    from app.credits import CreditPolicy, set_credit_policy

    set_credit_policy(CreditPolicy(max_codes_per_batch=10))
    try:
        with pytest.raises(credit_service.CreditError) as exc:
            await _make_batch(db_session, count=11)
        assert exc.value.code == "redeem_batch_too_large"
    finally:
        set_credit_policy(None)


async def test_create_batch_rejects_non_positive_credits(db_session):
    with pytest.raises(credit_service.CreditError) as exc:
        await redeem_service.create_batch(
            db_session, admin_id=ADMIN, name="x", credits_per_code=0, count=1
        )
    assert exc.value.code == "redeem_batch_invalid_credits"


async def test_redeem_adds_credits_and_marks_code(db_session):
    batch, codes = await _make_batch(db_session, count=1, credits=500)
    user = uuid.uuid4()

    result = await redeem_service.redeem(db_session, user_id=user, raw_code=codes[0])
    assert result.credits_added == 500
    assert result.balance == 500
    assert result.batch_name == "测试批次"

    code = (await db_session.execute(
        RedeemCode.__table__.select().where(RedeemCode.batch_id == batch.id)
    )).first()
    assert code.status == "redeemed"
    assert code.redeemed_by == user
    assert code.redeemed_at is not None


async def test_redeem_is_insensitive_to_formatting(db_session):
    _, codes = await _make_batch(db_session, count=1, credits=100)
    messy = codes[0].lower().replace("-", " ")
    result = await redeem_service.redeem(db_session, user_id=uuid.uuid4(), raw_code=messy)
    assert result.credits_added == 100


async def test_redeem_twice_fails_and_does_not_double_credit(db_session):
    _, codes = await _make_batch(db_session, count=1, credits=500)
    user = uuid.uuid4()
    await redeem_service.redeem(db_session, user_id=user, raw_code=codes[0])

    with pytest.raises(credit_service.CreditError) as exc:
        await redeem_service.redeem(db_session, user_id=user, raw_code=codes[0])
    assert exc.value.code == "redeem_code_used"
    assert exc.value.status_code == 409

    account = await credit_service.read_account(db_session, user)
    assert account.balance == 500  # 没有被加第二次


async def test_redeem_second_user_cannot_use_same_code(db_session):
    _, codes = await _make_batch(db_session, count=1, credits=500)
    await redeem_service.redeem(db_session, user_id=uuid.uuid4(), raw_code=codes[0])
    with pytest.raises(credit_service.CreditError) as exc:
        await redeem_service.redeem(db_session, user_id=uuid.uuid4(), raw_code=codes[0])
    assert exc.value.code == "redeem_code_used"


async def test_redeem_unknown_code(db_session):
    with pytest.raises(credit_service.CreditError) as exc:
        await redeem_service.redeem(db_session, user_id=uuid.uuid4(), raw_code="ZZZZ-ZZZZ-ZZZZ-ZZZZ")
    assert exc.value.code == "redeem_code_not_found"
    assert exc.value.status_code == 404


async def test_redeem_empty_code(db_session):
    with pytest.raises(credit_service.CreditError) as exc:
        await redeem_service.redeem(db_session, user_id=uuid.uuid4(), raw_code="   ")
    assert exc.value.code == "redeem_code_not_found"


async def test_redeem_expired_code(db_session):
    from sqlalchemy import update

    from app.models import RedeemCodeBatch

    past_batch, codes = await _make_batch(db_session, count=1, credits=500)
    # 直接把批次改成已过期 —— create_batch 本身拒绝过去的有效期。
    past = datetime.now(UTC) - timedelta(days=1)
    await db_session.execute(
        update(RedeemCodeBatch)
        .where(RedeemCodeBatch.id == past_batch.id)
        .values(expires_at=past)
    )
    with pytest.raises(credit_service.CreditError) as exc:
        await redeem_service.redeem(db_session, user_id=uuid.uuid4(), raw_code=codes[0])
    assert exc.value.code == "redeem_code_expired"
    assert exc.value.status_code == 410


async def test_create_batch_rejects_past_expiry(db_session):
    past = datetime.now(UTC) - timedelta(hours=1)
    with pytest.raises(credit_service.CreditError) as exc:
        await _make_batch(db_session, count=1, expires_at=past)
    assert exc.value.code == "redeem_batch_invalid_expiry"
    assert exc.value.status_code == 400


async def test_create_batch_rejects_current_moment_expiry(db_session):
    # 提前一点点构造，保证执行断言时它已过期。
    just_now = datetime.now(UTC) - timedelta(seconds=1)
    with pytest.raises(credit_service.CreditError) as exc:
        await _make_batch(db_session, count=1, expires_at=just_now)
    assert exc.value.code == "redeem_batch_invalid_expiry"


async def test_create_batch_accepts_future_expiry(db_session):
    future = datetime.now(UTC) + timedelta(days=30)
    batch, codes = await _make_batch(db_session, count=1, expires_at=future)
    assert batch.expires_at is not None
    assert len(codes) == 1


async def test_create_batch_allows_none_expiry_means_never(db_session):
    batch, codes = await _make_batch(db_session, count=1, expires_at=None)
    assert batch.expires_at is None
    result = await redeem_service.redeem(db_session, user_id=uuid.uuid4(), raw_code=codes[0])
    assert result.credits_added == 500


async def test_redeem_code_expiring_in_the_future_works(db_session):
    future = datetime.now(UTC) + timedelta(days=1)
    _, codes = await _make_batch(db_session, count=1, credits=500, expires_at=future)
    result = await redeem_service.redeem(db_session, user_id=uuid.uuid4(), raw_code=codes[0])
    assert result.credits_added == 500


async def test_void_batch_kills_only_active_codes(db_session):
    batch, codes = await _make_batch(db_session, count=3, credits=500)
    # 先兑掉一个
    await redeem_service.redeem(db_session, user_id=uuid.uuid4(), raw_code=codes[0])

    voided = await redeem_service.void_batch(db_session, batch_id=batch.id)
    assert voided == 2  # 只剩 2 个 active

    with pytest.raises(credit_service.CreditError) as exc:
        await redeem_service.redeem(db_session, user_id=uuid.uuid4(), raw_code=codes[1])
    assert exc.value.code == "redeem_code_void"


async def test_void_batch_leaves_already_redeemed_alone(db_session):
    batch, codes = await _make_batch(db_session, count=2, credits=500)
    user = uuid.uuid4()
    await redeem_service.redeem(db_session, user_id=user, raw_code=codes[0])
    await redeem_service.void_batch(db_session, batch_id=batch.id)
    # 已兑换的分数不受作废影响
    account = await credit_service.read_account(db_session, user)
    assert account.balance == 500


async def test_list_batches_reports_progress(db_session):
    batch, codes = await _make_batch(db_session, count=4, credits=100)
    await redeem_service.redeem(db_session, user_id=uuid.uuid4(), raw_code=codes[0])
    await redeem_service.void_batch(db_session, batch_id=batch.id)

    rows = await redeem_service.list_batches(db_session)
    target = next(r for r in rows if r.batch.id == batch.id)
    assert target.total == 4
    assert target.redeemed == 1
    assert target.void == 3
    assert target.active == 0


async def test_list_codes_never_returns_plaintext(db_session):
    batch, _ = await _make_batch(db_session, count=3, credits=100)
    rows = await redeem_service.list_codes(db_session, batch_id=batch.id)
    assert len(rows) == 3
    for row in rows:
        # 只暴露前缀，且前缀不含分隔符
        assert len(row.code_prefix) == 6
        assert "-" not in row.code_prefix


# ---- CAS 机制 ------------------------------------------------------------- #

async def test_cas_rejects_a_code_whose_pre_read_was_stale(db_session):
    """CAS 的判定依据是 UPDATE 当时的 ``status``，不是之前的 SELECT。

    这正是"先 SELECT 判断再 UPDATE"会踩的坑：READ COMMITTED 下两个事务可以
    同时把同一张码读成 ``active``，然后**都**通过应用层判断、**都**去加分。
    CAS 把判定压进 UPDATE 的 WHERE，抢输的一方拿到 0 行。

    测试手段：先在一个会话里把码按 active 读出来（模拟第二个事务的陈旧预读），
    然后用另一个会话把它兑掉并提交，最后对那个陈旧对象执行 CAS —— 必须是 0 行。

    SQLite 无法制造真并发（单写者），但可以不依赖并发地把这个时序摆出来，
    验证的是同一段 SQL 语义。真多写者的部署冒烟见
    ``docs/credits-operations.md`` 的并发演练小节。
    """
    from datetime import datetime, UTC

    from sqlalchemy import select, update

    from app.credits import hash_code, normalize_code
    from app.models import RedeemCode

    batch, codes = await _make_batch(db_session, count=1, credits=500)
    await db_session.commit()

    code = (
        await db_session.execute(
            select(RedeemCode).where(
                RedeemCode.code_hash == hash_code(normalize_code(codes[0]))
            )
        )
    ).scalar_one()
    assert code.status == "active"  # 陈旧预读

    # 另一个事务抢先把这张码兑掉。
    await redeem_service.redeem(db_session, user_id=uuid.uuid4(), raw_code=codes[0])
    await db_session.commit()

    # 迟到的 CAS：必须 0 行。
    late = await db_session.execute(
        update(RedeemCode)
        .where(RedeemCode.id == code.id, RedeemCode.status == "active")
        .values(status="redeemed", redeemed_by=uuid.uuid4(), redeemed_at=datetime.now(UTC))
    )
    assert late.rowcount == 0


async def test_concurrent_grants_accumulate_rather_than_overwrite(db_session):
    """多次发放必须累加，不能被后一次覆盖。

    行锁（``SELECT ... FOR UPDATE`` + ``populate_existing``）保证读到的余额是最新
    的，所以每次都是"在最新值上加"而不是"在陈旧值上加"。SQLite 上这条退化为
    顺序执行，但同样能抓住"用陈旧值覆盖"这个缺陷 —— 那正是缺了
    ``populate_existing`` 或漏了行锁时的表现。
    """
    user = uuid.uuid4()
    await credit_service.get_or_create_account(db_session, user)
    await db_session.commit()

    for _ in range(5):
        await credit_service.grant(db_session, user, amount=100, reason="admin_adjust")
    await db_session.commit()

    account = await credit_service.read_account(db_session, user)
    assert account.balance == 500
    assert account.lifetime_granted == 500
