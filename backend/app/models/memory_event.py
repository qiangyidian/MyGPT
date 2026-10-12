"""记忆审计事件（spec §9.6）。

存在的理由不是"日志好看"，而是 consolidation 是 **LLM 的判断**：线上出现"它把用户
的偏好改错了"，必须能区分是抽取错了还是合并错了。评估指标（候选接受率、取代率）
也都从这张表出。

``kind='commit_turn'`` 同时是**幂等锚点**：靠 ``dedup_key`` 上的唯一索引保证同一轮只提交
一次，sweeper 也靠它判定"哪一轮还没做"。

**为什么是 ``dedup_key`` 而不是 ``UNIQUE(conversation_id, message_id) WHERE kind='commit_turn'``**：
测试库是内存 SQLite，SQLAlchemy 对非 PG 方言会忽略 ``postgresql_where`` 却仍然建
``unique=True`` 的索引 —— 索引在 SQLite 上退化成 (conversation_id, message_id) 的无条件
唯一，于是"同一轮的多个 noop / superseded 事件"（S3 起必然出现）会在测试里凭空失败、
生产却正常。专用列 + 普通唯一索引在两种方言下语义一致：``kind=commit_turn`` 时写入键，
其余 kind 为 NULL，而 NULL 在两边都不互撞。
"""
from __future__ import annotations

import uuid

from sqlalchemy import ForeignKey, Index, String
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base
from app.models._mixins import TimestampMixin


class MemoryEvent(Base, TimestampMixin):
    __tablename__ = "memory_events"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    # 审计记录不该因注销账号而消失（与 tool_toggles.updated_by 同理由）。
    user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True, index=True,
    )
    conversation_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("conversations.id", ondelete="CASCADE"),
        nullable=True, index=True,
    )
    # 触发这轮记忆处理的 assistant 消息；可空（历史/系统事件）。
    message_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("messages.id", ondelete="SET NULL"), nullable=True
    )
    # extracted | proposed | activated | superseded | noop | rejected | deleted
    # | model_wrote | parse_error | commit_turn
    kind: Mapped[str] = mapped_column(String(24), nullable=False)
    memory_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("user_memories.id", ondelete="SET NULL"), nullable=True
    )
    payload: Mapped[dict] = mapped_column(JSONB, default=dict, nullable=False)
    # 幂等锚点：commit 事件写 "commit_turn:<conversation_id>:<message_id>"，其余 kind 为
    # NULL（NULL 在 PG 与 SQLite 都不互撞）。见文件头对 partial index 的说明。
    dedup_key: Mapped[str | None] = mapped_column(String(96), nullable=True)

    __table_args__ = (Index("uq_memory_events_dedup", "dedup_key", unique=True),)

    @classmethod
    def commit_key(cls, conversation_id: uuid.UUID, message_id: uuid.UUID) -> str:
        """commit_turn 事件的 ``dedup_key``。"""
        return f"commit_turn:{conversation_id}:{message_id}"
