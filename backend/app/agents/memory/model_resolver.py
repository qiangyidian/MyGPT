"""记忆侧 LLM 的模型解析（spec §11「计费」段）。

解析顺序：

1. ``MEMORY_MODEL_ID``（显式指定用哪个模型做记忆维护）；
2. 会话自己的 ModelConfig —— **这是现状行为**，所以什么都不配置时本模块是纯 no-op，
   不会偷偷换掉用户正在用的模型；
3. ``None``：调用方降级（抽取回退正则、摘要跳过）。

刻意**不**自动挑"系统级 config"：那会让"配了系统模型"这件事静默改变摘要质量与费用，
而这类改变必须是有意的。要换模型就设 ``MEMORY_MODEL_ID``。
"""
from __future__ import annotations

import logging
import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.models import ModelConfig

logger = logging.getLogger(__name__)

_warned = False


def _reset_warning_for_tests() -> None:
    global _warned
    _warned = False


async def resolve_memory_model(
    db: AsyncSession, *, conversation_cfg_id: uuid.UUID | None
) -> ModelConfig | None:
    """返回记忆侧调用应当使用的模型配置，或 None（调用方降级）。"""
    global _warned

    raw = (get_settings().MEMORY_MODEL_ID or "").strip()
    if raw:
        try:
            cfg = await db.get(ModelConfig, uuid.UUID(raw))
        except ValueError:
            cfg = None
        if cfg is not None:
            return cfg
        logger.warning("MEMORY_MODEL_ID=%r 找不到对应模型，退回会话模型", raw)

    if conversation_cfg_id is not None:
        cfg = await db.get(ModelConfig, conversation_cfg_id)
        if cfg is not None:
            if not raw and not _warned:
                # 只提示一次：这是"未配置专用模型"的常态，不该每轮刷日志。
                _warned = True
                logger.info("未配置 MEMORY_MODEL_ID，记忆维护使用会话模型 %s", cfg.model_name)
            return cfg

    return None
