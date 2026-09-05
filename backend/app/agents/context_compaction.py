"""Token-budget-aware context compaction (Codex pattern).

Two ideas ported from Codex:

1. **Body-after-prefix budgeting.** A naive "compact at 80% of the context
   window" over-compacts, because the static system-prompt + initial-context
   prefix is large and never grows. We measure only the *growable* body tokens
   (total − a stored prefill baseline) against ``auto_compact_limit``, with the
   model's hard context window as a backstop so a misconfigured budget can't
   deadlock the session.

2. **Summary + verbatim tail (newest-first).** Compaction doesn't replace
   everything with a summary — it keeps a token-budgeted tail of recent messages
   verbatim (preserving tool-call / multi-agent continuity) and summarizes only
   the older prefix. Newest-first means the most recent turn is always complete.

The summary step is injected (``summarize_fn``) so this module is pure and
unit-testable without an LLM.
"""
from __future__ import annotations

import logging
from collections.abc import Callable

logger = logging.getLogger(__name__)

# Best-effort tokenizer: tiktoken if available (accurate), else a heuristic that
# roughly accounts for CJK chars (≈1 token each) + ascii words (≈1.33 tokens/word).
_TIKTOKEN = None
try:  # pragma: no cover - import probe
    import tiktoken as _tiktoken_mod  # type: ignore

    _TIKTOKEN = _tiktoken_mod.get_encoding("cl100k_base")
except Exception:
    _TIKTOKEN = None


def estimate_tokens(text: str) -> int:
    """Rough token estimate (tiktoken when available; CJK-aware heuristic otherwise)."""
    if not text:
        return 0
    if _TIKTOKEN is not None:
        try:
            return len(_TIKTOKEN.encode(text))
        except Exception:
            pass
    cjk = sum(1 for c in text if "一" <= c <= "鿿")
    ascii_words = sum(1 for _ in text.replace("\n", " ").split(" ") if _.strip())
    return cjk + int(ascii_words * 1.33) + 1


def estimate_messages_tokens(messages: list[dict]) -> int:
    """Sum of estimated content tokens across messages (ignores roles/overhead)."""
    total = 0
    for m in messages:
        c = m.get("content")
        if isinstance(c, str):
            total += estimate_tokens(c)
        elif isinstance(c, list):
            for part in c:
                if isinstance(part, dict):
                    total += estimate_tokens(part.get("text", ""))
    return total


def should_compact(
    *,
    total_tokens: int,
    prefill_baseline_tokens: int,
    auto_compact_limit: int,
    hard_window_tokens: int,
) -> bool:
    """True when the *growable* body exceeds the compact limit, OR the hard window is hit.

    ``prefill_baseline_tokens`` is the token count present at the start of this
    compaction window (system prompt + initial context) — subtracted so we compact
    on real conversation growth, not total size.
    """
    body = max(0, total_tokens - prefill_baseline_tokens)
    if auto_compact_limit > 0 and body >= auto_compact_limit:
        return True
    if hard_window_tokens > 0 and total_tokens >= hard_window_tokens:
        return True
    return False


def _split_for_compaction(
    messages: list[dict],
    *,
    keep_recent_tokens: int,
    preserve_tool_pairs: bool = True,
    protected_count: int = 0,
) -> tuple[list[dict], list[dict], list[dict], list[dict], int]:
    """Pure split: returns ``(sys_msgs, protected_body, tail, older, used_tokens)``.

    This is the planning core shared by the sync :func:`compact_messages` and
    the async :meth:`ContextManager.compact_async` path. It does NOT summarize
    — callers produce the summary (sync or async) and assemble the final list
    via :func:`_assemble_compacted`.
    """
    if not messages:
        return [], [], [], [], 0

    # Split off the leading system message(s) (role == "system") — never compact them.
    sys_count = 0
    for m in messages:
        if m.get("role") == "system":
            sys_count += 1
        else:
            break
    sys_msgs = messages[:sys_count]
    body_msgs = messages[sys_count:]

    # Protected non-system fragments (e.g. a pinned instruction block) survive
    # verbatim — they are pulled out of the compactable body and re-prepended.
    protected_body: list[dict] = []
    if protected_count > 0:
        cut = min(protected_count, len(body_msgs))
        protected_body = body_msgs[:cut]
        body_msgs = body_msgs[cut:]

    # Walk newest-first to pick the verbatim tail within budget.
    tail: list[dict] = []
    used = 0
    for m in reversed(body_msgs):
        t = estimate_messages_tokens([m])
        if tail and used + t > keep_recent_tokens:
            break
        tail.append(m)
        used += t
        if used >= keep_recent_tokens:
            break
    tail.reverse()

    # Tool-pair retention (bidirectional atomicity): if any member of a
    # tool-pair unit is in the tail, the whole unit travels with it.
    if preserve_tool_pairs:
        tail = _enforce_tool_pair_atomicity(body_msgs, tail)

    keep_ids = {id(m) for m in tail}
    older = [m for m in body_msgs if id(m) not in keep_ids]
    return sys_msgs, protected_body, tail, older, used


