"""WeChat Official Account identity binding (扫码登录).

One row per bound WeChat follower: ``openid`` -> the MyChat account it logs
into. Kept as its own table rather than a ``users.wechat_openid`` column so a
single account can hold several identities over time (and so the uniqueness
rule is enforced by the database rather than by application code).

``openid`` is UNIQUE: the same WeChat follower can only ever resolve to one
account, which is what stops a second account from claiming an already-bound
openid and taking over its logins.
"""
from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, String, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


class WechatIdentity(Base):
    __tablename__ = "wechat_identities"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    # WeChat follower id, scoped to this Official Account. 64 chars is well
    # beyond any observed openid but leaves room for future formats.
    openid: Mapped[str] = mapped_column(
        String(64), unique=True, index=True, nullable=False
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    bound_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
