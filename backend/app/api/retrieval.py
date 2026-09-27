"""Retrieval router: standalone KB search (also used by the KB page's test box).

The chat stream does its own RAG internally; this endpoint exposes retrieval on its
own so the UI can preview what a knowledge base would return for a query.

它同时也是**附件**临时索引（``chat_attachments`` collection，超大文档的分片检索）
唯一的可观测入口：传 ``attachment_id`` 即按同一套 hybrid 索引回查该附件。

诚实的能力边界：语音附件在这里拿不到结果，而且不是「还没做」——音频在解析阶段根本不
产出文本（``attachment_service._extract`` 对音频只写 ``kind=audio`` 的 preview），因此
它从未被分块 / 向量化进任何 collection；转写只发生在发送时（音频输入模型直接收字节，
或 ``/api/speech/transcribe`` 把文本交回前端）。这里显式返回中文可读错误说明它为什么
不在检索范围内，而不是假装搜到了空结果。
"""
from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import get_current_user
from app.core.rate_limit import rate_limit_user
from app.db import get_db
from app.models import ChatAttachment, KnowledgeBase, User
from app.rag import attachment_rag
from app.rag.rag_service import rag_service
from app.schemas import Citation
from app.security.prompt_boundary import apply_untrusted_boundary

router = APIRouter(prefix="/api/retrieval", tags=["retrieval"])

# 预览一次最多取回多少分片：rag_service 的 caller top_k 会直接乘到每库的召回窗口上，
# 不设上限就能让一次点击拉走整库。
_MAX_TOP_K = 20


class SearchRequest(BaseModel):
    # 二选一：知识库检索（``knowledge_base_id``）或单个附件的临时索引检索
    # （``attachment_id``）。两个都不给 = 不知道要搜哪儿，直接拒。
    knowledge_base_id: uuid.UUID | None = None
    attachment_id: uuid.UUID | None = None
    query: str
    top_k: int = Field(default=5, ge=1, le=_MAX_TOP_K)


class SearchResponse(BaseModel):
    context: str
    citations: list[Citation] = []


@router.post("/search", response_model=SearchResponse,
             dependencies=[Depends(rate_limit_user(60, 60, "retrieval"))])
async def search(
    payload: SearchRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> SearchResponse:
    if payload.attachment_id is not None and payload.knowledge_base_id is not None:
        raise HTTPException(400, "knowledge_base_id 与 attachment_id 只能二选一")
    if payload.attachment_id is not None:
        return await _search_attachment(db, user, payload)
    if payload.knowledge_base_id is None:
        raise HTTPException(400, "请指定要检索的知识库（knowledge_base_id）或附件（attachment_id）")
    kb = await db.get(KnowledgeBase, payload.knowledge_base_id)
    if kb is None:
        raise HTTPException(404, "知识库不存在")
    if kb.user_id != user.id and user.role != "admin":
        raise HTTPException(404, "知识库不存在")
    context, citations = await rag_service.retrieve(
        db, payload.query, [kb.id], top_k=payload.top_k
    )
    return SearchResponse(context=context, citations=citations)


async def _search_attachment(
    db: AsyncSession, user: User, payload: SearchRequest
) -> SearchResponse:
    """Per-attachment ephemeral index lookup — the only retrieval view over uploads.

    只读检索，绑定关系一概不改：这条路径不碰 ``att.message_id``，「测试检索」不该
    把附件悄悄绑到某条消息上。
    """
    att = await db.get(ChatAttachment, payload.attachment_id)
    if att is None or (att.user_id != user.id and user.role != "admin"):
        # 不区分「不存在」和「不是你的」，否则这个端点变成别人的附件探测器和 ID oracle。
        raise HTTPException(404, "附件不存在")
    mime = (att.mime_type or "").lower()
    kind = str((att.preview_metadata or {}).get("kind") or "").lower()
    if kind == "audio" or mime.startswith("audio/"):
        raise HTTPException(
            400,
            "语音附件不在检索范围内：音频在解析阶段不产出文本，因此没有可检索的分块与向量；"
            "转写只发生在发送时（音频输入模型直接收音频字节，"
            "或由 /api/speech/transcribe 把识别文本交回输入框）。",
        )
    if att.parse_status != "ready" and not (att.extracted_text or "").strip():
        raise HTTPException(
            400,
            f"附件尚未解析完成（当前状态：{att.parse_status or att.status}），无法检索其文本片段",
        )
    snippets = await attachment_rag.retrieve(
        db, att.id, payload.query, top_k=payload.top_k
    )
    if not snippets:
        # 小文件从未进索引（只走内联路径）：这不是错误，如实返回空，让 UI 自己说明。
        return SearchResponse(context="", citations=[])
    blocks = [apply_untrusted_boundary("attachment", text) for text in snippets]
    citations = [
        Citation(
            document_name=att.original_filename,
            attachment_id=att.id,
            chunk_index=index,
            snippet=text[:300],
            source_type="attachment",
        )
        for index, text in enumerate(snippets)
    ]
    return SearchResponse(context="\n\n".join(b for b in blocks if b), citations=citations)
