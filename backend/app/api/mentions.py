"""Mentions router: the type-ahead source behind ``@`` in the composer.

One endpoint answers three questions at once, because the picker must show them
together and rank them side by side:

  * ``kb``   — the caller's knowledge bases;
  * ``doc``  — documents *inside* those knowledge bases (``DocFile`` rows);
  * ``file`` — the current conversation's chat attachments (a separate concept,
    see ``app/api/chat_attachments.py``; they are not KB documents).

Every returned item carries a ``token`` — the stable ``<kind>:<uuid>`` form the
client encodes into the message text and decodes back into the chat request's
``mentions`` field. Nothing here invents ids: the token is derived from the same
primary keys the retrieval path uses, so a mention survives a rename.

Scoping is strict and never leaks existence: knowledge bases are filtered to the
caller's own (admins see every KB, mirroring ``/api/knowledge-bases``), documents
are searched through an ownership join on their KB, and the attachment branch
requires the caller to own the conversation (404 otherwise). Results are capped
per kind and in total — the cap is a real bound, not a hint.
"""
from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import get_current_user
from app.core.like import LIKE_ESCAPE, like_pattern
from app.core.rate_limit import rate_limit_user
from app.db import get_db
from app.models import ChatAttachment, Document, KnowledgeBase, User
from app.schemas.chat import MAX_KB_PER_REQUEST, MAX_MENTIONS_PER_REQUEST
from app.services import attachment_service, conversation_service

router = APIRouter(prefix="/api/mentions", tags=["mentions"])

NOT_FOUND = status.HTTP_404_NOT_FOUND

# The picker is a small popup: a few candidates per category beat one long list,
# and the per-kind bound is what keeps a user with thousands of documents from
# paying for a full scan on every keystroke.
PER_KIND_LIMIT = 8
DEFAULT_LIMIT = 20
MAX_LIMIT = 50

KIND_KB = "kb"
KIND_DOC = "doc"
KIND_FILE = "file"

# Only an indexed document can actually be retrieved; the rest still show up in
# the picker (greyed out) because "my file is missing" is a worse answer than
# "your file is not ready yet".
_STATUS_LABEL = {
    "pending": "排队中",
    "parsing": "解析中",
    "chunking": "切块中",
    "embedding": "向量化中",
    "failed": "解析失败",
}


class MentionTarget(BaseModel):
    """One selectable ``@`` reference.

    ``token`` is what the client inserts (``kind:id``); ``label`` is the human
    part shown in the chip. ``selectable`` is False for a target that exists but
    cannot be retrieved yet.
    """

    kind: str
    id: uuid.UUID
    token: str
    label: str
    sublabel: str = ""
    knowledge_base_id: uuid.UUID | None = None
    selectable: bool = True


class MentionListOut(BaseModel):
    """A capped page of targets, plus the caps the chat request enforces.

    The limits travel with the payload so the client can refuse an over-select
    without hard-coding a second copy of the server's numbers.
    """

    items: list[MentionTarget] = []
    truncated: bool = False
    max_knowledge_bases: int = MAX_KB_PER_REQUEST
    max_mentions: int = MAX_MENTIONS_PER_REQUEST


def _token(kind: str, row_id: uuid.UUID) -> str:
    return f"{kind}:{row_id}"


def _kb_retrieval_hint(kb: KnowledgeBase) -> str:
    """What this KB contributes per turn — its own override, or the default.

    Retrieval is resolved per knowledge base (NULL columns inherit the platform
    default, see ``app/rag/rag_service.py``), so the picker must not imply there
    is one global ``top_k`` for a multi-KB query.
    """
    return f"召回 {kb.top_k} 条" if kb.top_k else "召回沿用默认"


async def _knowledge_bases(
    db: AsyncSession, user: User, pattern: str | None, limit: int
) -> list[KnowledgeBase]:
    """The caller's KBs, newest first, optionally narrowed by the typed query."""
    stmt = select(KnowledgeBase).order_by(
        KnowledgeBase.created_at.desc(), KnowledgeBase.id.desc()
    )
    if user.role != "admin":
        stmt = stmt.where(KnowledgeBase.user_id == user.id)
    if pattern:
        stmt = stmt.where(
            KnowledgeBase.name.ilike(pattern, escape=LIKE_ESCAPE)
            | KnowledgeBase.description.ilike(pattern, escape=LIKE_ESCAPE)
        )
    rows = await db.execute(stmt.limit(max(1, limit)))
    return list(rows.scalars().all())


