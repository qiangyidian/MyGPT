"""消息版本历史（条目 31）的读出形状。

只有 Out，没有 Request：版本是覆盖/删除**之前**写下的不可变快照，不接受外部写入或
编辑——能改的历史不是历史。切回某一版走 :class:`MessageVersionActivate`。
"""
from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from pydantic import AliasChoices, Field

from app.schemas.common import ORMModel


class MessageVersionOut(ORMModel):
    id: uuid.UUID
    message_id: uuid.UUID
    conversation_id: uuid.UUID
    role: str
    content: str
    # ORM 上的列属性叫 ``metadata_``（``metadata`` 被 SQLAlchemy 声明基类占了），
    # 但对外仍是 ``metadata`` 键——与 MessageOut 同一套别名写法。
    metadata: dict[str, Any] = Field(
        default={}, validation_alias=AliasChoices("metadata_", "metadata")
    )
    model_name: str | None = None
    total_tokens: int | None = None
    cost_usd: float | None = None
    #: 这一版是怎么被换下来的：edit / regenerate / restore / truncate。
    origin: str
    created_at: datetime


class VersionActivateResult(ORMModel):
    """切版结果。``changed=False`` 表示目标版就是当前版（幂等，不重复存档）。"""

    message_id: uuid.UUID
    activated_version_id: uuid.UUID
    changed: bool
