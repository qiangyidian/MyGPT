"""per-user 记忆分层开关（spec §5.6）。缺行 = 全默认开启。

为什么不是 users 表的列：这是记忆子系统的私有配置，改动只影响它自己；而且
``episodes_enabled=false`` 的语义是"不再索引我的历史原文"，是一条隐私承诺，值得有
自己的表与自己的 updated_at。
"""
from __future__ import annotations

import uuid

from sqlalchemy import Boolean, ForeignKey
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base
from app.models._mixins import TimestampMixin


class MemorySettings(Base, TimestampMixin):
    __tablename__ = "memory_settings"

    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), primary_key=True
    )
    facts_enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    working_enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    episodes_enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
