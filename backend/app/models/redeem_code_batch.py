"""兑换码批次：一次生成的一批码共享面额与有效期。

批次是运营单位（"2026 中秋活动，500 张 10000 分的卡"），也是作废与核销进度
统计的单位。
"""
from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import BigInteger, CheckConstraint, DateTime, ForeignKey, String, Text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base
from app.models._mixins import TimestampMixin


class RedeemCodeBatch(Base, TimestampMixin):
    __tablename__ = "redeem_code_batches"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    credits_per_code: Mapped[int] = mapped_column(BigInteger, nullable=False)
    # NULL = 永久有效。
    expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_by: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    )

    __table_args__ = (
        CheckConstraint("credits_per_code > 0", name="ck_redeem_batch_credits_positive"),
    )
