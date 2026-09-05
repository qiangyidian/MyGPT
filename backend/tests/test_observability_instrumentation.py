"""Task 11b — broad span/counter instrumentation + connector/tool quota enforcement.

These tests pin the contract that instrumentation is wired at the integration
points (model calls, tools, retrieval, workflow, queue, artifacts) and that the
emission flags (OTEL_ENABLED / PROMETHEUS_ENABLED) actually gate export — not
just package presence. They also pin the connector/tool quota enforcement
points: an over-quota tenant cannot enable a connector or execute a tool.

The instrumentation is observed through injectable **test recorders** (set via
``set_span_recorder`` / ``set_metric_recorder``) so the assertions do not depend
on a real exporter being present. The recorders are independent of the emission
flags — they observe the *call site*, while the flags control the *exporter*.
"""
from __future__ import annotations

import asyncio
import uuid

import pytest

from app.observability import (
    _emit_metrics,
    _emit_traces,
    counter,
    histogram,
    set_metric_recorder,
    set_span_recorder,
    span,
)


# --------------------------------------------------------------------------- #
# Recorder lifecycle: always reset between tests so no test leaks observations
# into the next, and production code never sees a recorder.
# --------------------------------------------------------------------------- #
@pytest.fixture
def span_recorder():
    rec: list = []
    set_span_recorder(rec)
    yield rec
    set_span_recorder(None)


@pytest.fixture
def metric_recorder():
    rec: list = []
    set_metric_recorder(rec)
    yield rec
    set_metric_recorder(None)


# --------------------------------------------------------------------------- #
# (a) A model-call span is opened/closed around a provider dispatch.
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_model_call_span_opened_around_provider_chat(span_recorder):
    """provider.chat() must open a span named ``model.call`` carrying the model
    name (redacted of secrets) and record the latency on close — even when the
    upstream HTTP fails (the span closes in the finally)."""
    from app.providers.openai_compatible import OpenAICompatibleProvider

    provider = OpenAICompatibleProvider(
        base_url="http://127.0.0.1:1",  # refused fast; we only need the span
        api_key="sk-secret-that-must-not-leak",
        model="test-model",
    )
    # The HTTP call fails (nothing listening); the span must still record.
    with pytest.raises(Exception):
        await provider.chat([{"role": "user", "content": "hi"}])

    entries = [r for r in span_recorder if r["name"] == "model.call"]
    assert entries, "expected a model.call span to be opened around provider.chat()"
    entry = entries[-1]
    # The model name is carried as a redacted attribute.
    assert entry["attributes"].get("model") == "test-model"
    # The api key must NEVER appear in span attributes (redaction chokepoint).
    flat = repr(entry["attributes"])
    assert "sk-secret-that-must-not-leak" not in flat
    # Latency was recorded on close.
    assert entry["duration_ms"] is not None and entry["duration_ms"] >= 0


@pytest.mark.asyncio
async def test_model_stream_span_opened(span_recorder):
    """stream_chat() must open a ``model.stream`` span around the SSE dispatch."""
    from app.providers.openai_compatible import OpenAICompatibleProvider

    provider = OpenAICompatibleProvider(
        base_url="http://127.0.0.1:1", api_key="k", model="m"
    )
    with pytest.raises(Exception):
        async for _ in provider.stream_chat([{"role": "user", "content": "hi"}]):
            pass  # connection refused; we only need the span to open+close

    names = [r["name"] for r in span_recorder]
    assert "model.stream" in names


# --------------------------------------------------------------------------- #
# (b) A tool execution records a counter + a latency histogram.
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_tool_execution_records_counter_and_histogram(
    db_session, span_recorder, metric_recorder
):
    """ToolGateway.execute() must emit a ``tool.execute`` span, a ``tool.calls``
    counter, and a ``tool.latency_ms`` histogram for each call."""
    from app.agents.gateway.tool_gateway import ToolGateway
    from app.models import AgentRun, Conversation, Message

    user_id = uuid.UUID("00000000-0000-0000-0000-000000000001")
    conv = Conversation(user_id=user_id, title="obs")
    db_session.add(conv)
    await db_session.flush()
    msg = Message(conversation_id=conv.id, role="assistant", content="", metadata_={})
    db_session.add(msg)
    await db_session.flush()
    run = AgentRun(
        conversation_id=conv.id,
        message_id=msg.id,
        user_id=user_id,
        runtime="native",
        flow_name="obs",
        status="running",
    )
    db_session.add(run)
    await db_session.flush()

    gw = ToolGateway(
        db_session,
        conversation_id=conv.id,
        assistant_message_id=msg.id,
        run_id=run.id,
        user=None,
    )
    exec_ = await gw.execute(
        tool_call_id="c1", tool_name="datetime_now", arguments={}
    )
    assert exec_.ok is True

    # Span.
    assert any(r["name"] == "tool.execute" for r in span_recorder), span_recorder
    tool_span = [r for r in span_recorder if r["name"] == "tool.execute"][-1]
    assert tool_span["attributes"].get("tool") == "datetime_now"
    assert tool_span["attributes"].get("outcome") == "success"
    assert tool_span["duration_ms"] is not None

    # Counter.
    counters = [r for r in metric_recorder if r["kind"] == "counter" and r["name"] == "tool.calls"]
    assert counters, f"expected a tool.calls counter entry, got {metric_recorder}"
    assert counters[-1]["attributes"].get("tool") == "datetime_now"

    # Histogram.
    hists = [r for r in metric_recorder if r["kind"] == "histogram" and r["name"] == "tool.latency_ms"]
    assert hists, f"expected a tool.latency_ms histogram entry, got {metric_recorder}"
    assert isinstance(hists[-1]["value"], (int, float)) and hists[-1]["value"] >= 0


