"""Postgres advisory-lock leader election for singleton loops (finding 39).

Several processes in this deploy want to run *exactly one* periodic loop: the
stale-job / retention sweepers (every API replica used to start one, which is
what this replaced), and the stream stalled-pending reclaim (every worker would
otherwise requeue the same entries). This module gives them a
mutual-exclusion primitive that needs no new table, no expiry clock and no
cleanup job.

Why a **session-level advisory lock** rather than a row with a heartbeat lease:

* the lock lives on the database *connection*, so a crashed holder's lock is
  released by the server itself the moment its backend dies — there is no
  "lease TTL" to size, and no stale row to reap;
* ``pg_try_advisory_lock`` never blocks, so a losing candidate keeps running its
  non-singleton work instead of stalling a whole event loop;
* it is transaction-independent, so it cannot be dropped by an unrelated COMMIT
  in the application's own session.

Failover semantics (what an operator should expect)
---------------------------------------------------
* **Steady state**: exactly one process holds the lock; it runs the loop. Every
  other process polls ``try_acquire()`` on ``LEADER_HEARTBEAT_SECONDS`` and stays
  idle. There is no priority: leadership goes to whoever asks first.
* **Clean shutdown**: :meth:`AdvisoryLeader.release` unlocks and returns the
  connection to the pool, so the next poller takes over within one interval.
* **Crash / OOMKill / node loss**: the kernel closes the socket, Postgres drops
  the backend, and the lock is released server-side. A standby promotes on its
  next poll. Worst-case detection time is therefore
  ``LEADER_HEARTBEAT_SECONDS``, not the lock's own lifetime — there is no
  lock-expiry window in which nobody is leader.
* **Network partition**: the holder keeps its connection (and so its lock) and
  continues as the single leader; the partitioned minority cannot acquire. When
  the connection is finally killed the holder's next ``is_leader`` probe fails
  and it demotes itself. The lock itself is the fencing token — a split brain
  would require Postgres to keep two backends alive while reporting the first
  one dead.
* **Failover is *at-most-once-started*, not exactly-once-completed**: a sweep
  interrupted mid-run is picked up from scratch by the new leader, so every
  guarded loop must stay idempotent (the retention/reclaim sweeps already are —
  they delete/claim by primary key and re-run cheaply).

Non-Postgres backends (the SQLite test suite, single-process dev) have no
advisory locks, so a leader there is itself: :class:`AdvisoryLeader` degrades to
always-leader rather than making the suite skip the behaviour it exercises.
"""
from __future__ import annotations

import hashlib
import logging
from collections.abc import Callable
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

from app.core.config import get_settings

logger = logging.getLogger(__name__)

# Namespaced lock identity: (class, obj) int4 pair. Postgres keeps one advisory
# lock namespace per (class, obj) so we never collide with locks another
# application takes in the same database (each guarded loop hashes its own name
# into ``obj``).
_SESSION_LEVEL_CLASS = 1


def _lock_key(name: str) -> int:
    """Map a loop name onto a stable signed int4 (Postgres locks are int4/int8).

    ``hashlib`` rather than builtin ``hash()`` because the latter is randomised
    per process (PYTHONHASHSEED) — two replicas would then take *different*
    locks and both believe they lead.
    """
    digest = hashlib.sha256(name.encode("utf-8")).digest()
    # 31 bits keeps the value inside signed int4 with the top bit clear, so the
    # key is unambiguously positive for the driver regardless of endianness.
    return int.from_bytes(digest[:4], "big") & 0x7FFF_FFFF


