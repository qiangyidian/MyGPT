"""Multi-axis quotas with admin-visible enforcement reasons.

Quota axes (per tenant, where "tenant" == the user id throughout this codebase):
  * concurrent-run  — how many runs may be in-flight at once (admission gate);
  * token           — tokens charged per accounting period;
  * cost            — USD charged per accounting period;
  * storage         — bytes stored (checked at upload time);
  * connector       — number of enabled MCP connectors;
  * tool            — distinct tools available to a single run.

Two invariants, both load-bearing:

  1. **Never trust client-supplied usage.** :meth:`QuotaService.charge_usage`
     accepts only server-measured ``prompt_tokens`` / ``completion_tokens`` and
     recomputes the total internally. There is no parameter by which a client
     could supply a (spoofable) total; negative values are rejected outright so
     a malicious client cannot roll the counter back.

  2. **Authoritative counters live server-side.** In production a Redis client
     is injected so :meth:`charge_usage`/`admit_run` use atomic ``INCRBY`` /
     ``INCRBYFLOAT`` (multi-worker correct). When no Redis is injected the
     service falls back to an in-process counter store (single-process correct)
     — mirroring :mod:`app.core.rate_limit`. The Message table (Task-2 token
     accounting) remains the durable reconciliation record.

Quotas default to **disabled** in ``ENV=test`` (again like rate_limit) so the
suite is never blocked; the unit tests construct a service with explicit small
limits to exercise the logic.
"""
from __future__ import annotations

import asyncio
import time
from collections import defaultdict
from dataclasses import dataclass
from typing import Any, Protocol

from app.core.config import get_settings


class _RedisLike(Protocol):
    async def incr(self, name: str, amount: int = ...) -> int: ...
    async def decr(self, name: str, amount: int = ...) -> int: ...
    async def incrbyfloat(self, name: str, amount: float) -> float: ...
    async def get(self, name: str) -> Any: ...
    async def sadd(self, name: str, *values: str) -> int: ...
    async def srem(self, name: str, *values: str) -> int: ...
    async def scard(self, name: str) -> int: ...
    async def smembers(self, name: str) -> set: ...
    async def expire(self, name: str, seconds: int) -> int: ...


# --------------------------------------------------------------------------- #
# Limits.
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class QuotaLimits:
    """Per-tenant quota caps. ``enabled=False`` makes the service a no-op."""

    enabled: bool = False
    max_concurrent_runs: int = 8
    max_tokens_per_period: int = 1_000_000
    max_cost_usd_per_period: float = 50.0
    max_storage_bytes: int = 10 * 1024 * 1024 * 1024  # 10 GiB
    max_connectors: int = 25
    max_tools_per_run: int = 40

    @classmethod
    def from_settings(cls, settings: Any | None = None) -> QuotaLimits:
        s = settings or get_settings()
        # Quotas are disabled in test (mirrors rate_limit) so the suite never
        # blocks on a counter. Production deployments opt in via env.
        enabled = bool(getattr(s, "QUOTAS_ENABLED", False)) and s.ENV != "test"
        return cls(
            enabled=enabled,
            max_concurrent_runs=int(getattr(s, "QUOTA_MAX_CONCURRENT_RUNS", 8)),
            max_tokens_per_period=int(getattr(s, "QUOTA_MAX_TOKENS", 1_000_000)),
            max_cost_usd_per_period=float(getattr(s, "QUOTA_MAX_COST_USD", 50.0)),
            max_storage_bytes=int(getattr(s, "QUOTA_MAX_STORAGE_BYTES", 10 * 1024**3)),
            max_connectors=int(getattr(s, "QUOTA_MAX_CONNECTORS", 25)),
            max_tools_per_run=int(getattr(s, "QUOTA_MAX_TOOLS_PER_RUN", 40)),
        )