# --------------------------------------------------------------------------- #
# (c) OTEL_ENABLED=false / PROMETHEUS_ENABLED=false suppress emission even when
#     the package is importable.
# --------------------------------------------------------------------------- #
def test_emit_traces_off_when_flag_false_even_with_pkg(monkeypatch):
    """The traces emission gate reads OTEL_ENABLED, not just package presence:
    with the package forced-available but the flag off (the test-env default),
    emission is suppressed and ``span()`` yields the no-op handle."""
    from app.core.config import get_settings

    monkeypatch.setattr("app.observability._OTEL_AVAILABLE", True)
    get_settings.cache_clear()
    try:
        assert _emit_traces() is False
        with span("x", {"model": "m"}) as s:
            assert s.__class__.__name__ == "_NoopSpan"
    finally:
        get_settings.cache_clear()


def test_emit_traces_on_when_flag_true(monkeypatch):
    """With the package available AND OTEL_ENABLED=true, emission turns on and
    ``span()`` yields the real OTel-backed handle."""
    from app.core.config import get_settings

    monkeypatch.setattr("app.observability._OTEL_AVAILABLE", True)
    monkeypatch.setenv("OTEL_ENABLED", "true")
    get_settings.cache_clear()
    try:
        assert _emit_traces() is True
        with span("x", {"model": "m"}) as s:
            assert s.__class__.__name__ == "_OtelSpan"
    finally:
        monkeypatch.delenv("OTEL_ENABLED", raising=False)
        get_settings.cache_clear()


def test_emit_metrics_off_when_flag_false(monkeypatch):
    """The metrics gate reads PROMETHEUS_ENABLED: flag off -> no-op handles."""
    from app.core.config import get_settings

    monkeypatch.setattr("app.observability._PROM_AVAILABLE", True)
    get_settings.cache_clear()
    try:
        assert _emit_metrics() is False
        # Use fresh metric names so the module handle cache isn't a factor.
        assert counter("test_off_metric_c").__class__.__name__ == "_NoopCounter"
        assert histogram("test_off_metric_h").__class__.__name__ == "_NoopHistogram"
    finally:
        get_settings.cache_clear()


def test_counter_label_schema_is_bounded(monkeypatch):
    """Prometheus counter labels must use a FIXED schema (model/tool/outcome/...),
    NOT the full attributes dict (cardinality risk). With prom enabled, the
    registered labelnames are exactly the fixed schema."""
    pytest.importorskip("prometheus_client")
    from app.core.config import get_settings
    from app.observability import _METRIC_LABEL_KEYS

    monkeypatch.setenv("PROMETHEUS_ENABLED", "true")
    get_settings.cache_clear()
    try:
        name = "test_bounded_label_schema_xyz"
        c = counter(name, "bounded schema test")
        assert c.__class__.__name__ == "_PromCounter", c.__class__.__name__
        collector = c._c  # prometheus_client.Counter
        assert set(collector._labelnames) == set(_METRIC_LABEL_KEYS), (
            set(collector._labelnames),
            set(_METRIC_LABEL_KEYS),
        )
    finally:
        monkeypatch.delenv("PROMETHEUS_ENABLED", raising=False)
        get_settings.cache_clear()


# --------------------------------------------------------------------------- #
# (d) check_connector blocks an over-quota tenant from enabling a connector.
# --------------------------------------------------------------------------- #
@pytest.fixture
def enabled_quota_singleton():
    """Inject an enabled QuotaService (max_connectors=0) for one test."""
    from app.quotas import QuotaLimits, QuotaService, set_quota_service

    svc = QuotaService(
        limits=QuotaLimits(
            enabled=True,
            max_concurrent_runs=8,
            max_tokens_per_period=10**9,
            max_cost_usd_per_period=10**6,
            max_storage_bytes=10 * 1024**3,
            max_connectors=0,  # over-quota on the very first enable
            max_tools_per_run=10,
        )
    )
    set_quota_service(svc)
    yield svc
    set_quota_service(None)


