"""Retrieval router: standalone KB search (also used by the KB page's test box).

The chat stream does its own RAG internally; this endpoint exposes retrieval on its
own so the UI can preview what a knowledge base would return for a query.
"""
from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import get_current_user
from app.db import get_db
from app.models import KnowledgeBase, User
from app.rag.rag_service import rag_service
from app.schemas import Citation

router = APIRouter(prefix="/api/retrieval", tags=["retrieval"])


class SearchRequest(BaseModel):
    knowledge_base_id: uuid.UUID
    query: str
    top_k: int = 5


class SearchResponse(BaseModel):
    context: str
    citations: list[Citation] = []


@router.post("/search", response_model=SearchResponse)
async def search(
    payload: SearchRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> SearchResponse:
    kb = await db.get(KnowledgeBase, payload.knowledge_base_id)
    if kb is None:
        raise HTTPException(404, "Knowledge base not found")
    if kb.user_id != user.id and user.role != "admin":
        raise HTTPException(404, "Knowledge base not found")
    context, citations = await rag_service.retrieve(
        db, payload.query, kb.id, top_k=payload.top_k
    )
    return SearchResponse(context=context, citations=citations)
