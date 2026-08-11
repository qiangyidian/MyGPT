"""Durable turn execution end-to-end (Task 5 acceptance).

The durable worker claims a persisted run, calls the REAL ``execute_run`` (not a
stub), and the run executes through the existing orchestrator + native runtime
using a Mock provider (no real model endpoint — mirrors ``test_chat_stream``).

Acceptance:
  * The run reaches terminal ``completed``.
  * There is at least one durable ``RunEvent`` beyond the worker's ``run.started``
    (the orchestrator + runtime emit real execution events).
  * The run's persisted assistant message has non-empty content AND usage
    metadata (token accounting was applied).
"""
from __future__ import annotations

import uuid

import pytest

from app.agents.events import EventStore
from app.agents.workflow.execution import execute_run
from app.agents.workflow.queue import InMemoryQueue
from app.agents.workflow.worker import RunWorker
from app.models import AgentRun, Conversation, Message, ModelConfig
from tests.conftest import TestSessionLocal

_SEEDED_USER = uuid.UUID("00000000-0000-0000-0000-000000000001")


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
async def _seed_durable_run(db_session) -> AgentRun:
    """Create a conversation + user msg + pending assistant msg + AgentRun.

    Mirrors what the chat API would persist BEFORE enqueuing the run: the
    conversation exists, the user message is persisted, and a pending assistant
    message is bound to the run via ``run.message_id``.
    """
    # A Mock-backed model config so no real endpoint is contacted.
    cfg = ModelConfig(
        name="Durable mock",
        provider="mock",
        api_base_url="http://localhost/v1",
        model_name="mock-model",
        supports_stream=True,
        supports_tools=False,
        is_embedding=False,
    )
    db_session.add(cfg)
    await db_session.flush()

    conv = Conversation(
        user_id=_SEEDED_USER,
        title="durable-exec-test",
        model_id=cfg.id,
    )
    db_session.add(conv)
    await db_session.flush()

    user_msg = Message(
        conversation_id=conv.id,
        role="user",
        content="hello durable world",
    )
    db_session.add(user_msg)

    assistant_msg = Message(
        conversation_id=conv.id,
        role="assistant",
        content="",
        model_name=cfg.model_name,
        metadata_={"status": "pending"},
    )
    db_session.add(assistant_msg)
    await db_session.flush()

    run = AgentRun(
        conversation_id=conv.id,
        message_id=assistant_msg.id,
        user_id=_SEEDED_USER,
        runtime="native",
        flow_name="native_chat",
        status="pending",
        input={
            "content": "hello durable world",
            "enable_tools": False,
            "execution_mode": "auto",
            "agent_profile": "general",
            "knowledge_base_id": None,
        },
        model_config_snapshot={
            "provider": cfg.provider,
            "model_name": cfg.model_name,
            "api_base_url": cfg.api_base_url,
            "supports_tools": False,
        },
    )
    db_session.add(run)
    await db_session.commit()
    return run


# --------------------------------------------------------------------------- #
# Acceptance test
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_durable_run_executes_end_to_end(db_session):
    """Enqueue a run, run ONE worker iteration with the real execute_run, and
    assert the run completes with real events + a populated assistant message."""
    # The durable executor reads the persistence factory off the chat_service
    # singleton (same pattern the `client` fixture uses to bind it to the test DB).
    from app.services.chat_service import chat_service

    original_factory = chat_service._persistence_session_factory
    chat_service._persistence_session_factory = TestSessionLocal
    try:
        run = await _seed_durable_run(db_session)

        queue = InMemoryQueue()
        await queue.enqueue(run.id)
        worker = RunWorker(
            queue=queue,
            execute_fn=execute_run,
            session_factory=TestSessionLocal,
        )
        processed = await worker.run_once()
        assert processed == run.id

        async with TestSessionLocal() as s:
            # 1. Run reached terminal completed.
            finished = await s.get(AgentRun, run.id)
            assert finished is not None
            assert finished.status == "completed", (
                f"expected completed, got {finished.status} "
                f"(error: {finished.error_message})"
            )

            # 2. Durable events beyond run.started (the worker's lease-acquire
            #    marker). The orchestrator + runtime must have emitted real ones.
            events = await EventStore(s).replay(run.id)
            kinds = [e.event_type for e in events]
            assert "run.started" in kinds, f"missing run.started in {kinds}"
            assert len(events) > 1, (
                f"expected events beyond run.started, got {kinds}"
            )
            # The native runtime / orchestrator must have produced at least one
            # of these real execution events.
            assert any(
                k in kinds
                for k in ("token", "done", "run_started", "runtime_selected")
            ), f"no execution events in {kinds}"

            # 3. Assistant message has non-empty content + usage metadata.
            msg = await s.get(Message, finished.message_id)
            assert msg is not None
            assert msg.content, f"assistant content empty (metadata={msg.metadata_})"
            assert msg.metadata_, "assistant metadata empty"
            assert (
                msg.metadata_.get("usage")
                or msg.total_tokens
            ), f"no usage metadata (tokens={msg.total_tokens}, meta={msg.metadata_})"
    finally:
        chat_service._persistence_session_factory = original_factory


@pytest.mark.asyncio
async def test_durable_run_unknown_run_id_yields_error(db_session):
    """A non-existent run_id yields an error event (not a crash)."""
    queue = InMemoryQueue()
    phantom = uuid.uuid4()

    # The worker's _process early-returns on unknown runs (ack without exec),
    # so drive execute_run directly to verify its contract.
    async with TestSessionLocal() as session:
        events = []
        async for evt in execute_run(phantom, session):
            events.append(evt)
    assert events
    assert events[-1].kind == "error"
