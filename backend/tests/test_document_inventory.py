"""Document-overview requests use scoped catalog facts, not web research."""
from __future__ import annotations

import uuid

import pytest

from app.agents.intent_router import decide_route, decide_route_with_intent
from app.agents.planning import is_document_inventory_question
from app.agents.schemas import IntentDecision
from app.models import Document, DocumentChunk, KnowledgeBase
from app.rag.rag_service import RagService

QUESTION = "给我进行介绍我的这些文档中都有哪些"
USER_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")


@pytest.mark.parametrize("mode", ["speed", "expert", "deep_research", "auto"])
def test_inventory_stays_native_even_with_research_intent(mode):
    for route in (
        decide_route(mode, has_knowledge_base=True, user_content=QUESTION),
        decide_route_with_intent(mode, has_knowledge_base=True, user_content=QUESTION,
                                 intent=IntentDecision(route="deep_research", confidence=1.0)),
    ):
        assert not route.use_multi_agent
        assert not route.enable_tools
        assert route.disable_web
        assert route.requested_mode == mode


@pytest.mark.parametrize("question", [
    "对比这些文档中有哪些差异", "研究这些文档涉及的技术并联网查找资料", "总结这份文档",
])
def test_content_research_is_not_mistaken_for_a_catalog(question):
    assert not is_document_inventory_question(question)


async def test_inventory_includes_unindexed_documents_and_stays_in_selected_kbs(db_session):
    selected = KnowledgeBase(user_id=USER_ID, name="Selected")
    other = KnowledgeBase(user_id=USER_ID, name="Not selected")
    db_session.add_all([selected, other])
    await db_session.flush()
    documents = [Document(
        knowledge_base_id=kb.id, filename=name, file_path="unused", file_type="txt", status=status,
    ) for kb, name, status in (
        (selected, "indexed.txt", "indexed"), (selected, "pending.txt", "pending"),
        (other, "excluded.txt", "indexed"),
    )]
    db_session.add_all(documents)
    await db_session.flush()
    chunk_id = uuid.uuid4()
    db_session.add(DocumentChunk(id=chunk_id, document_id=documents[0].id,
                                 knowledge_base_id=selected.id, chunk_index=0,
                                 content="A real document introduction " + "x" * 2000, token_count=500))
    await db_session.flush()

    context, citations = await RagService().retrieve(db_session, QUESTION, [selected.id])
    assert "共 2 份文档" in context
    assert "indexed.txt" in context and "pending.txt" in context
    assert "excluded.txt" not in context
    assert "A real document introduction" in context
    assert "x" * 401 not in context
    assert {c.document_id for c in citations} == {doc.id for doc in documents[:2]}
    assert citations[0].chunk_id == chunk_id

    narrowed, narrowed_citations = await RagService().retrieve(
        db_session, QUESTION, [selected.id], document_ids=[documents[1].id]
    )
    assert "共 1 份文档" in narrowed
    assert [c.document_id for c in narrowed_citations] == [documents[1].id]
