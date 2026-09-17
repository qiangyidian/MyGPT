"""单张兑换码。

**只存 SHA-256 哈希，不存明文。** 兑换码是不记名凭证，等价于现金：明文入库
意味着任何一次数据库泄露、备份外泄或日志误打都直接变成白送积分。明文只在
生成那一次响应里返回供管理员导出，此后系统内不再有明文。

代价是有意接受的：CSV 丢失后这批码不可恢复，只能整批作废重发。

``code_prefix`` 存前 6 位明文，仅供管理员在列表中辨认某张卡（"用户说他手上
是 AB12CD 开头的那张"），不足以从哈希反推。
"""
from __future__ import annotations

import uuid
from datetime import datetime, UTC

from sqlalchemy import DateTime, ForeignKey, Index, String
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


class RedeemCode(Base):
    __tablename__ = "redeem_codes"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    batch_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("redeem_code_batches.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    code_hash: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    code_prefix: Mapped[str] = mapped_column(String(8), nullable=False)
    # active | redeemed | void
    status: Mapped[str] = mapped_column(String(16), default="active", nullable=False)
    redeemed_by: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    )
    redeemed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    # Python 侧默认（微秒精度），理由同 CreditLedger.created_at：server_default
    # 在 Postgres 上取的是事务开始时间，redeem_service.create_batch 把一整批码
    # 插进同一事务，会给批里所有码同一个时间戳，list_codes 按 created_at 排序
    # 就变成任意顺序。
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC), nullable=False
    )

    __table_args__ = (Index("ix_redeem_codes_batch_status", "batch_id", "status"),)