# --------------------------------------------------------------------------- #
# Enforcement exception.
# --------------------------------------------------------------------------- #
class QuotaExceeded(Exception):
    """Raised when a tenant has exhausted a quota axis.

    Carries an admin-visible ``reason`` plus the structured fields an operator
    needs to diagnose and lift the block (which axis, the limit, the current
    usage, and the tenant identity).
    """

    def __init__(
        self,
        reason: str,
        *,
        limit: float,
        used: float,
        quota_type: str,
        tenant: str,
        **extra: Any,
    ) -> None:
        self.reason = reason
        self.limit = limit
        self.used = used
        self.quota_type = quota_type
        self.tenant = tenant
        self.extra = extra
        super().__init__(reason)

    def to_dict(self) -> dict[str, Any]:
        d = {
            "reason": self.reason,
            "quota_type": self.quota_type,
            "limit": self.limit,
            "used": self.used,
            "tenant": self.tenant,
        }
        d.update(self.extra)
        return d


# --------------------------------------------------------------------------- #
# Run ticket (admission handle). Released back to the pool on run finish.
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class RunTicket:
    tenant: str
    run_id: str


def _ticket_run_id(ticket: RunTicket | str | None) -> str | None:
    """Extract the run_id from a ticket (RunTicket or bare run_id string)."""
    if ticket is None:
        return None
    if isinstance(ticket, RunTicket):
        return ticket.run_id
    return str(ticket)


# --------------------------------------------------------------------------- #
# In-process counter fallback (used when no Redis is injected).
# --------------------------------------------------------------------------- #
class _MemoryCounters:
    def __init__(self) -> None:
        self._ints: dict[str, int] = defaultdict(int)
        self._floats: dict[str, float] = defaultdict(float)
        # Active-run sets: tenant-keyed set of admitted run_ids. Source of truth
        # for the concurrent-run count (len == concurrent). Set semantics make
        # release idempotent (discard never underflows).
        self._sets: dict[str, set[str]] = defaultdict(set)
        self._lock = asyncio.Lock()

    async def incr_int(self, key: str, amount: int = 1) -> int:
        async with self._lock:
            self._ints[key] += amount
            return self._ints[key]

    async def decr_int(self, key: str, amount: int = 1) -> int:
        async with self._lock:
            self._ints[key] -= amount
            return self._ints[key]

    async def incr_float(self, key: str, amount: float) -> float:
        async with self._lock:
            self._floats[key] += amount
            return self._floats[key]

    async def get_int(self, key: str) -> int:
        async with self._lock:
            return self._ints[key]

    async def get_float(self, key: str) -> float:
        async with self._lock:
            return self._floats[key]

    async def set_add(self, key: str, member: str) -> bool:
        async with self._lock:
            if member in self._sets[key]:
                return False
            self._sets[key].add(member)
            return True

    async def set_remove(self, key: str, member: str) -> int:
        async with self._lock:
            s = self._sets.get(key)
            if not s or member not in s:
                return 0
            s.discard(member)
            return 1

    async def set_size(self, key: str) -> int:
        async with self._lock:
            return len(self._sets[key])

    async def set_members(self, key: str) -> list[str]:
        async with self._lock:
            return sorted(self._sets.get(key, set()))


