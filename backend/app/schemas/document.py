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
    # Queue state, so a document that is *waiting to be retried* can be told
    # apart from one that is finished failing — with neither, a stuck document
    # looked identical in the UI in both cases.
    ingest_attempts: int = 0
    ingest_next_retry_at: datetime | None = None
    created_at: datetime
    updated_at: datetime


class ReindexResult(BaseModel):
    document_id: uuid.UUID
    status: str
    chunk_count: int = 0
    # The queue's own state, so a caller can tell "queued again" from "still
    # burning retries" without a second request.
    ingest_attempts: int = 0
    ingest_next_retry_at: datetime | None = None


class UploadCapabilities(BaseModel):
    """What the KB upload endpoint will actually accept right now.

    The client used to hard-code its own ``accept=`` list, so it drifted from the
    server: formats the backend had gained stayed greyed out in the file picker,
    and formats it had dropped produced a rejected upload. This is the effective
    (post parser-intersection) answer, so the picker can never offer a rejection.
    """
    allowed_extensions: list[str]
    max_upload_mb: int


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
