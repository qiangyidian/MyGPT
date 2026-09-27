"""消息版本历史（条目 31）：被覆盖/被删掉的回答必须还能拿回来。

原先两件事会静默销毁内容：编辑 user 消息原地覆盖 ``content``，以及 regenerate
直接 ``delete`` 上一条 assistant 回复。用户切不回上一版答案，也没法对比两次生成
的好坏——而「同一个问题换个模型再答一次」正是这个产品最该有的对比能力。

设计约束：

* **append-only 快照**，不做成「可编辑的版本」：版本的意义是忠实记录当时那一版
  内容，任何修改都是新增一条。
* 快照连带 ``metadata``（含 citations/steps）一起存。只存正文的话，切回旧版会把
  引用来源留在新版上，那是引用完整性问题，不是省事的理由。
* 每条消息只保留最近 ``MAX_VERSIONS_PER_MESSAGE`` 版（写入时裁剪），否则一段长
  对话能无限堆正文，存储成本全压在租户身上。
* ``created_at`` 用逐行 Python 时间戳，理由与 ``Message`` 上那段注释相同：一次
  事务里插多行时 ``server_default=func.now()`` 拿到的是事务起始时间，排序不稳定。
"""
from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy import BigInteger, DateTime, Float, ForeignKey, String, Text
from sqlalchemy import Index as _sa_Index
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base

__all__ = ["MessageVersion", "VERSION_ORIGINS"]

#: 一条消息最多留几版。超过即裁掉最旧的（裁剪发生在写入路径里）。
MAX_VERSIONS_PER_MESSAGE = 20
#: 一段会话的总版本上限。message 维度的裁剪管不到「消息行已经没了」的孤儿版本，
#: 而 regenerate 恰恰每次都留下一批——所以总量必须在会话维度上封顶。
MAX_VERSIONS_PER_CONVERSATION = 200
#: origin 取值：edit 用户改问法 / regenerate 重新生成顶掉旧回答 /
#: restore 切版本时把当前版存档 / truncate 截断后续时连带存档。
VERSION_ORIGINS = ("edit", "regenerate", "restore", "truncate")


class MessageVersion(Base):
    __tablename__ = "message_versions"
    __table_args__ = (
        _sa_Index("ix_message_versions_message_created", "message_id", "created_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    # 故意不建外键：regenerate 会真的删掉旧的那一行，而版本的全部意义就是
    # 「那一行没了之后，它的内容还在」。建了 ON DELETE CASCADE 的 FK，就等于在
    # 存档的同一刻把档删了。回收走 conversation_id 那条级联。
    message_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False, index=True)
    # 冗余一份会话 id：删除会话时级联回收，且「这条会话还剩多少历史版本」这类
    # 清理/统计查询不必再回 join messages。
    conversation_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("conversations.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    role: Mapped[str] = mapped_column(String(32), nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False, default="")
    metadata_: Mapped[dict] = mapped_column("metadata", JSONB, default=dict, nullable=False)
    model_name: Mapped[str | None] = mapped_column(String(128), nullable=True)
    total_tokens: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    cost_usd: Mapped[float | None] = mapped_column(Float, nullable=True)
    #: edit | regenerate | restore | truncate —— 用户界面上要说清「这一版是怎么没的」。
    origin: Mapped[str] = mapped_column(String(16), nullable=False, default="regenerate")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        nullable=False,
    )
