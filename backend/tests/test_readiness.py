"""Task 11 — strict readiness probes + deterministic offline eval suite.

Readiness is strict but non-fatal to boot: ``/ready`` returns 200 only when
every component passes, 503 otherwise, with a structured per-component body
(each component reports ``ok`` + a human-readable ``reason``). The offline eval
suite runs without external credentials; live "golden" tasks are skipped with a
clear message when their credential env var is absent.
"""
from __future__ import annotations

import pytest

from app.core.health import REPO_MIGRATION_HEAD, check_readiness


# --------------------------------------------------------------------------- #
# Structured readiness result.
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_readiness_returns_structured_components():
    result = await check_readiness()
    assert "status" in result
    assert "components" in result
    comps = result["components"]
    # Every required component is present and carries ok + reason.
    required = {
        "db",
        "db_migration",
        "redis",
        "qdrant",
        "storage",
        "runner",
        "chat_model",
    }
    assert required.issubset(comps.keys())
    for name in required:
        info = comps[name]
        assert "ok" in info, f"{name} missing ok"
        assert "reason" in info, f"{name} missing reason"
        assert isinstance(info["ok"], bool)
        assert isinstance(info["reason"], str)


def test_repo_migration_head_constant_matches_alembic_head():
    # The head revision is 0010_artifacts (the artifacts migration).
    assert REPO_MIGRATION_HEAD == "0010_artifacts"


@pytest.mark.asyncio
async def test_readiness_reports_reason_when_component_down():
    # In the test env Redis is unreachable (port 6399), Qdrant is unreachable,
    # and no chat model is seeded. The probe must report each as ok=False with a
    # non-empty reason (not a bare False) so an operator can act on it.
    result = await check_readiness()
    comps = result["components"]
    # At least one component is down in the offline test env; it must carry a
    # clear reason string.
    down = [c for c, v in comps.items() if not v["ok"]]
    assert down, "expected at least one down component in the offline test env"
    for name in down:
        assert comps[name]["reason"], f"{name} is down with no reason"


@pytest.mark.asyncio
async def test_ready_endpoint_returns_503_when_not_ready(client):
    # The strict /ready endpoint returns 503 with the structured body when any
    # required component is down (the case in the offline test env).
    resp = await client.get("/ready")
    assert resp.status_code in (200, 503)
    body = resp.json()
    assert "components" in body
    # In the offline env at least one component fails -> 503.
    if any(not v["ok"] for v in body["components"].values()):
        assert resp.status_code == 503


# --------------------------------------------------------------------------- #
# Offline eval suite: deterministic contracts + live-golden skip.
# --------------------------------------------------------------------------- #
def test_run_evals_offline_passes_deterministically():
    from app.evals.runner import run_evals

    report = run_evals()
    assert "results" in report
    assert "total" in report and "passed" in report and "skipped" in report
    assert report["total"] >= 1
    # Every OFFLINE contract must pass (deterministic, no credentials).
    offline = [r for r in report["results"] if r.get("kind") != "live"]
    assert offline, "expected at least one offline eval contract"
    for r in offline:
        assert r["status"] == "pass", f"offline eval {r['name']!r} did not pass: {r}"


def test_live_golden_skipped_without_credentials(monkeypatch):
    monkeypatch.delenv("EVAL_LIVE_MODEL_API_KEY", raising=False)
    from app.evals.runner import run_evals

    report = run_evals()
    live = [r for r in report["results"] if r.get("kind") == "live"]
    assert live, "expected a live golden eval contract"
    for r in live:
        assert r["status"] == "skip"
        assert r.get("reason"), "skipped live eval must carry a reason"