def _assemble_compacted(
    sys_msgs: list[dict],
    protected_body: list[dict],
    tail: list[dict],
    older: list[dict],
    summary: str,
    *,
    prefill_baseline_tokens: int,
    used: int,
) -> list[dict]:
    """Build the final compacted message list from a split + a summary string."""
    summary_msg = {
        "role": "system",
        "content": (
            f"[Earlier conversation summary ({estimate_tokens(summary)} tokens), "
            f"compact baseline={prefill_baseline_tokens}]:\n{summary}"
        ),
    }
    logger.info(
        "context compacted: %d older msgs -> summary (%d tokens); kept %d tail msgs (%d tokens)",
        len(older), estimate_tokens(summary), len(tail), used,
    )
    return sys_msgs + protected_body + [summary_msg] + tail


def compact_messages(
    messages: list[dict],
    *,
    summarize_fn: Callable[[list[dict]], str],
    keep_recent_tokens: int = 4000,
    prefill_baseline_tokens: int = 0,
    preserve_tool_pairs: bool = True,
    protected_count: int = 0,
) -> tuple[list[dict], str]:
    """Compact a message list: summarize the older prefix, keep a verbatim tail.

    ``messages[0]`` (the system message) is ALWAYS preserved unchanged. Of the
    rest, a newest-first tail up to ``keep_recent_tokens`` is kept verbatim; the
    older prefix is summarized via ``summarize_fn``. Returns
    ``(new_messages, summary)`` where summary is "" if nothing was compacted.

    Mid-turn / mid-run compaction (interrupting an in-flight agent loop) is
    supported via the same path: call this between workflow steps or agent-loop
    iterations with the current in-flight transcript. For an LLM-backed
    (async) summarizer inside a runtime loop, use
    :meth:`app.agents.context_manager.ContextManager.compact_async` instead,
    which shares the same split core.

    Tool-pair retention (``preserve_tool_pairs=True``, the default): a
    ``(assistant tool_call, tool tool_result)`` pair is treated as an atomic
    unit — both kept verbatim or both summarized together. Compaction NEVER
    emits a ``tool`` role message whose matching ``assistant`` tool_call was
    dropped, which would be an invalid transcript for the provider.

    ``protected_count`` extends the always-preserved leading block past the
    system message(s) by that many additional messages (protected fragments
    that must survive compaction verbatim).
    """
    if not messages:
        return list(messages), ""

    sys_msgs, protected_body, tail, older, used = _split_for_compaction(
        messages,
        keep_recent_tokens=keep_recent_tokens,
        preserve_tool_pairs=preserve_tool_pairs,
        protected_count=protected_count,
    )

    if not older:
        return list(messages), ""  # nothing to compact

    summary = summarize_fn(older) or ""
    new_msgs = _assemble_compacted(
        sys_msgs, protected_body, tail, older, summary,
        prefill_baseline_tokens=prefill_baseline_tokens, used=used,
    )
    return new_msgs, summary


def _enforce_tool_pair_atomicity(body_msgs: list[dict], tail: list[dict]) -> list[dict]:
    """Treat each ``(assistant tool_call, tool tool_result)`` pair as atomic.

    Compaction must NEVER produce an invalid transcript: a ``tool`` role message
    whose issuing ``assistant`` tool_call was dropped, OR an ``assistant`` with
    ``tool_calls`` whose ``tool`` results were dropped. Both directions are
    handled — if ANY member of a tool-pair unit is in the tail, the WHOLE unit
    is pulled into the tail (caller + all its results), accepting a small
    budget overshoot in exchange for transcript validity.

    Returns the (possibly extended) tail, rebuilt in original body order so an
    assistant always precedes its tool results.
    """
    if not tail:
        return tail

    # Index: tool_call_id -> (assistant message, [tool result messages])
    caller_of: dict[str, dict] = {}
    results_of: dict[str, list[dict]] = {}
    for m in body_msgs:
        if m.get("role") == "assistant":
            for tc in m.get("tool_calls") or []:
                tid = tc.get("id") if isinstance(tc, dict) else None
                if tid:
                    caller_of[tid] = m
                    results_of.setdefault(tid, [])
        elif m.get("role") == "tool":
            tid = m.get("tool_call_id")
            if tid:
                results_of.setdefault(tid, []).append(m)

    # Iteratively close the closure: any tool_call referenced (as caller OR as
    # result) by a tailed message pulls in the whole unit; pulled-in members may
    # themselves reference further pairs, so loop until stable.
    tail_ids = {id(m) for m in tail}
    queue = list(tail)
    while queue:
        m = queue.pop()
        referenced_ids: set[str] = set()
        if m.get("role") == "assistant":
            for tc in m.get("tool_calls") or []:
                tid = tc.get("id") if isinstance(tc, dict) else None
                if tid:
                    referenced_ids.add(tid)
        elif m.get("role") == "tool":
            tid = m.get("tool_call_id")
            if tid:
                referenced_ids.add(tid)
        for tid in referenced_ids:
            caller = caller_of.get(tid)
            if caller is not None and id(caller) not in tail_ids:
                tail_ids.add(id(caller))
                queue.append(caller)
            for res in results_of.get(tid, []):
                if id(res) not in tail_ids:
                    tail_ids.add(id(res))
                    queue.append(res)

    # Rebuild the tail in original body order so the assistant precedes results.
    return [m for m in body_msgs if id(m) in tail_ids]