@pytest.mark.asyncio
async def test_connector_enable_blocked_by_quota(db_session, enabled_quota_singleton):
    """With quotas enabled and max_connectors=0, enabling a connector is refused
    with an admin-visible quota reason (AppException → 429 quota_exceeded)."""
    from app.connectors.service import ConnectorService
    from app.core.exceptions import AppException

    user_id = uuid.UUID("00000000-0000-0000-0000-0000000000c1")
    svc = ConnectorService(db_session)
    conn = await svc.create(
        user_id=user_id,
        name="c",
        provider="github",
        credentials={"access_token": "t"},
        oauth_scopes=list(__import__("app.connectors.catalog", fromlist=["get_manifest"]).get_manifest("github").required_scopes),
        enabled=False,  # create disabled; we'll try to enable
    )
    await db_session.commit()

    with pytest.raises(AppException) as ei:
        await svc.enable(user_id, conn.id)
    assert ei.value.status_code == 429
    assert ei.value.code == "quota_exceeded"
    # The admin-visible quota dict is attached.
    details = ei.value.extra
    assert "quota" in details or "quota_type" in details
    q = details.get("quota", details)
    assert q["quota_type"] == "connectors"
    assert q["limit"] == 0


@pytest.mark.asyncio
async def test_connector_enable_allowed_when_quota_off(db_session):
    """With quotas OFF (the default), enable never hits the quota gate."""
    from app.connectors.service import ConnectorService

    user_id = uuid.UUID("00000000-0000-0000-0000-0000000000c2")
    svc = ConnectorService(db_session)
    conn = await svc.create(
        user_id=user_id,
        name="c",
        provider="github",
        credentials={"access_token": "t"},
        oauth_scopes=list(__import__("app.connectors.catalog", fromlist=["get_manifest"]).get_manifest("github").required_scopes),
        enabled=False,
    )
    await db_session.commit()
    enabled = await svc.enable(user_id, conn.id)
    assert enabled.enabled is True


# --------------------------------------------------------------------------- #
# (e) check_tool blocks an over-quota tenant's tool call.
# --------------------------------------------------------------------------- #
@pytest.fixture
def tool_quota_singleton():
    """Inject an enabled QuotaService (max_tools_per_run=0) for one test."""
    from app.quotas import QuotaLimits, QuotaService, set_quota_service

    svc = QuotaService(
        limits=QuotaLimits(
            enabled=True,
            max_concurrent_runs=8,
            max_tokens_per_period=10**9,
            max_cost_usd_per_period=10**6,
            max_storage_bytes=10 * 1024**3,
            max_connectors=25,
            max_tools_per_run=0,  # over-quota on the first distinct tool
        )
    )
    set_quota_service(svc)
    yield svc
    set_quota_service(None)


@pytest.mark.asyncio
async def test_tool_call_blocked_by_quota(
    db_session, tool_quota_singleton, metric_recorder
):
    """With quotas enabled and max_tools_per_run=0, a tool call is refused with a
    quota_exceeded tool result (admin-visible reason in the error)."""
    from app.agents.gateway.tool_gateway import ToolGateway
    from app.core.security import hash_password
    from app.models import AgentRun, Conversation, Message, User

    # The quota service keys on user id, so the gateway needs a real user.
    user_id = uuid.UUID("00000000-0000-0000-0000-00000000000e")
    if await db_session.get(User, user_id) is None:
        db_session.add(
            User(
                id=user_id,
                email="quota-tool@example.com",
                username="quota-tool",
                password_hash=hash_password("Aa1234567"),
                role="user",
                is_active=True,
            )
        )
        await db_session.flush()

    conv = Conversation(user_id=user_id, title="quota-tool")
    db_session.add(conv)
    await db_session.flush()
    msg = Message(conversation_id=conv.id, role="assistant", content="", metadata_={})
    db_session.add(msg)
    await db_session.flush()
    run = AgentRun(
        conversation_id=conv.id,
        message_id=msg.id,
        user_id=user_id,
        runtime="native",
        flow_name="quota-tool",
        status="running",
    )
    db_session.add(run)
    await db_session.flush()

    user = await db_session.get(User, user_id)
    gw = ToolGateway(
        db_session,
        conversation_id=conv.id,
        assistant_message_id=msg.id,
        run_id=run.id,
        user=user,
    )
    exec_ = await gw.execute(
        tool_call_id="c1", tool_name="datetime_now", arguments={}
    )
    # The tool call is refused (not executed) with the quota reason.
    assert exec_.ok is False
    assert exec_.status == "quota_exceeded"
    assert "tool" in (exec_.error or "").lower() or "quota" in (exec_.error or "").lower()


