"""Credit/quota admission correctness under real Postgres concurrency (finding 36).

Why a separate file, and why it is skipped everywhere but the Postgres CI job:

* The suite's default database is in-memory SQLite (``tests/conftest.py``).
  SQLite has one writer and a single connection, so ``SELECT ... FOR UPDATE``
  is a no-op there and every "two transactions race" test is vacuous — it
  passes whether or not the row lock is actually applied. These tests therefore
  assert something that only a real MVCC engine can falsify.
* They talk to the **migrated** schema (the CI job runs ``alembic upgrade head``
  first), not ``create_all``, so they also prove the partial unique index
  ``uq_credit_ledger_ref`` — the thing that makes a duplicated charge a no-op —
  exists with the shape the code assumes. A migration that renames it silently
  turns idempotency off, and only a real-DB run notices.

Skips unless ``DATABASE_URL`` points at Postgres, so the sqlite gate reports them
as skipped rather than failing.
"""
from __future__ import annotations

import asyncio
import os
import uuid

import pytest

pytestmark = pytest.mark.skipif(
    not os.environ.get("DATABASE_URL", "").startswith("postgresql"),
    reason="requires a real Postgres (run by the CI backend-postgres job)",
)

_RESERVE = 10  # deliberately small: 5 admitted turns from a 55-credit account


def _dsn() -> str:
    return os.environ["DATABASE_URL"]


@pytest.fixture
async def engine():
    from sqlalchemy.ext.asyncio import create_async_engine

    eng = create_async_engine(_dsn(), pool_pre_ping=True)
    yield eng
    await eng.dispose()


@pytest.fixture
def session_factory(engine):
    from sqlalchemy.ext.asyncio import async_sessionmaker

    return async_sessionmaker(engine, expire_on_commit=False)


async def _make_user_and_account(session_factory, balance: int):
    """Create a real user + credit account at ``balance``.

    Goes through the FK'd ``users`` table on purpose: a test that inserted into
    ``credit_accounts`` alone would pass even with the FK or the account-row
    backfill missing, which is exactly the schema property under test here.
    """
    from app.models import CreditAccount, User

    user_id = uuid.uuid4()
    async with session_factory() as db:
        db.add(
            User(
                id=user_id,
                email=f"pg-concurrency-{user_id.hex[:12]}@example.test",
                hashed_password="x",
                role="user",
            )
        )
        db.add(CreditAccount(user_id=user_id, balance=balance))
        await db.commit()
    return user_id


async def _balance(session_factory, user_id) -> int:
    from sqlalchemy import select

    from app.models import CreditAccount

    async with session_factory() as db:
        return int(
            (
                await db.execute(
                    select(CreditAccount.balance).where(CreditAccount.user_id == user_id)
                )
            ).scalar_one()
        )


async def test_concurrent_admit_turn_never_oversells_the_balance(session_factory):
    """20 simultaneous admits against a balance that funds 5 of them.

    The pre-fix code read the balance with an unlocked ``db.get()`` and compared
    it to 0, so all 20 coroutines saw 55 and all 20 were admitted — the classic
    TOCTOU. With the row lock serialised, exactly ``balance // reserve`` admits
    succeed and the balance lands at the remainder, never below zero.
    """
    from app.credits import CreditPolicy
    from app.services import credit_service

    policy = CreditPolicy(enforced=True, turn_reserve=_RESERVE)
    funded_turns = 5
    user_id = await _make_user_and_account(session_factory, _RESERVE * funded_turns + 5)

    async def admit_one() -> str:
        # Each admit runs in its own session: sharing one AsyncSession across
        # concurrent tasks is not a thing (it is not re-entrant), and real
        # requests each bring their own.
        try:
            hold = await credit_service.admit_turn(
                user_id, policy=policy, session_factory=session_factory
            )
        except credit_service.CreditError:
            return "rejected"
        return "admitted" if hold is not None else "no-op"

    results = await asyncio.gather(*(admit_one() for _ in range(20)))
    admitted = results.count("admitted")
    remaining = await _balance(session_factory, user_id)

    assert admitted == funded_turns, f"oversold: {admitted} admits from {funded_turns} funded"
    assert remaining == _RESERVE * funded_turns + 5 - admitted * _RESERVE
    assert remaining >= 0, "admission drove the balance negative"


async def test_turn_hold_release_is_idempotent_under_racing_releases(session_factory):
    """Two concurrent releases of one hold must refund exactly once.

    The chat path releases in a ``finally``; a retrying client or a
    cancellation + exception racing can call it twice. The unique partial index
    on ``(ref_type, ref_id, reason)`` is the only thing preventing a double
    refund — i.e. minting credits out of a race.
    """
    from app.credits import CreditPolicy
    from app.services import credit_service

    policy = CreditPolicy(enforced=True, turn_reserve=_RESERVE)
    user_id = await _make_user_and_account(session_factory, _RESERVE * 3)
    hold = await credit_service.admit_turn(
        user_id, policy=policy, session_factory=session_factory
    )
    assert hold is not None
    start = await _balance(session_factory, user_id)

    await asyncio.gather(
        *(
            credit_service.release_turn_hold(hold, session_factory=session_factory)
            for _ in range(4)
        )
    )

    assert await _balance(session_factory, user_id) == start + _RESERVE


async def test_ledger_sum_matches_balance_after_concurrent_charges(session_factory):
    """Concurrent charges + grants keep ``sum(delta) == balance``.

    This is the reconciliation invariant ops relies on. It fails the moment any
    writer computes ``balance_after`` from a stale read, which is what happens
    without ``FOR UPDATE`` + ``populate_existing`` under real row-level
    concurrency.
    """
    from sqlalchemy import func, select

    from app.models import CreditLedger
    from app.services import credit_service

    user_id = await _make_user_and_account(session_factory, 0)

    async def grant_one(i: int) -> None:
        async with session_factory() as db:
            await credit_service.grant(
                db, user_id, amount=100, reason="test_grant", ref_type="test", ref_id=f"g{i}"
            )
            await db.commit()

    async def charge_one(i: int) -> None:
        async with session_factory() as db:
            await credit_service.charge_usage(
                db,
                user_id,
                amount=40,
                ref_type="test",
                ref_id=f"c{i}",
            )
            await db.commit()

    await asyncio.gather(*(grant_one(i) for i in range(8)))
    await asyncio.gather(*(charge_one(i) for i in range(8)))

    async with session_factory() as db:
        ledger_sum = (
            await db.execute(
                select(func.coalesce(func.sum(CreditLedger.delta), 0)).where(
                    CreditLedger.user_id == user_id
                )
            )
        ).scalar_one()
    assert int(ledger_sum) == await _balance(session_factory, user_id)


async def test_migrated_schema_has_the_unique_ref_index(session_factory):
    """``uq_credit_ledger_ref`` exists on the migrated DB and is partial.

    Every idempotency claim above rests on this index. Assert it directly so a
    migration that drops or de-partitions it fails here with a readable message
    instead of showing up as duplicated charges in production.
    """
    from sqlalchemy import text

    async with session_factory() as db:
        row = (
            await db.execute(
                text(
                    "SELECT indexdef FROM pg_indexes "
                    "WHERE tablename = 'credit_ledger' AND indexname = 'uq_credit_ledger_ref'"
                )
            )
        ).scalar()
    assert row is not None, "uq_credit_ledger_ref missing — idempotency is off"
    assert "WHERE" in row.upper(), f"index is not partial, so admin adjusts collide: {row}"
