"""Chunk provenance (C21), lexical recall for Chinese (C22) and the context budget (C24).

Three separately-broken things this file keeps fixed:

1. ``RecursiveTextSplitter`` returned bare strings, so a chunk's character span,
   page and heading were throwaway — citations could name a document but never a
   place in it, and ``ParsedDocument.pages`` was parsed and dropped on the floor.
2. The keyword retriever scored chunks with ``content.split()``. Chinese has no
   spaces, so *every* Chinese chunk scored 0.0 and the lexical half of hybrid
   retrieval was silently English-only — on a Chinese-language product.
3. Retrieval was bounded by chunk *count* only, so prompt size grew with the
   longest chunks and the biggest ``top_k`` nobody remembered to tune.
"""
from __future__ import annotations

import hashlib
import uuid

from app.core.config import get_settings
from app.models import Document, DocumentChunk, KnowledgeBase, ModelConfig
from app.rag.base import ParsedDocument, SearchHit
from app.rag.chunk_meta import annotate, chunk_metadata
from app.rag.keyword import (
    KeywordRetriever,
    _score,
    _tokenize,
)
from app.rag.prompts import fit_context, format_context_block

SEEDED_USER = uuid.UUID("00000000-0000-0000-0000-000000000001")


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
async def _seed_chunks(db, contents: list[tuple[str, str]]) -> KnowledgeBase:
    """KB + one document + one chunk per (content, filename) tuple."""
    kb = KnowledgeBase(user_id=SEEDED_USER, name=f"kw-{uuid.uuid4().hex[:6]}")
    db.add(kb)
    await db.flush()
    for content, filename in contents:
        doc = Document(
            knowledge_base_id=kb.id,
            filename=filename,
            file_path=f"/tmp/{filename}",
            file_type=".txt",
            status="indexed",
        )
        db.add(doc)
        await db.flush()
        db.add(
            DocumentChunk(
                document_id=doc.id,
                knowledge_base_id=kb.id,
                chunk_index=0,
                content=content,
                token_count=len(content),
                metadata_={},
            )
        )
    await db.commit()
    return kb


def _hit(text: str, *, name: str = "doc.txt", **payload) -> SearchHit:
    return SearchHit(
        id=str(uuid.uuid4()),
        score=0.5,
        payload={"document_name": name, "text": text, **payload},
    )


# --------------------------------------------------------------------------- #
# 1. Provenance: where a chunk came from inside its document
# --------------------------------------------------------------------------- #
def _md_doc() -> ParsedDocument:
    text = (
        "# 架构总览\n系统采用分层架构。\n\n"
        "# 检索设计\n向量与关键词两路召回，再做 RRF 融合。\n\n"
        "# 部署\n使用 docker compose 起全栈。\n"
    )
    return ParsedDocument(text=text, metadata={"parser_used": "markdown"})


def test_annotate_recovers_span_and_heading():
    parsed = _md_doc()
    chunks = [
        "# 检索设计\n向量与关键词两路召回，再做 RRF 融合。\n",
        "# 部署\n使用 docker compose 起全栈。\n",
    ]
    spans = annotate(chunks, parsed)

    assert [s.heading for s in spans] == ["检索设计", "部署"]
    assert spans[0].char_start is not None and spans[1].char_start > spans[0].char_start
    assert parsed.text[spans[0].char_start : spans[0].char_end] == chunks[0]


def test_annotate_recovers_page_numbers():
    page1 = "第一段内容 alpha\n"
    page2 = "第二段内容 beta\n"
    parsed = ParsedDocument(text=page1 + page2, pages=[page1, page2])
    assert [s.page for s in annotate([page1, page2], parsed)] == [1, 2]


def test_annotate_tolerates_a_chunk_it_cannot_locate():
    """A chunk that is not a slice of the text loses its span, not the pipeline."""
    spans = annotate(["这段文字根本不在原文里 at-all-42"], _md_doc())
    assert spans[0].char_start is None
    assert spans[0].as_metadata() == {}


def test_chunk_metadata_is_sparse_but_carries_parser_provenance():
    parsed = ParsedDocument(
        text="abc", metadata={"parser_used": "pypdf", "pages": 2, "ocr_used": False}
    )
    meta = chunk_metadata(
        annotate(["abc"], parsed)[0], parsed, token_count=7, sha256="deadbeef"
    )
    assert meta["tokens"] == 7
    assert meta["sha256"] == "deadbeef"
    assert meta["parser_used"] == "pypdf"
    assert meta["pages"] == 2
    assert meta["ocr_used"] is False
    assert "heading" not in meta  # no heading in this document -> key absent


