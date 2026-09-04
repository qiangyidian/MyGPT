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
from sqlalchemy import select
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
            # M5: exactly ONE start event — the dotted Task-4 ``run.started``.
            # The orchestrator's underscore ``run_started`` is suppressed.
            assert "run_started" not in kinds, (
                f"duplicate start event in {kinds}"
            )
            assert len(events) > 1, (
                f"expected events beyond run.started, got {kinds}"
            )
            # The native runtime / orchestrator must have produced at least one
            # of these real execution events.
            assert any(
                k in kinds
                for k in ("token", "done", "runtime_selected")
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


@pytest.mark.asyncio
async def test_durable_create_binds_attachments_and_persists_ids(db_session):
    """create_and_enqueue_durable_run must bind attachments to the user message
    (metadata summaries drive the UI cards; message_id enables cross-turn
    re-hydration) and persist the ids on run.input. Previously the durable path
    — the production BACKGROUND_WORKER=redis configuration — dropped every
    attachment: no binding, no UI card, and the worker-side turn never saw the
    file at all."""
    from app.models import ChatAttachment, User
    from app.schemas import ChatRequest
    from app.services.chat_service import chat_service

    original_factory = chat_service._persistence_session_factory
    chat_service._persistence_session_factory = TestSessionLocal
    try:
        run = await _seed_durable_run(db_session)
        conv_id = run.conversation_id
        user_msg_row = (
            await db_session.execute(
                select(Message).where(
                    Message.conversation_id == conv_id, Message.role == "user"
                )
            )
        ).scalar_one()

        att = ChatAttachment(
            user_id=_SEEDED_USER,
            conversation_id=conv_id,
            filename="notes.txt",
            original_filename="notes.txt",
            mime_type="text/plain",
            size_bytes=64,
            storage_key="/tmp/notes.txt",
            status="ready",
            parse_status="ready",
            extracted_text="QUARTERLY REPORT revenue up 42%",
            is_temporary=True,
        )
        db_session.add(att)
        await db_session.commit()
        await db_session.refresh(att)

        user = await db_session.get(User, _SEEDED_USER)
        assert user is not None
        new_run_id = await chat_service.create_and_enqueue_durable_run(
            db_session,
            user,
            ChatRequest(
                conversation_id=conv_id,
                model_id=None,
                content="总结这个文件",
                attachment_ids=[att.id],
            ),
        )
        assert new_run_id != run.id

        new_run = await db_session.get(AgentRun, new_run_id)
        assert new_run.input.get("attachment_ids") == [str(att.id)]

        sent_msg = (
            await db_session.execute(
                select(Message)
                .where(Message.conversation_id == conv_id, Message.role == "user")
                .order_by(Message.created_at.desc())
            )
        ).scalars().first()
        assert sent_msg.id != user_msg_row.id
        summaries = (sent_msg.metadata_ or {}).get("attachments")
        assert summaries and summaries[0]["id"] == str(att.id), (
            f"no attachment summaries on user message: {sent_msg.metadata_}"
        )
        assert (sent_msg.metadata_ or {}).get("send_params", {}).get(
            "attachment_ids"
        ) == [str(att.id)]

        bound = await db_session.get(ChatAttachment, att.id)
        assert bound.message_id == sent_msg.id, "attachment never bound to message"
    finally:
        chat_service._persistence_session_factory = original_factory


@pytest.mark.asyncio
async def test_durable_turn_injects_attachment_text(db_session, monkeypatch):
    """run_durable_turn re-hydrates the bound attachment text into the provider
    message list AND ctx.user_content — the model must actually SEE the file's
    content on the durable (worker) path, matching the inline path."""
    from app.models import ChatAttachment
    from app.services.chat_service import run_durable_turn

    run = await _seed_durable_run(db_session)
    user_msg = (
        await db_session.execute(
            select(Message).where(
                Message.conversation_id == run.conversation_id,
                Message.role == "user",
            )
        )
    ).scalars().first()

    att = ChatAttachment(
        user_id=_SEEDED_USER,
        conversation_id=run.conversation_id,
        message_id=user_msg.id,
        filename="notes.txt",
        original_filename="notes.txt",
        mime_type="text/plain",
        size_bytes=64,
        storage_key="/tmp/notes.txt",
        status="ready",
        parse_status="ready",
        extracted_text="QUARTERLY REPORT revenue up 42%",
        is_temporary=True,
    )
    db_session.add(att)
    run.input = {**run.input, "attachment_ids": [str(att.id)]}
    await db_session.commit()

    captured: dict = {}

    class _CaptureOrchestrator:
        async def stream(self, ctx):
            captured["messages"] = ctx.messages
            captured["user_content"] = ctx.user_content
            from app.agents.schemas import AgentEvent

            yield AgentEvent(kind="done", data={"finish_reason": "stop"})

    monkeypatch.setattr(
        "app.services.chat_service.chat_orchestrator", _CaptureOrchestrator()
    )

    async with TestSessionLocal() as session:
        async for _evt in run_durable_turn(run.id, session):
            pass

    user_turns = [m for m in captured.get("messages", []) if m.get("role") == "user"]
    assert user_turns, f"no user message in prompt: {captured.get('messages')}"
    assert "[附件内容]" in user_turns[-1]["content"], (
        f"attachment text missing from provider message: {user_turns[-1]['content']}"
    )
    assert "QUARTERLY REPORT" in user_turns[-1]["content"]
    assert "QUARTERLY REPORT" in captured.get("user_content", "")


@pytest.mark.asyncio
async def test_durable_turn_folds_active_user_memories_into_prompt(
    db_session, monkeypatch
):
    """Task 7 (M-2): the durable path assembles its system prompt via the SAME
    ContextManager as the inline path, so the user's active semantic memories
    are present in a durable turn's prompt — not just the inline turn's."""
    from app.models import UserMemory
    from app.services.chat_service import run_durable_turn

    run = await _seed_durable_run(db_session)
    # Seed an ACTIVE user memory for the run's user.
    db_session.add(
        UserMemory(
            user_id=_SEEDED_USER,
            memory_type="preference",
            content="prefers concise answers (durable)",
            active=True,
            confidence=0.9,
        )
    )
    await db_session.commit()

    captured: dict = {}

    class _CaptureOrchestrator:
        async def stream(self, ctx):
            captured["system_prompt"] = ctx.messages[0].get("content", "")
            from app.agents.schemas import AgentEvent

            yield AgentEvent(kind="done", data={"finish_reason": "stop"})

    monkeypatch.setattr(
        "app.services.chat_service.chat_orchestrator", _CaptureOrchestrator()
    )

    async with TestSessionLocal() as session:
        async for _evt in run_durable_turn(run.id, session):
            pass

    assert "prefers concise answers (durable)" in captured.get("system_prompt", ""), (
        "active user memory missing from durable prompt"
    )
