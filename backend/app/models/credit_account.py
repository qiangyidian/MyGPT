"""积分账户：每个用户一行，权威余额。

余额是**派生值** —— 真正的真相来源是 :class:`CreditLedger` 的流水求和。
这里单独存一行是为了让准入检查（每轮对话都要做）是 O(1) 单行读，而不是
每次聚合整张账本。两者是否一致由运维对账 SQL 检查（见 docs/credits-operations.md）。

``balance`` 刻意不加 ``CHECK (balance >= 0)``：准入在轮前、扣费在轮后，
最后一轮必然把余额扣成负数（平台确实花了这笔钱）。加了约束只会让合法的
最后一轮被数据库拒绝，并把真实负债藏起来。
"""
from __future__ import annotations

import uuid

from sqlalchemy import BigInteger, ForeignKey
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base
from app.models._mixins import TimestampMixin


class CreditAccount(Base, TimestampMixin):
    __tablename__ = "credit_accounts"

    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        primary_key=True,
    )
    # 权威余额，可为负（见模块 docstring）。
    balance: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    lifetime_granted: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    lifetime_consumed: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
