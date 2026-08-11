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
async def test_release_run_is_idempotent_no_phantom_slots(quota_service: QuotaService):
    """A double-release (retry / finally on cancellation) must not underflow the
    concurrent counter and must not grant phantom slots above the cap.

    Regression: the old release_run ignored the ticket and decremented
    unconditionally, so admit->release->release->release(None) drove the counter
    negative, after which a max_concurrent_runs=2 admitted 4+ runs.
    """
    t1 = await quota_service.admit_run(TENANT)
    # Double-release the same ticket, plus a None release — all must be no-ops
    # beyond the first.
    await quota_service.release_run(TENANT, t1)
    await quota_service.release_run(TENANT, t1)  # duplicate -> no-op
    await quota_service.release_run(TENANT, t1.run_id)  # bare id, still dup -> no-op
    await quota_service.release_run(TENANT, None)  # None -> no-op
    used = await quota_service.get_usage(TENANT)
    assert used["concurrent_runs"] == 0  # never negative

    # With the pool empty (concurrent=0) and limit=2, exactly 2 admits succeed.
    a = await quota_service.admit_run(TENANT)
    b = await quota_service.admit_run(TENANT)
    with pytest.raises(QuotaExceeded):
        await quota_service.admit_run(TENANT)  # no phantom slot granted
    await quota_service.release_run(TENANT, a)
    await quota_service.release_run(TENANT, b)
    # Final release of an already-released run must still be safe.
    await quota_service.release_run(TENANT, a)
    assert (await quota_service.get_usage(TENANT))["concurrent_runs"] == 0


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


# --------------------------------------------------------------------------- #
# Wiring: the chat-path quota hooks consult the process singleton. When an
# operator enables quotas, the cost-control hot paths (admit/release around the
# turn; charge on the server-computed usage) become active. Disabled by default
# so the wiring is inert in the rest of the suite.
# --------------------------------------------------------------------------- #
@pytest.fixture
def enabled_quota_singleton():
    """Inject an enabled QuotaService into the process singleton for one test."""
    from app.quotas import QuotaService, set_quota_service

    svc = QuotaService(
        limits=QuotaLimits(
            enabled=True,
            max_concurrent_runs=1,
            max_tokens_per_period=100,
            max_cost_usd_per_period=1.0,
            max_storage_bytes=1024,
            max_connectors=2,
            max_tools_per_run=5,
        )
    )
    set_quota_service(svc)
    yield svc
    set_quota_service(None)  # always reset -> rest of suite sees the no-op


@pytest.mark.asyncio
async def test_wiring_admit_release_normal_turn(enabled_quota_singleton):
    """A normal turn admits at start and releases at end; the slot is freed."""
    svc = enabled_quota_singleton
    tenant = str(TENANT)
    # Simulate the stream() lifecycle: admit -> (turn body) -> release.
    ticket = await svc.admit_run(tenant)
    assert ticket is not None and ticket.run_id != "disabled"
    assert (await svc.get_usage(tenant))["concurrent_runs"] == 1
    await svc.release_run(tenant, ticket)
    assert (await svc.get_usage(tenant))["concurrent_runs"] == 0


@pytest.mark.asyncio
async def test_wiring_concurrent_limit_blocks_turn(enabled_quota_singleton):
    """The 2nd concurrent run for a max_concurrent_runs=1 tenant is blocked."""
    from app.quotas import QuotaExceeded, get_quota_service

    svc = get_quota_service()
    tenant = str(TENANT)
    ticket = await svc.admit_run(tenant)  # fills the single slot
    with pytest.raises(QuotaExceeded) as ei:
        await svc.admit_run(tenant)
    assert ei.value.quota_type == "concurrent_runs"
    assert "concurrent" in ei.value.reason.lower()
    # The admin-visible dict carries limit + used + tenant.
    d = ei.value.to_dict()
    assert d["limit"] == 1 and d["tenant"] == tenant
    await svc.release_run(tenant, ticket)


@pytest.mark.asyncio
async def test_wiring_charge_usage_on_accounting_path(enabled_quota_singleton):
    """charge_usage (called from the post-model accounting seam) raises once the
    tenant's token quota is crossed, and never trusts a client total."""
    from app.quotas import QuotaExceeded, get_quota_service

    svc = get_quota_service()
    tenant = str(TENANT)
    # Charge to exactly the limit (server-recomputed: 50+50 = 100 == limit).
    await svc.charge_usage(
        tenant, prompt_tokens=50, completion_tokens=50, cost_usd=0.1
    )
    # One more token tips it over.
    with pytest.raises(QuotaExceeded):
        await svc.charge_usage(
            tenant, prompt_tokens=1, completion_tokens=0, cost_usd=0.0
        )


async def _create_mock_model(client, headers):
    r = await client.post(
        "/api/models",
        json={
            "name": "Mock for quota",
            "provider": "mock",
            "api_base_url": "http://localhost/v1",
            "model_name": "mock-model",
            "supports_stream": True,
            "supports_tools": False,
            "is_embedding": False,
        },
        headers=headers,
    )
    assert r.status_code == 201, r.text
    return r.json()["id"]


@pytest.mark.asyncio
async def test_chat_stream_surfaces_quota_exceeded(client, enabled_quota_singleton):
    """End-to-end: with quotas enabled and the tenant already at the concurrent
    cap, the chat stream surfaces a ``quota_exceeded`` error event carrying the
    admin-visible reason (proving the stream() wiring consults the service)."""
    import json

    from app.quotas import get_quota_service
    from tests.conftest import auth_headers

    h = auth_headers()
    model_id = await _create_mock_model(client, h)
    # Exhaust the single concurrent slot up front (the user the stream will run
    # as is the seeded user; the SSE-turn user id resolves to the same tenant).
    svc = get_quota_service()
    tenant = "00000000-0000-0000-0000-000000000001"
    held_ticket = await svc.admit_run(tenant)

    frames: list[tuple[str, dict]] = []
    event_name = ""
    try:
        async with client.stream(
            "POST",
            "/api/chat/stream",
            json={"content": "hello", "model_id": model_id},
            headers=h,
        ) as resp:
            assert resp.status_code == 200
            async for line in resp.aiter_lines():
                if line.startswith("event:"):
                    event_name = line.split(":", 1)[1].strip()
                elif line.startswith("data:"):
                    frames.append((event_name, json.loads(line.split(":", 1)[1])))
    finally:
        await svc.release_run(tenant, held_ticket)

    # The turn must surface the quota block as an error event with the code.
    assert frames, "expected at least one SSE frame"
    last = frames[-1]
    assert last[0] == "error"
    assert last[1]["code"] == "quota_exceeded"
    assert "concurrent" in last[1]["message"].lower()
    # The admin-visible quota dict is attached.
    assert last[1]["quota"]["quota_type"] == "concurrent_runs"
