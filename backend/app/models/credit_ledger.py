"""积分流水：只追加，永不修改。

账本既是审计轨迹（谁、何时、因为什么、变动多少、变动后余额），也是幂等的
执行机制 —— ``uq_credit_ledger_ref`` 这个唯一部分索引让"同一轮对话扣两次"和
"同一个码兑两次"在数据库层面不可能发生，重试 / 双发 / worker 重复消费都安全。

``ref_type IS NULL`` 的行不受约束（管理员多次调分是合法的重复），所以索引用
的是**部分**唯一索引而不是普通唯一索引。
"""
from __future__ import annotations

import uuid
from datetime import datetime, UTC

from sqlalchemy import BigInteger, DateTime, ForeignKey, Index, String, Text, text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


class CreditLedger(Base):
    __tablename__ = "credit_ledger"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    # 正 = 发放，负 = 消耗。
    delta: Mapped[int] = mapped_column(BigInteger, nullable=False)
    # 写这一行之后的余额快照。用于展示与对账，避免为了显示"当时余额"而
    # 反推整张账本。
    balance_after: Mapped[int] = mapped_column(BigInteger, nullable=False)
    # redeem | admin_adjust | usage | signup_bonus
    reason: Mapped[str] = mapped_column(String(32), nullable=False)
    # redeem_code | message | admin | NULL
    ref_type: Mapped[str | None] = mapped_column(String(32), nullable=True)
    ref_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    # 操作管理员；系统扣费为 NULL。
    actor_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    )
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Python 侧默认（微秒精度），理由同 Message.created_at：server_default
    # 在 Postgres 上取的是事务开始时间，同一事务内多行会同时间戳，分页与
    # 排序都不稳定。
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC), nullable=False
    )

    __table_args__ = (
        Index("ix_credit_ledger_user_created", "user_id", "created_at"),
        Index(
            "uq_credit_ledger_ref",
            "ref_type",
            "ref_id",
            "reason",
            unique=True,
            # 两个方言都要给 where，否则测试库（SQLite）不会真正强制约束，
            # 幂等测试就会变成假绿。
            postgresql_where=text("ref_type IS NOT NULL"),
            sqlite_where=text("ref_type IS NOT NULL"),
        ),
    )