# --------------------------------------------------------------------------- #
# Service.
# --------------------------------------------------------------------------- #
class QuotaService:
    """Admission + post-usage accounting for every quota axis."""

    def __init__(
        self,
        limits: QuotaLimits | None = None,
        *,
        redis: _RedisLike | None = None,
        db_factory: Any | None = None,
    ) -> None:
        self.limits = limits or QuotaLimits.from_settings()
        self._redis = redis
        self._db_factory = db_factory
        self._mem = _MemoryCounters()

    # ---- helpers -----------------------------------------------------------
    @property
    def enabled(self) -> bool:
        return bool(self.limits.enabled)

    def _k(self, axis: str, tenant: str) -> str:
        # Token/cost counters are PERIOD-scoped: the bucket index is part of
        # the key, so a period rolls over automatically without any reset job
        # (the old single eternal key made "per period" mean "per lifetime" —
        # users hit the cap once and stayed blocked until ops cleared Redis).
        if axis in ("tokens", "cost"):
            return f"quota:{axis}:{tenant}:{self._period_bucket()}"
        return f"quota:{axis}:{tenant}"

    @staticmethod
    def _period_seconds() -> int:
        seconds = int(getattr(get_settings(), "QUOTA_PERIOD_SECONDS", 2_592_000) or 0)
        return max(seconds, 60)

    @classmethod
    def _period_bucket(cls) -> int:
        return int(time.time()) // cls._period_seconds()

    async def _incr_int(self, key: str, amount: int = 1) -> int:
        if self._redis is not None:
            try:
                return int(await self._redis.incr(key, amount))
            except Exception:  # noqa: BLE001 — Redis down -> memory fallback
                pass
        return await self._mem.incr_int(key, amount)

    async def _decr_int(self, key: str, amount: int = 1) -> int:
        if self._redis is not None:
            try:
                return int(await self._redis.decr(key, amount))
            except Exception:  # noqa: BLE001
                pass
        return await self._mem.decr_int(key, amount)

    async def _incr_float(self, key: str, amount: float) -> float:
        if self._redis is not None:
            try:
                return float(await self._redis.incrbyfloat(key, amount))
            except Exception:  # noqa: BLE001
                pass
        return await self._mem.incr_float(key, amount)

    async def _expire_period_key(self, key: str) -> None:
        """Best-effort TTL so stale period buckets don't accumulate in Redis."""
        if self._redis is None:
            return
        try:
            await self._redis.expire(key, self._period_seconds() * 2)
        except Exception:  # noqa: BLE001 — housekeeping must never break charging
            pass

    async def _get_int(self, key: str) -> int:
        if self._redis is not None:
            try:
                v = await self._redis.get(key)
                return int(v) if v not in (None, "") else 0
            except Exception:  # noqa: BLE001
                pass
        return await self._mem.get_int(key)

    async def _get_float(self, key: str) -> float:
        if self._redis is not None:
            try:
                v = await self._redis.get(key)
                return float(v) if v not in (None, "") else 0.0
            except Exception:  # noqa: BLE001
                pass
        return await self._mem.get_float(key)

    # ---- active-run set (idempotent admit/release) -------------------------
    async def _set_add(self, key: str, member: str) -> bool:
        """Add ``member`` to set ``key``. Returns True if newly added."""
        if self._redis is not None:
            try:
                return int(await self._redis.sadd(key, member)) == 1
            except Exception:  # noqa: BLE001
                pass
        return await self._mem.set_add(key, member)

    async def _set_remove(self, key: str, member: str) -> int:
        """Remove ``member`` from set ``key``. Returns 1 if removed, 0 if absent.

        This is the idempotency primitive: a duplicate release removes nothing
        and returns 0, so the concurrent count can never underflow.
        """
        if self._redis is not None:
            try:
                return int(await self._redis.srem(key, member))
            except Exception:  # noqa: BLE001
                pass
        return await self._mem.set_remove(key, member)

    async def _set_size(self, key: str) -> int:
        if self._redis is not None:
            try:
                return int(await self._redis.scard(key))
            except Exception:  # noqa: BLE001
                pass
        return await self._mem.set_size(key)

    async def _set_members(self, key: str) -> list[str]:
        if self._redis is not None:
            try:
                return sorted(str(m) for m in await self._redis.smembers(key))
            except Exception:  # noqa: BLE001
                pass
        return await self._mem.set_members(key)

    # ---- admission: concurrent-run + token + cost --------------------------
    async def admit_run(self, tenant: str) -> RunTicket:
        """Reserve a run slot. Raises :class:`QuotaExceeded` if any axis is full.

        Order: token + cost are checked first (idempotent reads), then the
        concurrent slot is reserved by adding a fresh run_id to the tenant's
        active-runs SET. The set cardinality IS the concurrent count, so
        :meth:`release_run` is idempotent (a duplicate release removes a
        non-member and can never underflow the count).
        """
        if not self.enabled:
            return RunTicket(tenant=tenant, run_id="disabled")

        # Token quota.
        used_tok = await self._get_int(self._k("tokens", tenant))
        if used_tok >= self.limits.max_tokens_per_period:
            raise QuotaExceeded(
                f"token quota ({self.limits.max_tokens_per_period}/period) exceeded",
                limit=self.limits.max_tokens_per_period,
                used=used_tok,
                quota_type="tokens",
                tenant=tenant,
            )
        # Cost quota.
        used_cost = await self._get_float(self._k("cost", tenant))
        if used_cost >= self.limits.max_cost_usd_per_period:
            raise QuotaExceeded(
                f"cost quota (${self.limits.max_cost_usd_per_period:g}/period) exceeded",
                limit=self.limits.max_cost_usd_per_period,
                used=used_cost,
                quota_type="cost",
                tenant=tenant,
            )
        # Concurrent-run quota: reserve via the active-runs set. The set is the
        # source of truth — its size is the live concurrent count — so release
        # is an idempotent set remove (never a counter decrement). Members carry
        # a deadline suffix ("<run_id>@<expiry>") and stale members are purged
        # on admission, so a release lost to a Redis outage cannot wedge the
        # tenant at "limit reached" forever.
        runs_key = self._k("runs", tenant)
        import uuid as _uuid

        run_id = _uuid.uuid4().hex
        member = self._run_member(run_id)
        await self._set_add(runs_key, member)
        size = await self._set_size(runs_key)
        if size > self.limits.max_concurrent_runs:
            # Over cap: purge expired members, then re-count before refusing.
            await self._purge_expired_runs(runs_key)
            size = await self._set_size(runs_key)
            if size > self.limits.max_concurrent_runs:
                # Still over cap: roll back the member we just added and refuse.
                await self._set_remove(runs_key, member)
                raise QuotaExceeded(
                    f"concurrent-run limit ({self.limits.max_concurrent_runs}) reached",
                    limit=self.limits.max_concurrent_runs,
                    used=size - 1,
                    quota_type="concurrent_runs",
                    tenant=tenant,
                )
        return RunTicket(tenant=tenant, run_id=run_id)

    @staticmethod
    def _run_member(run_id: str) -> str:
        deadline = int(time.time()) + QuotaService._run_ttl_seconds()
        return f"{run_id}@{deadline}"

    @staticmethod
    def _run_ttl_seconds() -> int:
        return max(int(getattr(get_settings(), "QUOTA_RUN_TTL_SECONDS", 3600) or 0), 60)

    async def _purge_expired_runs(self, runs_key: str) -> None:
        """Remove active-run members whose deadline has passed."""
        now = int(time.time())
        try:
            members = await self._set_members(runs_key)
        except Exception:  # noqa: BLE001
            return
        for member in members:
            _, _, suffix = member.rpartition("@")
            if suffix.isdigit() and int(suffix) < now:
                await self._set_remove(runs_key, member)

    async def release_run(self, tenant: str, ticket: RunTicket | str | None) -> None:
        """Return a run slot to the pool. Idempotent; never raises.

        The ``ticket`` (a :class:`RunTicket` or a run_id string) identifies the
        run to release. Removing a run_id from the active-runs SET is idempotent:
        a second release of the same ticket removes a non-member (Redis ``SREM``
        returns 0) and is a no-op, so the concurrent count can never underflow
        and a retry/finally double-release cannot corrupt the counter.
        """
        if not self.enabled:
            return
        run_id = _ticket_run_id(ticket)
        if not run_id or run_id == "disabled":
            return
        try:
            runs_key = self._k("runs", tenant)
            # Members are stored as "<run_id>@<deadline>"; remove every member
            # whose run_id prefix matches (idempotent — a duplicate release
            # matches nothing, so the count can never underflow).
            for member in await self._set_members(runs_key):
                if member.rpartition("@")[0] == run_id:
                    await self._set_remove(runs_key, member)
        except Exception:  # noqa: BLE001 — release must never crash the caller
            pass

    # ---- post-usage accounting (NEVER trusts client totals) ----------------
    async def charge_usage(
        self,
        tenant: str,
        *,
        prompt_tokens: int,
        completion_tokens: int,
        cost_usd: float,
    ) -> None:
        """Charge server-measured usage to the tenant's counters.

        The token total is RECOMPUTED as ``prompt_tokens + completion_tokens``
        — there is no ``total_tokens`` parameter, so a client can never supply
        a spoofed total. Negative counts are rejected (a client must not be able
        to roll the counter back). When disabled this is a no-op.
        """
        # Validate even when disabled, so the contract is uniform.
        if isinstance(prompt_tokens, bool) or not isinstance(prompt_tokens, int) or prompt_tokens < 0:
            raise ValueError("prompt_tokens must be a non-negative integer")
        if (
            isinstance(completion_tokens, bool)
            or not isinstance(completion_tokens, int)
            or completion_tokens < 0
        ):
            raise ValueError("completion_tokens must be a non-negative integer")
        if isinstance(cost_usd, bool) or not isinstance(cost_usd, (int, float)) or cost_usd < 0:
            raise ValueError("cost_usd must be a non-negative number")

        if not self.enabled:
            return

        # Pre-charge admission: if the tenant is ALREADY at/over a cap, further
        # spend is refused. (The charge that pushed them over already happened
        # on the previous call and is fully counted; this blocks the NEXT one.)
        # This is the cost-control signal the chat-path accounting seam consults.
        used_tok = await self._get_int(self._k("tokens", tenant))
        if used_tok >= self.limits.max_tokens_per_period:
            raise QuotaExceeded(
                f"token quota ({self.limits.max_tokens_per_period}/period) exceeded",
                limit=self.limits.max_tokens_per_period,
                used=used_tok,
                quota_type="tokens",
                tenant=tenant,
            )
        used_cost = await self._get_float(self._k("cost", tenant))
        if used_cost >= self.limits.max_cost_usd_per_period:
            raise QuotaExceeded(
                f"cost quota (${self.limits.max_cost_usd_per_period:g}/period) exceeded",
                limit=self.limits.max_cost_usd_per_period,
                used=used_cost,
                quota_type="cost",
                tenant=tenant,
            )

        total = prompt_tokens + completion_tokens  # server-recomputed; never trusted
        tokens_key = self._k("tokens", tenant)
        cost_key = self._k("cost", tenant)
        await self._incr_int(tokens_key, total)
        await self._incr_float(cost_key, float(cost_usd))
        await self._expire_period_key(tokens_key)
        await self._expire_period_key(cost_key)

    async def get_usage(self, tenant: str) -> dict[str, Any]:
        """Read the authoritative server-side counters for a tenant."""
        return {
            "total_tokens": await self._get_int(self._k("tokens", tenant)),
            "cost_usd": await self._get_float(self._k("cost", tenant)),
            "concurrent_runs": await self._set_size(self._k("runs", tenant)),
            "storage_bytes": await self._get_int(self._k("storage", tenant)),
            "connectors": await self._get_int(self._k("connectors", tenant)),
        }

    # ---- storage -----------------------------------------------------------
    async def record_storage(self, tenant: str, bytes_count: int) -> None:
        if not self.enabled:
            return
        if not isinstance(bytes_count, int) or isinstance(bytes_count, bool) or bytes_count < 0:
            raise ValueError("bytes_count must be a non-negative integer")
        await self._incr_int(self._k("storage", tenant), bytes_count)

    async def check_storage(self, tenant: str, bytes_to_add: int) -> None:
        """Raise if adding ``bytes_to_add`` would exceed the storage cap."""
        if not self.enabled:
            return
        used = await self._get_int(self._k("storage", tenant))
        if used + bytes_to_add > self.limits.max_storage_bytes:
            raise QuotaExceeded(
                f"storage quota ({self.limits.max_storage_bytes} bytes) exceeded",
                limit=self.limits.max_storage_bytes,
                used=used,
                quota_type="storage",
                tenant=tenant,
                requested=bytes_to_add,
            )

    # ---- connectors --------------------------------------------------------
    async def record_connector(self, tenant: str) -> None:
        if not self.enabled:
            return
        await self._incr_int(self._k("connectors", tenant), 1)

    async def check_connector(self, tenant: str) -> None:
        if not self.enabled:
            return
        used = await self._get_int(self._k("connectors", tenant))
        if used >= self.limits.max_connectors:
            raise QuotaExceeded(
                f"connector quota ({self.limits.max_connectors}) reached",
                limit=self.limits.max_connectors,
                used=used,
                quota_type="connectors",
                tenant=tenant,
            )

    # ---- tools (per-run; enforced at the gateway/MCP layer) ----------------
    async def check_tool(
        self, tenant: str, tool_name: str, *, run_id: str | None = None
    ) -> None:
        """Gate one tool call against the per-run distinct-tool cap.

        Called from :meth:`ToolGateway.execute` on every tool invocation (gated
        on ``QUOTAS_ENABLED``). Tracks the distinct set of tools used in a run
        via the same set primitive as the concurrent-run counter; the set's
        cardinality IS the distinct-tool count. The first call for a given
        (tenant, run_id, tool_name) adds the member; a repeat call for the same
        tool is a no-op (a tool already used this run is always allowed again).

        Raises :class:`QuotaExceeded` when adding a NEW distinct tool would
        exceed ``max_tools_per_run``. When ``run_id`` is None the scope falls
        back to a per-tenant global (the historic seam contract).
        """
        if not self.enabled:
            return
        scope = run_id or "global"
        key = self._k("tools", f"{tenant}:{scope}")
        added = await self._set_add(key, tool_name)
        if added:
            size = await self._set_size(key)
            if size > self.limits.max_tools_per_run:
                # Roll back the member we just added and refuse.
                await self._set_remove(key, tool_name)
                raise QuotaExceeded(
                    f"tool quota ({self.limits.max_tools_per_run} distinct/run) reached",
                    limit=self.limits.max_tools_per_run,
                    used=size - 1,
                    quota_type="tools",
                    tenant=tenant,
                    tool=tool_name,
                    run_id=run_id,
                )


