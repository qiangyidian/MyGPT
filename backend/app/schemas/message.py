from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from pydantic import AliasChoices, Field

from app.schemas.common import ORMModel


class MessageContentUpdate(ORMModel):
    """改一条消息的正文。

    空正文没有意义（要清空就删掉这条），上限与前端输入框一致，超了直接 422，
    不要悄悄截断——静默丢字是最难查的那种 bug。
    """

    content: str = Field(min_length=1, max_length=200_000)


class MessageTruncateResult(ORMModel):
    """截断的结果：删了几条、会话现在长什么样。

    界面必须有「撤不回来」的自觉——把删掉的条数说出来，用户才知道自己按下了什么。
    """

    conversation_id: uuid.UUID
    deleted: int
    last_message_preview: str | None = None


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
