"""Small datetime helpers shared across services.

Kept central so DB-dialect quirks (SQLite returns offset-naive datetimes while
Postgres returns offset-aware) are handled in ONE place.
"""
from __future__ import annotations

from datetime import datetime, UTC


def is_expired(expires_at: datetime | None, now: datetime | None = None) -> bool:
    """True when ``expires_at`` is set and in the past.

    Robust across DB dialects: a naive ``expires_at`` (SQLite) is treated as
    UTC, and ``now`` defaults to the current UTC time.
    """
    if expires_at is None:
        return False
    if now is None:
        now = datetime.now(UTC)
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=UTC)
    if now.tzinfo is None:
        now = now.replace(tzinfo=UTC)
    return expires_at <= now


__all__ = ["is_expired"]
