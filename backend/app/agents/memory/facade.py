"""回合级记忆生命周期：唯一写入入口（spec §4.2）。

只收 id、自开 session（``session_factory`` 可注入，模块级默认值可 patch）：这样
inline 与 durable 两条路径调用的是**同一个函数、同一个事务边界**，行为差异只可能来自
调用时机，不会来自"某条路径忘了写" —— 那正是改造前的缺陷（写入全在 inline 路径里）。
"""
from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.agents.intent_router import memory_mode
from app.agents.memory.model_resolver import resolve_memory_model
from app.agents.memory.service import propose_candidate
from app.core.config import get_settings
from app.db import AsyncSessionLocal
from app.models import Conversation, MemoryEvent, Message, User
# 必须**模块级** import：仓库的 offline_model 夹具是按"名字绑在消费模块上"来 patch 的
# （见 tests/conftest.py 里 offline_model 的 docstring）。写成函数内 import 的话夹具
# patch 不到，测试里摘要会去打真实端点（连接被拒 → 被 try/except 吞掉但拖慢并有噪声）。
from app.providers.registry import get_provider_for_config

logger = logging.getLogger(__name__)

# 模块级、可被测试 patch（同 credit_service._default_session_factory 的模式）。
# 任何"自己开 session"的模块都必须留这个口，否则测试会打到生产引擎上。
_default_session_factory: Any = AsyncSessionLocal


@dataclass
class CommitResult:
    committed: bool
    candidates: int = 0
    summarized: bool = False
    skipped_reason: str | None = None


async def _mark_committed(
    db, *, conversation_id: uuid.UUID, user_id: uuid.UUID, message_id: uuid.UUID
) -> bool:
    """写幂等锚点。返回 False = 这一轮已经提交过。"""
    db.add(
        MemoryEvent(
            user_id=user_id,
            conversation_id=conversation_id,
            message_id=message_id,
            kind="commit_turn",
            payload={},
            # 幂等键必须由应用层生成：唯一索引在 dedup_key 上，不在
            # (conversation_id, message_id) 上（见 models/memory_event.py）。
            dedup_key=MemoryEvent.commit_key(conversation_id, message_id),
        )
    )
    try:
        await db.flush()
    except IntegrityError:
        await db.rollback()
        return False
    return True


async def _latest_user_message(db, conversation_id: uuid.UUID, before: Message) -> Message | None:
    """这一轮的用户消息：assistant 消息之前最近的一条 user 消息。"""
    return (
        await db.execute(
            select(Message)
            .where(
                Message.conversation_id == conversation_id,
                Message.role == "user",
                Message.created_at <= before.created_at,
            )
            .order_by(Message.created_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()


async def commit_turn(
    *,
    conversation_id: uuid.UUID,
    user_id: uuid.UUID,
    assistant_message_id: uuid.UUID,
    mode: str,
    cfg_id: uuid.UUID | None = None,
    session_factory: Any | None = None,
) -> CommitResult:
    """一个回合结束后的记忆写入。自身不抛（调用方仍应 try/except 兜底）。

    顺序是刻意的：**先打幂等锚点，再做实际工作**。锚点先写 = at-most-once（进程在
    抽取途中被杀，这一轮不会重来），代价是极端情况下丢一条候选；反过来则会在 sweeper
    与 detached 任务并发时重复抽取。丢一条候选可接受，重复写不可接受。
    """
    factory = session_factory or _default_session_factory
    gate = memory_mode(mode)

    async with factory() as db:
        message = await db.get(Message, assistant_message_id)
        if message is None:
            return CommitResult(committed=False, skipped_reason="no_message")

        if not await _mark_committed(
            db, conversation_id=conversation_id, user_id=user_id, message_id=assistant_message_id
        ):
            return CommitResult(committed=False, skipped_reason="already_committed")

        if not gate.write:
            await db.commit()
            return CommitResult(committed=True, skipped_reason="mode_disabled")

        conversation = await db.get(Conversation, conversation_id)
        user = await db.get(User, user_id)
        if conversation is None or user is None:
            await db.commit()
            return CommitResult(committed=True, skipped_reason="missing_context")

        user_msg = await _latest_user_message(db, conversation_id, message)

        candidates = 0
        if user_msg is not None and get_settings().MEMORY_AUTO_PROPOSE:
            from app.agents.memory_auto_propose import extract_memory_candidates

            for cand in extract_memory_candidates(user_msg.content or ""):
                try:
                    row = await propose_candidate(
                        db,
                        user_id=user_id,
                        content=cand.content,
                        memory_type=cand.memory_type,
                        confidence=cand.confidence,
                        source_message_id=user_msg.id,
                        source_conversation_id=conversation_id,
                    )
                except Exception:
                    logger.debug("memory candidate rejected", exc_info=True)
                    continue
                if row is not None:
                    candidates += 1

        summarized = await _maybe_summarize(
            db, conversation=conversation, user_id=user_id, cfg_id=cfg_id
        )
        await db.commit()
        return CommitResult(committed=True, candidates=candidates, summarized=summarized)


async def commit_turn_detached(**kwargs: Any) -> None:
    """inline 路径用：调用 ``commit_turn`` 且绝不把异常抛进 asyncio 的无人区。

    与 durable 路径的唯一区别是"谁 await 它"：这里是流结束后的后台任务（沿用仓库既有
    的 ``_refine_title_after_answer`` 模式），durable 是 worker 里直接 await。
    """
    try:
        await commit_turn(**kwargs)
    except Exception:
        logger.warning("detached memory commit failed", exc_info=True)


async def _maybe_summarize(db, *, conversation, user_id, cfg_id) -> bool:
    """历史超预算时把旧消息滚成 summary（由 inline 的 ``_maybe_summarize`` 迁入）。

    失败一律吞掉：摘要不是这一轮的结果，绝不能因此让回合写入整体失败。
    """
    try:
        from app.agents.chat_helpers import load_history, safe_int
        from app.agents.planning import should_summarize, summarize_history
        from app.agents.state_store import save_summary
        from app.services.chat_context import _DEFAULT_MAX_CONTEXT_TOKENS, _estimate_tokens
        # get_provider_for_config 见模块顶部：必须模块级 import 才能被 offline_model patch。

        cfg = await resolve_memory_model(
            db, conversation_cfg_id=cfg_id or conversation.model_id
        )
        if cfg is None:
            return False
        history = await load_history(db, conversation.id)
        messages = [{"role": m.role, "content": m.content} for m in history]
        if not messages:
            return False
        total = sum(
            _estimate_tokens(str(m.get("content") or ""), cfg.model_name) for m in messages
        )
        # 默认值必须与 inline 路径一致：传字面 0 会让 should_summarize 的判定完全变样
        # （要么永不触发，要么每轮触发）。inline 用的是
        # ``_safe_int(cfg.max_context_tokens, _DEFAULT_MAX_CONTEXT_TOKENS)``。
        max_ctx = safe_int(cfg.max_context_tokens, _DEFAULT_MAX_CONTEXT_TOKENS)
        if not should_summarize(total, max_ctx):
            return False
        provider = get_provider_for_config(cfg)
        summary = await summarize_history(provider, messages, keep_recent=6)
        if not summary:
            return False
        await save_summary(db, conversation.id, user_id, summary)
        return True
    except Exception:
        logger.warning("post-turn summary failed", exc_info=True)
        return False
