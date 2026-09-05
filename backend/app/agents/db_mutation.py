"""Request-scoped serialization and cancellation-safe DB cleanup."""
from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any


@asynccontextmanager
async def db_mutation_scope(lock: asyncio.Lock | None) -> AsyncIterator[None]:
    """Acquire the request's DB lock at an outer transaction boundary."""
    if lock is None:
        yield
        return
    async with lock:
        yield


async def rollback_safely(db: Any) -> None:
    """Protect rollback from cancellation without masking the original error."""
    rollback_task = asyncio.create_task(db.rollback())
    while not rollback_task.done():
        try:
            await asyncio.shield(rollback_task)
        except asyncio.CancelledError:
            # Keep the transaction lock until cleanup finishes. The caller
            # re-raises its original cancellation once rollback is complete.
            continue
        except BaseException:
            break
    try:
        rollback_task.result()
    except BaseException:
        pass


async def commit_with_rollback(db: Any) -> None:
    """Commit and restore the session after any commit failure or cancellation."""
    try:
        await db.commit()
    except BaseException:
        await rollback_safely(db)
        raise