# --------------------------------------------------------------------------- #
# 2. Lexical recall
# --------------------------------------------------------------------------- #
def test_tokenize_splits_latin_words_and_cjk_bigrams():
    latin, cjk = _tokenize("知识库检索 KB 5000 AI模型")
    # A mixed CJK/latin run still yields its latin part — `\w` is unicode-aware.
    assert latin == ["kb", "5000", "ai"]
    assert cjk == ["知识", "识库", "库检", "检索", "模型"]


def test_score_counts_cjk_substrings_and_latin_whole_tokens():
    assert _score("the catalog of cats", ["cat"], []) == 0.0
    assert _score("知识库检索方案", [], ["检索"]) > 0.0


async def test_keyword_retriever_finds_chinese_chunks(db_session):
    """The regression this section exists for.

    With whitespace tokenisation the SQL recalled these chunks and the Python
    scorer then threw every one of them away.
    """
    kb = await _seed_chunks(
        db_session,
        [
            ("本知识库支持混合检索能力。", "manual.txt"),
            ("Quarterly revenue grew sharply.", "english.txt"),
        ],
    )
    hits = await KeywordRetriever(db_session).retrieve("知识库检索", kb.id, top_k=5)
    assert [h.payload["document_name"] for h in hits] == ["manual.txt"]
    assert hits[0].payload["retriever"] == "keyword"

    en = await KeywordRetriever(db_session).retrieve("quarterly revenue", kb.id, top_k=5)
    assert [h.payload["document_name"] for h in en] == ["english.txt"]


async def test_candidate_cap_keeps_the_best_covering_chunks(db_session, monkeypatch):
    """An unordered ``limit`` used to evict the right chunk on a CJK query.

    A Chinese query expands into several bigrams, so many chunks match at least
    one; the survivor set is now ordered by how *many* query terms a chunk holds.
    """
    monkeypatch.setattr(get_settings(), "RAG_KEYWORD_CANDIDATES", 1)
    kb = await _seed_chunks(
        db_session,
        [("知识库", f"partial{i}.txt") for i in range(6)]
        + [("知识库检索能力很强，检索也快。", "full.txt")],
    )
    hits = await KeywordRetriever(db_session).retrieve("知识库检索", kb.id, top_k=5)
    assert [h.payload["document_name"] for h in hits] == ["full.txt"]


async def test_keyword_payload_carries_chunk_provenance(db_session):
    from sqlalchemy import select

    kb = await _seed_chunks(db_session, [("知识库检索能力", "one.txt")])
    chunk = (
        await db_session.execute(
            select(DocumentChunk).where(DocumentChunk.knowledge_base_id == kb.id)
        )
    ).scalar_one()
    chunk.metadata_ = {"page": 3, "heading": "检索设计"}
    await db_session.commit()

    hits = await KeywordRetriever(db_session).retrieve("知识库检索", kb.id, top_k=5)
    assert hits[0].payload["page"] == 3
    assert hits[0].payload["heading"] == "检索设计"


# --------------------------------------------------------------------------- #
# 3. Context budget
# --------------------------------------------------------------------------- #
def test_fit_context_packs_whole_chunks_and_drops_the_tail():
    hits = [_hit("一" * 300), _hit("二" * 300), _hit("三" * 300)]
    kept = fit_context(hits, max_tokens=350)
    assert len(kept) < len(hits)
    # Whole-chunk packing: a kept hit is the very same object, unmodified.
    assert kept[0] is hits[0]


def test_fit_context_truncates_only_the_straddling_chunk():
    big = "abcdefghij " * 400
    hits = [_hit("small chunk", name="first.txt"), _hit(big, name="second.txt")]
    kept = fit_context(hits, max_tokens=200)
    assert kept[0] is hits[0]
    assert kept[1].payload["text"].startswith("abcdefghij")
    assert kept[1].payload["text"] != big
    # Replaced, never mutated: the caller's own hit still holds the full text,
    # so a citation built from `kept` shows exactly what the model saw.
    assert hits[1].payload["text"] == big