@pytest.mark.asyncio
async def test_tool_call_allowed_when_quota_off(db_session, metric_recorder):
    """With quotas OFF (the default), tool execution never hits the quota gate."""
    from app.agents.gateway.tool_gateway import ToolGateway
    from app.models import AgentRun, Conversation, Message

    user_id = uuid.UUID("00000000-0000-0000-0000-000000000001")
    conv = Conversation(user_id=user_id, title="quota-off")
    db_session.add(conv)
    await db_session.flush()
    msg = Message(conversation_id=conv.id, role="assistant", content="", metadata_={})
    db_session.add(msg)
    await db_session.flush()
    run = AgentRun(
        conversation_id=conv.id,
        message_id=msg.id,
        user_id=user_id,
        runtime="native",
        flow_name="quota-off",
        status="running",
    )
    db_session.add(run)
    await db_session.flush()

    gw = ToolGateway(
        db_session,
        conversation_id=conv.id,
        assistant_message_id=msg.id,
        run_id=run.id,
        user=None,
    )
    exec_ = await gw.execute(
        tool_call_id="c1", tool_name="datetime_now", arguments={}
    )
    assert exec_.ok is True
    assert exec_.status == "success"


# --------------------------------------------------------------------------- #
# Integration points: a span/counter is emitted for retrieval / artifact /
# workflow / queue operations. These pin that the call sites EXIST (one span per
# logical operation) without asserting exporter internals.
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_rag_retrieval_span_emitted(db_session, span_recorder):
    """RagService.retrieve() emits a ``rag.retrieve`` span even when the KB is
    missing (best-effort: returns empty, but the span still opens/closes)."""
    from app.rag.rag_service import RagService

    svc = RagService()
    ctx, citations = await svc.retrieve(
        db_session, "question", [uuid.UUID("00000000-0000-0000-0000-0000000000f1")]
    )
    assert ctx == "" and citations == []
    names = [r["name"] for r in span_recorder]
    assert "rag.retrieve" in names, names


@pytest.mark.asyncio
async def test_artifact_create_span_emitted(db_session, span_recorder, tmp_path):
    """ArtifactService.create_from_bytes emits an ``artifact.create`` span +
    counter; open() emits ``artifact.open``."""


    from app.artifacts.service import ArtifactService
    from app.core.security import hash_password
    from app.models.user import User

    # Seed a user.
    user_id = uuid.UUID("00000000-0000-0000-0000-0000000000a1")
    if await db_session.get(User, user_id) is None:
        db_session.add(
            User(
                id=user_id,
                email="art-obs@example.com",
                username="art-obs",
                password_hash=hash_password("Aa1234567"),
                role="user",
                is_active=True,
            )
        )
        await db_session.flush()

    svc = ArtifactService(db_session)
    art = await svc.create_from_bytes(
        owner_id=user_id,
        data=b"hello-artifact",
        media_type="text/plain",
        filename="a.txt",
    )
    names = [r["name"] for r in span_recorder]
    assert "artifact.create" in names, names

    # open emits its own span.
    await svc.open(art.id, user_id)
    names = [r["name"] for r in span_recorder]
    assert "artifact.open" in names, names


def test_workflow_engine_span_emitted(span_recorder):
    """The workflow engine emits ``workflow.step`` spans as steps execute."""
    from app.agents.workflow.engine import WorkflowEngine
    from app.agents.workflow.schemas import (
        Plan,
        Step,
        StepObservation,
        VerificationVerdict,
        VerifierResult,
    )

    class _StubExecutor:
        async def execute(self, step, observations):
            return StepObservation(step_id=step.id, output="ok")

    class _PassVerifier:
        async def verify(self, plan, observations):
            return VerifierResult(
                verdict=VerificationVerdict.pass_, findings=[], revise_step_ids=[], note="",
            )

    plan = Plan(steps=[Step(id="s1", task="do")], max_replans=0)
    eng = WorkflowEngine(executor=_StubExecutor(), verifier=_PassVerifier())
    result = asyncio.run(eng.run(plan))
    assert result.status == "completed"
    names = [r["name"] for r in span_recorder]
    assert "workflow.step" in names, names


@pytest.mark.asyncio
async def test_queue_enqueue_dequeue_latency_observed(span_recorder, metric_recorder):
    """The InMemoryQueue records a ``queue.wait_ms`` histogram (enqueue→dequeue)
    and the worker records ``queue.process`` on ack."""
    from app.agents.workflow.queue import InMemoryQueue

    q = InMemoryQueue()
    rid = uuid.uuid4()
    await q.enqueue(rid)
    got = await q.dequeue("test-owner")
    assert got == rid
    # The wait-time histogram was observed.
    hists = [r for r in metric_recorder if r["kind"] == "histogram" and r["name"] == "queue.wait_ms"]
    assert hists, metric_recorder
