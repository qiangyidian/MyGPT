"""Per-request artifact auth context (Task 10 production wiring).

The ContextManager's spill seam is synchronous and lives on a module-level
singleton, but a spilled blob must be persisted as a *tenant-scoped* Artifact
(owner_id = the chat turn's user) attributed to its run. Resolving owner_id at
module construction time is impossible, so the production spill writer reads it
from this contextvar, which the chat turn binds at its boundary.

Carries:
  * ``owner_id`` — tenant scope + authorization for the created Artifact.
  * ``run_id`` — optional attribution (the agent run that produced the spill).
  * ``db_factory`` — zero-arg async-session context manager. Production uses
    ``AsyncSessionLocal``; tests override it with the test session factory so
    spilled artifacts land in the same engine the API under test reads from.

When the context is unset (no active turn — e.g. a unit test of the pure
compaction path), the spill writer falls through to the temp-file default
(best-effort; spill never blocks).
"""
from __future__ import annotations

import contextvars
import uuid
from collections.abc import Callable
from typing import Any

# token type from ContextVar.set; opaque to callers.
_ArtifactCtx = dict | None
_ARTIFACT_SPILL_CTX: contextvars.ContextVar[_ArtifactCtx] = contextvars.ContextVar(
    "artifact_spill_ctx", default=None
)


def set_artifact_spill_context(
    *,
    owner_id: uuid.UUID,
    db_factory: Callable[Any, Any],
    run_id: uuid.UUID | None = None,
) -> Any:
    """Bind the artifact auth context for the current chat turn.

    Returns a token to pass to :func:`reset_artifact_spill_context` in a
    ``finally`` so the context never leaks across turns / tasks.
    """
    return _ARTIFACT_SPILL_CTX.set(
        {"owner_id": owner_id, "run_id": run_id, "db_factory": db_factory}
    )


def reset_artifact_spill_context(token: Any) -> None:
    """Drop the bound context (``set``/``reset`` must balance in ``finally``)."""
    _ARTIFACT_SPILL_CTX.reset(token)


def get_artifact_spill_context() -> _ArtifactCtx:
    """Return the current context dict, or ``None`` if no turn is bound."""
    return _ARTIFACT_SPILL_CTX.get()


__all__ = [
    "get_artifact_spill_context",
    "reset_artifact_spill_context",
    "set_artifact_spill_context",
]
