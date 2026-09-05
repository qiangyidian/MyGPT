"""Best-effort audit logging.

``log()`` writes an :class:`~app.models.AuditEvent` in its OWN session so an
audit failure can never roll back or poison the caller's transaction. It always
swallows errors — auditing must never break the feature it observes.
"""
from __future__ import annotations

import logging
import uuid
from typing import Any

from app.db import AsyncSessionLocal
from app.models import AuditEvent

logger = logging.getLogger(__name__)


async def log(
    *,
    actor_id: uuid.UUID | None,
    action: str,
    target: str | None = None,
    detail: dict[str, Any] | None = None,
) -> None:
    """Append an audit event. Never raises into the caller."""
    try:
        async with AsyncSessionLocal() as session:
            session.add(
                AuditEvent(actor_id=actor_id, action=action, target=target, detail=detail)
            )
            await session.commit()
    except Exception:
        logger.warning("audit log failed for action=%s target=%s", action, target, exc_info=True)
