from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel

from app.schemas.common import ORMModel


class DocumentOut(ORMModel):
    id: uuid.UUID
    knowledge_base_id: uuid.UUID
    filename: str
    file_type: str
    file_size: int
    status: str            # pending|parsing|chunking|embedding|indexed|failed
    error_message: str | None
    chunk_count: int
    created_at: datetime
    updated_at: datetime


class ReindexResult(BaseModel):
    document_id: uuid.UUID
    status: str
    chunk_count: int = 0


class DocumentPreview(BaseModel):
    """Online preview payload: the parsed full text of a document.

    ``render_as`` tells the client how to present the text: markdown source
    (rendered), or plain text (preformatted). ``truncated`` marks that the
    full text exceeded the preview cap and ``content`` was cut; the client
    can then page in the rest via ``offset``.
    """
    document_id: uuid.UUID
    filename: str
    file_type: str
    file_size: int            # original upload size in bytes
    status: str               # document status at preview time
    render_as: str            # "markdown" | "text"
    chars: int                # chars returned in this page
    total_chars: int          # chars in the full parsed text
    truncated: bool = False   # True if the full text was not fully returned
    content: str
