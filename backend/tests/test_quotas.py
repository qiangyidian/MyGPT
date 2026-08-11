"""Task 11 — multi-axis quotas with admin-visible enforcement reasons.

Quotas NEVER trust client-supplied usage: ``charge_usage`` recomputes the token
total from server-measured prompt/completion counts and the quota state is read
from the authoritative server counter (Redis in prod, in-memory in tests). The
service is constructed directly with small explicit limits so the suite is not
blocked by the production defaults (which, like rate_limit, are disabled in
``ENV=test``).
"""
from __future__ import annotations

import inspect

import pytest

from app.quotas import QuotaExceeded, QuotaLimits, QuotaService

TENANT = "00000000-0000-0000-0000-000000000001"


@pytest.fixture
def limits() -> QuotaLimits:
    return QuotaLimits(
        enabled=True,
        max_concurrent_runs=2,
        max_tokens_per_period=1000,
        max_cost_usd_per_period=1.0,
        max_storage_bytes=1024,  # 1 KiB for easy exhaustion
        max_connectors=3,
        max_tools_per_run=5,
    )


@pytest.fixture
def quota_service(limits: QuotaLimits) -> QuotaService:
    # No Redis injected -> the in-memory counter store (the documented
    # single-process fallback, mirroring rate_limit.py) keeps the test fully
    # deterministic and offline.
    return QuotaService(limits=limits)


# --------------------------------------------------------------------------- #
# Admission: concurrent-run, token, cost quotas each block a new run.
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_concurrent_run_limit_blocks(quota_service: QuotaService):
    t1 = await quota_service.admit_run(TENANT)
    t2 = await quota_service.admit_run(TENANT)
    with pytest.raises(QuotaExceeded) as ei:
        await quota_service.admit_run(TENANT)
    assert "concurrent" in ei.value.reason.lower()
    assert ei.value.quota_type == "concurrent_runs"
    assert ei.value.limit == 2
    # Releasing one ticket frees a slot.
    await quota_service.release_run(TENANT, t1)
    t3 = await quota_service.admit_run(TENANT)
    assert t3 is not None
    await quota_service.release_run(TENANT, t2)
    await quota_service.release_run(TENANT, t3)


@pytest.mark.asyncio
async def test_tenant_token_quota_blocks_new_run(quota_service: QuotaService):
    # Charge to just under the limit, then over.
    await quota_service.charge_usage(
        TENANT, prompt_tokens=400, completion_tokens=400, cost_usd=0.1
    )
    await quota_service.charge_usage(
        TENANT, prompt_tokens=150, completion_tokens=150, cost_usd=0.1
    )  # total 1100 > 1000
    with pytest.raises(QuotaExceeded) as ei:
        await quota_service.admit_run(TENANT)
    assert "token" in ei.value.reason.lower()
    assert ei.value.quota_type == "tokens"
    assert ei.value.limit == 1000


@pytest.mark.asyncio
async def test_cost_quota_blocks_new_run(quota_service: QuotaService):
    await quota_service.charge_usage(
        TENANT, prompt_tokens=10, completion_tokens=10, cost_usd=0.6
    )
    await quota_service.charge_usage(
        TENANT, prompt_tokens=10, completion_tokens=10, cost_usd=0.6
    )  # cost 1.2 > 1.0
    with pytest.raises(QuotaExceeded) as ei:
        await quota_service.admit_run(TENANT)
    assert "cost" in ei.value.reason.lower()
    assert ei.value.quota_type == "cost"


@pytest.mark.asyncio
async def test_storage_quota_blocks_upload(quota_service: QuotaService):
    await quota_service.record_storage(TENANT, 1024)  # exactly full
    with pytest.raises(QuotaExceeded) as ei:
        await quota_service.check_storage(TENANT, 1)
    assert "storage" in ei.value.reason.lower()
    assert ei.value.quota_type == "storage"


@pytest.mark.asyncio
async def test_connector_quota_blocks(quota_service: QuotaService):
    # Record 3 connectors (the limit); the 4th must be refused.
    for _ in range(3):
        await quota_service.record_connector(TENANT)
    with pytest.raises(QuotaExceeded) as ei:
        await quota_service.check_connector(TENANT)
    assert "connector" in ei.value.reason.lower()
    assert ei.value.quota_type == "connectors"


# --------------------------------------------------------------------------- #
# Never trust client-supplied usage: server recomputes the token total.
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_client_supplied_usage_total_is_not_trusted(quota_service: QuotaService):
    # charge_usage accepts only server-measured prompt/completion tokens and
    # recomputes total internally — there is no parameter by which a client
    # could supply a (spoofable) total.
    sig = inspect.signature(quota_service.charge_usage)
    assert "total_tokens" not in sig.parameters
    assert "reported_tokens" not in sig.parameters

    await quota_service.charge_usage(
        TENANT, prompt_tokens=100, completion_tokens=200, cost_usd=0.0
    )
    used = await quota_service.get_usage(TENANT)
    # Recomputed server-side as 100 + 200 = 300 (a spoofed 99999 would have
    # blown the 1000-token quota; instead the honest 300 is what was charged).
    assert used["total_tokens"] == 300


@pytest.mark.asyncio
async def test_charge_usage_negative_tokens_rejected(quota_service: QuotaService):
    # A malicious client cannot roll the counter back with a negative claim.
    with pytest.raises(ValueError):
        await quota_service.charge_usage(
            TENANT, prompt_tokens=-50, completion_tokens=10, cost_usd=0.0
        )


# --------------------------------------------------------------------------- #
# Admin-visible enforcement reasons + disabled-mode no-op.
# --------------------------------------------------------------------------- #
def test_quota_exceeded_carries_admin_visible_reason():
    exc = QuotaExceeded(
        reason="token quota (1000/period) exceeded",
        limit=1000,
        used=1200,
        quota_type="tokens",
        tenant=TENANT,
    )
    assert exc.reason
    assert exc.limit == 1000
    assert exc.used == 1200
    d = exc.to_dict()
    assert d["reason"] and d["quota_type"] == "tokens"
    assert d["limit"] == 1000 and d["used"] == 1200
    assert d["tenant"] == TENANT


@pytest.mark.asyncio
async def test_quotas_disabled_does_not_block():
    svc = QuotaService(limits=QuotaLimits(enabled=False))
    # Admit + charge far past the configured caps; nothing raises because the
    # service is disabled (the default in ENV=test, mirroring rate_limit).
    for _ in range(50):
        await svc.admit_run(TENANT)
    await svc.charge_usage(
        TENANT, prompt_tokens=10**9, completion_tokens=10**9, cost_usd=10**6
    )
    await svc.check_storage(TENANT, 10**12)