async def _documents(
    db: AsyncSession, user: User, pattern: str | None, limit: int
) -> list[Document]:
    """Documents of every KB the caller can read, newest first.

    The ownership rule is a JOIN, not the KB page above: a document is reachable
    even when the *KB name* does not match the query the user typed.
    """
    stmt = (
        select(Document)
        .join(KnowledgeBase, Document.knowledge_base_id == KnowledgeBase.id)
        .order_by(Document.created_at.desc(), Document.id.desc())
    )
    if user.role != "admin":
        stmt = stmt.where(KnowledgeBase.user_id == user.id)
    if pattern:
        stmt = stmt.where(Document.filename.ilike(pattern, escape=LIKE_ESCAPE))
    rows = await db.execute(stmt.limit(max(1, limit)))
    return list(rows.scalars().all())


async def _kb_names(db: AsyncSession, kb_ids: list[uuid.UUID]) -> dict[uuid.UUID, str]:
    """Names for the KBs the document hits came from (one small query)."""
    if not kb_ids:
        return {}
    rows = await db.execute(
        select(KnowledgeBase.id, KnowledgeBase.name).where(
            KnowledgeBase.id.in_(list(dict.fromkeys(kb_ids)))
        )
    )
    return {row[0]: row[1] for row in rows.all()}


def _kb_target(kb: KnowledgeBase) -> MentionTarget:
    return MentionTarget(
        kind=KIND_KB,
        id=kb.id,
        token=_token(KIND_KB, kb.id),
        label=kb.name,
        sublabel=f"知识库 · {_kb_retrieval_hint(kb)}",
        knowledge_base_id=kb.id,
    )


def _doc_target(doc: Document, kb_name: str) -> MentionTarget:
    indexed = doc.status == "indexed"
    state = "已索引" if indexed else _STATUS_LABEL.get(doc.status or "", doc.status or "")
    return MentionTarget(
        kind=KIND_DOC,
        id=doc.id,
        token=_token(KIND_DOC, doc.id),
        label=doc.filename,
        sublabel=f"{kb_name} · {state}",
        knowledge_base_id=doc.knowledge_base_id,
        selectable=indexed,
    )


def _file_target(att: ChatAttachment) -> MentionTarget:
    return MentionTarget(
        kind=KIND_FILE,
        id=att.id,
        token=_token(KIND_FILE, att.id),
        label=att.original_filename,
        sublabel="本对话附件",
        # A failed parse still has bytes the model can take (vision/audio parts),
        # so only a deleted row is unselectable.
        selectable=att.status != "deleted",
    )


@router.get(
    "",
    response_model=MentionListOut,
    dependencies=[Depends(rate_limit_user(120, 60, "mentions"))],
)
async def search_mentions(
    q: str = Query(default="", max_length=200),
    conversation_id: uuid.UUID | None = Query(default=None),
    limit: int = Query(default=DEFAULT_LIMIT, ge=1, le=MAX_LIMIT),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> MentionListOut:
    """Type-ahead over the caller's KBs, their documents, and this chat's files.

    An empty ``q`` is a real request: opening the picker with nothing typed shows
    the most recently created target of each kind.
    """
    term = q.strip()
    pattern = like_pattern(term) if term else None
    per_kind = max(1, min(PER_KIND_LIMIT, limit))

    kbs = await _knowledge_bases(db, user, pattern, per_kind)
    docs = await _documents(db, user, pattern, per_kind)
    names = await _kb_names(db, [d.knowledge_base_id for d in docs])

    items: list[MentionTarget] = [_kb_target(kb) for kb in kbs]
    items += [_doc_target(doc, names.get(doc.knowledge_base_id, "知识库")) for doc in docs]

    if conversation_id is not None:
        # Owner-only (an admin's own attachment list would be a different
        # conversation): the branch simply stays empty for a chat with no files.
        if await conversation_service.get(db, conversation_id, user.id) is None:
            raise HTTPException(NOT_FOUND, "Conversation not found")
        rows = await attachment_service.list_for_conversation(
            db, conversation_id, user.id
        )
        files = [a for a in rows if term.lower() in a.original_filename.lower()]
        items += [_file_target(a) for a in files[:per_kind]]

    truncated = len(items) > limit
    return MentionListOut(items=items[:limit], truncated=truncated)