__all__ = [
    "QuotaExceeded",
    "QuotaLimits",
    "QuotaService",
    "RunTicket",
    "get_quota_service",
    "set_quota_service",
]


# --------------------------------------------------------------------------- #
# Process-wide singleton + test injection.
# --------------------------------------------------------------------------- #
# Lazily built from settings on first access. In test / default deployments
# QUOTAS_ENABLED is False (and ENV=test forces disabled), so the service is a
# no-op and the chat-path wiring around it never blocks. Production opts in via
# QUOTAS_ENABLED=true; the Redis client is injected best-effort (the in-memory
# fallback handles the no-Redis case, mirroring rate_limit).
_quota_service_singleton: QuotaService | None = None


def get_quota_service() -> QuotaService:
    """Return the process-wide :class:`QuotaService`, building it on first use.

    The app-wide Redis client is injected so counters are atomic across
    workers/replicas. Without injection every process counted into its own
    memory — multi-replica deployments oversold every limit N-fold while
    single-process users hit an eternal cap (no period reset). Injection is
    best-effort: with Redis unreachable the in-memory fallback applies.
    """
    global _quota_service_singleton
    if _quota_service_singleton is None:
        redis_client = None
        try:
            from app.core.redis import get_redis

            redis_client = get_redis()
        except Exception:  # noqa: BLE001 — fall back to in-process counters
            redis_client = None
        _quota_service_singleton = QuotaService(redis=redis_client)
    return _quota_service_singleton


def set_quota_service(svc: QuotaService | None) -> None:
    """Test injection: override (or reset with ``None``) the process singleton.

    The chat-path wiring consults :func:`get_quota_service`, so a test can swap
    in an enabled service to exercise enforcement end-to-end and reset it after.
    """
    global _quota_service_singleton
    _quota_service_singleton = svc
