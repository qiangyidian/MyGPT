"""Per-call tenant (user) context for tool execution.

**Why this exists.** Every tool is reached through :class:`ToolGateway` (both
runtimes) or through ``/api/tools/test`` — and both of those callers already know
*who* is asking. But the tenant was never handed to ``tool.run()``, so a tool that
takes a resource id could only trust the id: ``file_analyze(document_id=<别人>)``
read another user's document in full. Argument-level ids are attacker-controllable
(the model, or a user typing into the ad-hoc test endpoint), so the owner check has
to come from the *caller's* identity, not from the arguments.

**Mechanism — one contextvar, no new global state.** A :class:`contextvars.ContextVar`
holding an immutable :class:`ToolContext`, bound by the caller immediately around
``tool.run()`` and reset in ``finally``. This is the same pattern the codebase
already uses for tenant-scoped artifact spills
(:mod:`app.artifacts.context`), and it fits the two hard constraints here:

  * It is per-asyncio-task, so concurrent chats of different users never see each
    other's context (a module-level "current user" singleton would leak).
  * Bind/reset are plain synchronous dict writes on the context — **no lock and no
    await inside the critical region**. ``BudgetGuard`` guards its counters with a
    ``threading.RLock`` and its checks are called from ``tool.run`` call sites, so
    anything we add around execution must stay await-free; holding an RLock across
    an await would let another worker thread block on a lock whose owner is
    suspended, i.e. a deadlock. That is why this is a contextvar and not a
    lock-protected registry.

**Fail closed.** :func:`current_tool_context` returns ``None`` when nothing is
bound. A tool declaring ``requires_user = True`` must refuse in that case (the
gateway enforces this before execution too, step 2b) — "no context" is never
treated as "trusted internal caller".
"""
from __future__ import annotations

import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar, Token
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # pragma: no cover
    from app.models.user import User

__all__ = [
    "ToolContext",
    "bind_tool_context",
    "current_tool_context",
    "forbidden_result",
    "make_tool_context",
    "reset_tool_context",
    "use_tool_context",
]


@dataclass(frozen=True)
class ToolContext:
    """Who is on the hook for this tool call. Immutable on purpose.

    ``user`` is the authenticated principal (may be a lightweight object with
    ``id``/``role`` — tests pass ``SimpleNamespace``); ``user_id``/``is_admin``
    are derived so tools never re-read mutable attributes mid-check.
    """

    user: User | None = None
    user_id: uuid.UUID | None = None
    conversation_id: uuid.UUID | None = None
    run_id: uuid.UUID | None = None
    is_admin: bool = False

    @property
    def has_user(self) -> bool:
        return self.user_id is not None


def make_tool_context(
    user: User | None,
    *,
    conversation_id: uuid.UUID | None = None,
    run_id: uuid.UUID | None = None,
) -> ToolContext:
    """Build a :class:`ToolContext` from an authenticated user (None-safe)."""
    if user is None:
        return ToolContext(conversation_id=conversation_id, run_id=run_id)
    raw_id = getattr(user, "id", None)
    user_id: uuid.UUID | None
    if isinstance(raw_id, uuid.UUID):
        user_id = raw_id
    elif raw_id is None:
        user_id = None
    else:
        try:
            user_id = uuid.UUID(str(raw_id))
        except (TypeError, ValueError):
            user_id = None
    return ToolContext(
        user=user,
        user_id=user_id,
        conversation_id=conversation_id,
        run_id=run_id,
        is_admin=bool(getattr(user, "role", "") == "admin"),
    )


_CTX: ContextVar[ToolContext | None] = ContextVar("tool_context", default=None)


def bind_tool_context(ctx: ToolContext | None) -> Token[ToolContext | None]:
    """Bind ``ctx`` for the current task; the token must be reset in ``finally``."""
    return _CTX.set(ctx)


def reset_tool_context(token: Token[ToolContext | None]) -> None:
    """Undo a :func:`bind_tool_context` (``set``/``reset`` must balance)."""
    _CTX.reset(token)


def current_tool_context() -> ToolContext | None:
    """The bound context, or ``None`` when the tool runs outside a bound caller."""
    return _CTX.get()


@contextmanager
def use_tool_context(ctx: ToolContext | None) -> Iterator[None]:
    """Synchronous ``with`` binding — the body may ``await``, the binding may not.

    Kept lock-free on purpose (see the module docstring): the only work here is a
    contextvar set/reset, so wrapping an ``await`` never holds a mutex.
    """
    token = bind_tool_context(ctx)
    try:
        yield
    finally:
        reset_tool_context(token)


def forbidden_result(reason: str, **extra: Any) -> dict[str, Any]:
    """Canonical refusal payload for a tool that may not serve this caller.

    Tools return this instead of raising so the gateway records a clean
    ``ok=False`` result and the model sees an explicit, non-retryable denial.
    ``authorized=False`` lets callers (and the UI) distinguish a permission
    failure from an ordinary tool error without string matching.
    """
    result: dict[str, Any] = {"ok": False, "authorized": False, "error": reason}
    result.update(extra)
    return result
