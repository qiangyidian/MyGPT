from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from pydantic import AliasChoices, Field

from app.schemas.common import ORMModel


class MessageOut(ORMModel):
    id: uuid.UUID
    conversation_id: uuid.UUID
    role: str
    content: str
    # The ORM column attribute is `metadata_` (mapped to the DB column "metadata")
    # because `metadata` is reserved by SQLAlchemy's declarative base. Read from
    # `metadata_` when validating off the ORM, but serialize the JSON key as
    # "metadata" (what the frontend expects).
    metadata: dict[str, Any] = Field(
        default={},
        validation_alias=AliasChoices("metadata_", "metadata"),
    )
    model_name: str | None = None
    created_at: datetime