def test_fit_context_handles_a_first_hit_bigger_than_the_budget():
    kept = fit_context([_hit("x" * 2000)], max_tokens=100)
    assert len(kept) == 1
    assert kept[0].payload["text"]


def test_fit_context_budget_of_zero_is_the_old_unbounded_behaviour():
    hits = [_hit("x" * 400), _hit("y" * 400)]
    assert fit_context(hits, max_tokens=0) == hits


def test_fit_context_reads_the_settings_knob(monkeypatch):
    monkeypatch.setattr(get_settings(), "RAG_CONTEXT_TOKENS", 0)
    hits = [_hit("x" * 400), _hit("y" * 400)]
    assert fit_context(hits) == hits
    monkeypatch.setattr(get_settings(), "RAG_CONTEXT_TOKENS", 40)
    assert len(fit_context(hits)) < 2


def test_context_block_names_the_place_not_just_the_document():
    block = format_context_block(
        [_hit("正文内容", name="手册.pdf", page=7, heading="部署章节")]
    )
    assert "[source 1] 手册.pdf · 章节：部署章节 · 第 7 页" in block


# --------------------------------------------------------------------------- #
# 4. Ingestion writes the provenance it computed
# --------------------------------------------------------------------------- #
async def test_index_document_stores_provenance_and_vector_payload(monkeypatch, tmp_path, db_session):
    from app.rag import qdrant_store
    from app.services import document_service

    path = tmp_path / "手册.md"
    path.write_text(
        "# 检索设计\n向量与关键词两路召回再做融合，命中后统一重排。\n\n"
        "# 部署\n使用 compose 起全栈，队列走 redis stream。\n",
        encoding="utf-8",
    )

    cfg = ModelConfig(
        id=uuid.uuid4(),  # explicit: the column default only fires at INSERT
        name=f"emb-{uuid.uuid4().hex[:8]}",
        provider="mock",
        api_base_url="mock://",
        model_name="mock",
        embedding_model_name="mock-embed-v1",
        is_embedding=True,
    )
    kb = KnowledgeBase(
        user_id=SEEDED_USER,
        name="prov",
        chunk_size=24,
        chunk_overlap=4,
        # Pinned explicitly: with no embedding_model_id the resolver falls back
        # to the oldest embedding config on the platform — which, in a shared
        # test database, belongs to whichever file ran first.
        embedding_model_id=cfg.id,
    )
    db_session.add_all([kb, cfg])
    await db_session.flush()
    doc = Document(
        knowledge_base_id=kb.id,
        filename="手册.md",
        file_path=str(path),
        file_type=".md",
        status="pending",
    )
    db_session.add(doc)
    await db_session.commit()

    points: list = []

    class _Store:
        async def ensure_collection(self, collection, dim):
            return None

        async def delete_by_filter(self, collection, filters=None):
            return None

        async def upsert(self, collection, batch):
            points.extend(batch)

    class _Embedder:
        dim = 4

        async def embed(self, texts):
            return [[0.1, 0.2, 0.3, 0.4] for _ in texts]

    monkeypatch.setattr(document_service, "get_vector_store", lambda: _Store())
    monkeypatch.setattr(qdrant_store, "get_vector_store", lambda: _Store())
    monkeypatch.setattr(document_service, "ProviderEmbedder", lambda *a, **k: _Embedder())
    monkeypatch.setattr(document_service, "get_provider_for_config", lambda cfg: object())

    await document_service.index_document(db_session, doc.id)
    await db_session.commit()

    rows = (
        await db_session.execute(
            DocumentChunk.__table__.select().where(DocumentChunk.document_id == doc.id)
        )
    ).mappings().all()
    assert len(rows) >= 2, "the KB's own chunk_size=24 must beat the platform default"
    for row in rows:
        assert row["embedding_model"] == "mock-embed-v1"
        assert row["embedding_dim"] == 4
        assert row["content_sha256"] == hashlib.sha256(
            row["content"].encode("utf-8")
        ).hexdigest()
    headings = {(row["metadata"] or {}).get("heading") for row in rows}
    assert {"检索设计", "部署"} <= headings

    assert points
    payload = points[0].payload
    assert payload["collection"] == document_service.collection_name(kb.id)
    assert payload["heading"] in {"检索设计", "部署"}
    assert (await db_session.get(Document, doc.id)).status == "indexed"
