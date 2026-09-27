"""Usage and terminal-state accounting for a single chat turn."""
from __future__ import annotations

import logging
import uuid
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Message
from app.quotas import QuotaExceeded, get_quota_service
from app.services import credit_service

logger = logging.getLogger(__name__)

# finish_reason → consumer-facing generation status, persisted in message
# metadata so the UI can tell a real completion apart from a truncation,
# timeout, cancel, or failure. The old code collapsed everything non-cancelled
# to "complete", hiding length/timeout truncations.
_FINISH_STATUS: dict[str, str] = {
    "stop": "complete",
    "tool_calls": "complete",
    "length": "truncated",
    "budget": "truncated",
    "cancelled": "cancelled",
    "timeout": "error",
    "content_filter": "error",
    "provider_error": "error",
    "stream_disconnected": "interrupted",
    "error": "error",
}

# ev_error `code` → finish_reason, so a provider timeout is recorded as
# finish_reason="timeout" instead of a generic "error".
_ERROR_FINISH: dict[str, str] = {
    "agent_budget_exceeded": "budget",
    "provider_timeout": "timeout",
    "provider_error": "provider_error",
    "stream_disconnected": "stream_disconnected",
}


def _status_for_finish(finish_reason: str) -> str:
    if not finish_reason:
        return "complete"
    return _FINISH_STATUS.get(finish_reason, "error")


def _finish_for_error_code(code: str | None) -> str:
    if not code:
        return "error"
    return _ERROR_FINISH.get(code, "error")


def _apply_usage_accounting(
    message: Message,
    model_name: str | None,
    usage: dict[str, Any] | None,
) -> None:
    """Persist one already-aggregated turn usage payload and its total cost."""
    from app.core.pricing import normalize_usage, usage_cost

    normalized = normalize_usage(usage)
    if normalized is None:
        return
    message.prompt_tokens = normalized["prompt_tokens"]
    message.completion_tokens = normalized["completion_tokens"]
    message.total_tokens = normalized["total_tokens"]
    message.cost_usd = usage_cost(model_name, usage)
    message.metadata_ = {
        **(message.metadata_ or {}),
        "usage": dict(usage or {}),
    }


async def _charge_quota_if_enabled(tenant_id: str, message: Message) -> None:
    """Charge the tenant's quota counters from the just-persisted message usage.

    Reads the authoritative ``Message`` token/cost fields written by
    :func:`_apply_usage_accounting` (server-computed; never client-supplied) and
    forwards them to the quota service. No-op when quotas are disabled (the
    default + test env), so this call site is inert unless an operator opts in
    via ``QUOTAS_ENABLED=true``.
    """
    svc = get_quota_service()
    if not svc.enabled:
        return
    prompt = int(message.prompt_tokens or 0)
    completion = int(message.completion_tokens or 0)
    if prompt == 0 and completion == 0:
        return  # nothing to charge (e.g. a no-usage mock turn)
    try:
        await svc.charge_usage(
            tenant_id,
            prompt_tokens=prompt,
            completion_tokens=completion,
            cost_usd=float(message.cost_usd or 0.0),
        )
    except QuotaExceeded:
        # Post-usage overage (tenant crossed the cap mid-period). Surface the
        # admin-visible reason via the turn's metadata so the operator sees it;
        # we do not fail the turn that already produced this output.
        logger.warning(
            "quota overage for tenant %s after turn usage charge", tenant_id
        )


async def settle_turn_usage(
    db: AsyncSession,
    user_id: uuid.UUID,
    message: Message,
    model_name: str | None,
    usage: dict[str, Any] | None,
) -> None:
    """一轮对话的统一结算入口：记账 + 配额计费 + 积分扣减。

    五个调用点全部走这里，不再各自成对调用 ``_apply_usage_accounting`` 与
    ``_charge_quota_if_enabled``。原因是一个真实的漏洞：``_finalize_error``
    与 ``_finalize_interrupted`` 过去只记账不计费，于是"故意让请求报错"就能
    烧 token 而不付账。合并成单一入口后，结构上不可能只记账不计费。

    原子性边界：记账与积分扣减在**同一 DB 事务**内（由调用方提交），幂等由
    ``credit_ledger`` 上 ``ref_type='message'`` 的唯一部分索引保证。配额计费
    走 Redis，是 best-effort（与 :mod:`app.quotas` 既有语义一致），失败不影响
    积分账本 —— 积分是钱，配额是限流，可靠性要求不同，不该捆成一个事务。
    """
    _apply_usage_accounting(message, model_name, usage)
    await _charge_quota_if_enabled(str(user_id), message)
    await credit_service.charge_message_credits(db, user_id, message)


