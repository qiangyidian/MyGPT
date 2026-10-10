"""Chat streaming end-to-end: a Mock-backed turn produces meta -> tokens -> done."""
from __future__ import annotations

import asyncio
import json
import uuid
from unittest.mock import AsyncMock, Mock

import pytest

from app.agents.token_budget import MESSAGE_TOO_LARGE, PromptAdmissionError
from tests.conftest import auth_headers


async def _create_mock_model(client, headers):
    r = await client.post(
        "/api/models",
        json={
            "name": "Mock for stream",
            "provider": "openai-compatible",
            "api_base_url": "http://localhost/v1",
            "model_name": "mock-model",
            "supports_stream": True,
            "supports_tools": True,
            "is_embedding": False,
        },
        headers=headers,
    )
    assert r.status_code == 201, r.text
    return r.json()["id"]


async def test_chat_stream_emits_meta_token_done(client, offline_model):
    h = auth_headers()
    model_id = await _create_mock_model(client, h)

    events: list[str] = []
    async with client.stream(
        "POST",
        "/api/chat/stream",
        json={"content": "hello world", "model_id": model_id},
        headers=h,
    ) as resp:
        assert resp.status_code == 200
        async for line in resp.aiter_lines():
            if line.startswith("event:"):
                events.append(line.split(":", 1)[1].strip())

    assert "meta" in events, f"missing meta event in {events}"
    assert "token" in events, f"missing token event in {events}"
    assert events[-1] in ("done", "error"), f"unexpected last event {events[-1]}"


@pytest.mark.parametrize("mode", ["expert", "deep_research"])
@pytest.mark.parametrize("background_worker", ["inprocess", "durable"])
async def test_casual_research_turn_answers_without_crew_or_retrieval(
    client, db_session, monkeypatch, offline_model, mode, background_worker
):
    from app.agents.intent_service import intent_service
    from app.agents.orchestrator import ChatOrchestrator
    from app.agents.workflow.execution import execute_run
    from app.agents.workflow.queue import InMemoryQueue, set_run_queue
    from app.agents.workflow.worker import RunWorker
    from app.core.config import get_settings
    from app.models import KnowledgeBase, Message
    from app.services.chat_service import rag_service
    from tests.conftest import TestSessionLocal

    settings = get_settings()
    monkeypatch.setattr(settings, "BACKGROUND_WORKER", background_worker)
    monkeypatch.setattr(settings, "RAG_SKIP_CASUAL", True)
    retrieve = AsyncMock(return_value=("", []))
    judge = AsyncMock(return_value=None)
    crew_status = Mock(return_value=(False, "unexpected_crew"))
    monkeypatch.setattr(rag_service, "retrieve", retrieve)
    monkeypatch.setattr(intent_service, "judge", judge)
    monkeypatch.setattr(ChatOrchestrator, "_crewai_status", crew_status)
    kb = KnowledgeBase(
        user_id=uuid.UUID("00000000-0000-0000-0000-000000000001"), name="Casual routing KB"
    )
    db_session.add(kb)
    await db_session.commit()
    headers = auth_headers()
    model_id = await _create_mock_model(client, headers)

    queue = InMemoryQueue()
    worker_task = None
    if background_worker == "durable":
        set_run_queue(queue)

        async def drive_worker():
            async with asyncio.timeout(10):
                while not await queue.pending_ids():
                    await asyncio.sleep(0.01)
                await RunWorker(
                    queue=queue, execute_fn=execute_run, session_factory=TestSessionLocal
                ).run_once()

        worker_task = asyncio.create_task(drive_worker())

    try:
        async with asyncio.timeout(10):
            response = await client.post(
                "/api/chat/stream",
                json={
                    "content": "你好，请介绍一下自己", "mode": mode,
                    "model_id": model_id, "knowledge_base_id": str(kb.id),
                },
                headers=headers,
            )
            if worker_task is not None:
                await worker_task
        assert response.status_code == 200
        assert "event: done" in response.text
        assert "event: error" not in response.text
        meta = json.loads(next(
            line.removeprefix("data: ") for line in response.text.splitlines()
            if line.startswith("data:")
        ))
        async with TestSessionLocal() as session:
            message = await session.get(Message, uuid.UUID(meta["message_id"]))
            assert message.content
            assert message.metadata_["requested_mode"] == mode
            assert message.metadata_["effective_mode"] == "auto"
            assert message.metadata_["effective_runtime"] == "native"
            assert message.metadata_["multi_agent_executed"] is False
        retrieve.assert_not_awaited()
        judge.assert_not_awaited()
        crew_status.assert_not_called()
    finally:
        if worker_task is not None:
            worker_task.cancel()
            await asyncio.gather(worker_task, return_exceptions=True)
            set_run_queue(None)


async def test_chat_sse_preserves_service_admission_error(client, monkeypatch):
    safe_message = "The latest user message exceeds the configured prompt budget"

    class RejectingChatService:
        async def stream(self, **kwargs):
            if False:  # pragma: no cover - makes this an async generator
                yield {}
            raise PromptAdmissionError(MESSAGE_TOO_LARGE, safe_message)

    monkeypatch.setattr(
        "app.api.chat._get_chat_service", lambda: RejectingChatService()
    )

    frames: list[tuple[str, dict]] = []
    event_name = ""
    async with client.stream(
        "POST",
        "/api/chat/stream",
        json={"content": "oversized request"},
        headers=auth_headers(),
    ) as response:
        assert response.status_code == 200
        async for line in response.aiter_lines():
            if line.startswith("event:"):
                event_name = line.split(":", 1)[1].strip()
            elif line.startswith("data:"):
                frames.append((event_name, json.loads(line.split(":", 1)[1])))

    assert frames[-1] == (
        "error",
        {"code": "message_too_large", "message": safe_message},
    )