class AdvisoryLeader:
    """Holds a session-level advisory lock on a dedicated pooled connection.

    Construct one per guarded loop, name it after what it guards (the live
    names are ``"retention"``, ``"recovery"`` and ``"stale-jobs"`` — distinct
    on purpose, since the name is hashed into the lock key and two loops with
    the same name would share one slot). Call :meth:`try_acquire` until it
    returns True, run the loop, then :meth:`release` on shutdown.
    """

    def __init__(
        self,
        name: str,
        *,
        session_factory: async_sessionmaker[Any] | None = None,
        engine: AsyncEngine | None = None,
    ) -> None:
        self.name = name
        self._session_factory = session_factory
        self._engine = engine
        self._conn: Any = None
        self._held = False
        self._keys = (get_settings().LEADER_LOCK_NAMESPACE, _lock_key(name))

    @property
    def is_leader(self) -> bool:
        """True while this instance holds the lock (never re-checked lazily)."""
        return self._held

    def _resolve_engine(self) -> AsyncEngine | None:
        if self._engine is not None:
            return self._engine
        factory = self._session_factory
        if factory is None:
            from app.db import engine as module_engine

            return module_engine
        # ``async_sessionmaker`` exposes its engine; reuse it instead of opening
        # a second pool just for leader bookkeeping.
        return getattr(factory, "bind", None) or getattr(factory, "kw", {}).get("bind")

    async def try_acquire(self) -> bool:
        """Non-blocking acquisition. True = this process leads ``name``.

        Idempotent: an instance that already holds the lock returns True without
        touching the database. On a non-Postgres dialect it always leads.
        """
        if self._held:
            return True
        engine = self._resolve_engine()
        if engine is None:  # pragma: no cover - defensive: no DB wired
            return True
        if engine.dialect.name != "postgresql":
            # SQLite/dev: single process, so mutual exclusion is vacuous.
            self._held = True
            return True
        try:
            conn = await engine.connect()
            row = (
                await conn.execute(
                    text("SELECT pg_try_advisory_lock(CAST(:ns AS int), CAST(:obj AS int))"),
                    {"ns": self._keys[0], "obj": self._keys[1]},
                )
            ).scalar()
        except Exception:
            logger.warning("leader %s: advisory lock probe failed", self.name, exc_info=True)
            return False
        if not row:
            # Not ours: hand the connection straight back so a losing candidate
            # does not idle-park a pool slot every heartbeat.
            await conn.close()
            return False
        self._conn = conn
        self._held = True
        logger.info("leader %s: acquired advisory lock %s", self.name, self._keys)
        return True

    async def release(self) -> None:
        """Unlock then close. Safe to call when not leading; never raises."""
        if not self._held:
            return
        self._held = False
        conn, self._conn = self._conn, None
        if conn is None:
            return
        try:
            await conn.execute(
                text("SELECT pg_advisory_unlock(CAST(:ns AS int), CAST(:obj AS int))"),
                {"ns": self._keys[0], "obj": self._keys[1]},
            )
        except Exception:  # pragma: no cover - best-effort on shutdown
            # An unlock that fails is still released by the close() below, which
            # ends the session that owns the lock.
            logger.warning("leader %s: advisory unlock failed", self.name, exc_info=True)
        finally:
            try:
                await conn.close()
            except Exception:  # pragma: no cover
                pass

    async def renew(self) -> bool:
        """Liveness probe: confirm the held lock is still ours.

        A connection killed server-side (failover, ``pg_terminate_backend``,
        network drop) silently loses its advisory locks, so the guarded loop
        must demote itself on a False return rather than keep sweeping while a
        promoted peer sweeps too.
        """
        if not self._held:
            return False
        if self._conn is None:
            # ``held`` without a connection is only ever the degraded mode of
            # :meth:`try_acquire` (non-Postgres backend): there is no lock here
            # to lose, so reporting False would silence every gated loop after
            # its *first* tick on SQLite/dev — the exact "coverage quietly
            # disappears in local development" failure that the degradation
            # exists to prevent. Postgres always sets ``_conn`` with ``_held``
            # (and ``release`` clears both), so this cannot mask a real loss.
            return True
        try:
            row = (
                await self._conn.execute(
                    text(
                        "SELECT 1 FROM pg_locks "
                        "WHERE locktype = 'advisory' AND pid = pg_backend_pid() "
                        "AND classid = CAST(:ns AS int) AND objid = CAST(:obj AS int)"
                    ),
                    {"ns": self._keys[0], "obj": self._keys[1]},
                )
            ).scalar()
        except Exception:
            logger.warning("leader %s: renew probe failed, demoting", self.name, exc_info=True)
            self._held = False
            self._conn = None
            return False
        if row is None:
            logger.warning("leader %s: lost advisory lock, demoting", self.name)
            self._held = False
            self._conn = None
            return False
        return True


class LeaderGate:
    """Poll-until-leader wrapper for a periodic singleton coroutine.

    Deliberately dumb: the caller owns the loop cadence, this only answers
    "may I run this tick?". Losing the lock mid-life demotes the gate so the
    next tick is skipped until re-acquisition.
    """

    def __init__(
        self,
        name: str,
        *,
        session_factory: async_sessionmaker[Any] | None = None,
        enabled: bool | None = None,
    ) -> None:
        settings = get_settings()
        # LEADER_ELECTION_ENABLED=false turns the gate into a pass-through, which
        # is the documented escape hatch for a topology where the operator
        # guarantees a single process (and for the test suite).
        self._enabled = (
            settings.LEADER_ELECTION_ENABLED if enabled is None else bool(enabled)
        )
        self._leader = AdvisoryLeader(name, session_factory=session_factory)
        self.heartbeat_seconds = max(int(settings.LEADER_HEARTBEAT_SECONDS), 1)

    @property
    def enabled(self) -> bool:
        return self._enabled

    @property
    def is_leader(self) -> bool:
        return (not self._enabled) or self._leader.is_leader

    async def acquire(self) -> bool:
        """True when this tick may run the guarded body (idempotent)."""
        if not self._enabled:
            return True
        if self._leader.is_leader:
            return await self._leader.renew()
        return await self._leader.try_acquire()

    async def step(self, fn: Callable[[], Any]) -> Any:
        """Run ``fn`` only while leading; returns ``None`` when a non-leader.

        ``fn`` may be sync or async; awaiting a non-awaitable is avoided so a
        plain function stays plain.
        """
        if not await self.acquire():
            return None
        result = fn()
        if hasattr(result, "__await__"):
            return await result
        return result

    async def release(self) -> None:
        await self._leader.release()


__all__ = ["AdvisoryLeader", "LeaderGate"]
