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
from typing import Callable

logger = logging.getLogger(__name__)

# Best-effort tokenizer: tiktoken if available (accurate), else a heuristic that
# roughly accounts for CJK chars (≈1 token each) + ascii words (≈1.33 tokens/word).
_TIKTOKEN = None
try:  # pragma: no cover - import probe
    import tiktoken as _tiktoken_mod  # type: ignore

    _TIKTOKEN = _tiktoken_mod.get_encoding("cl100k_base")
except Exception:  # noqa: BLE001 — optional dep
    _TIKTOKEN = None


def estimate_tokens(text: str) -> int:
    """Rough token estimate (tiktoken when available; CJK-aware heuristic otherwise)."""
    if not text:
        return 0
    if _TIKTOKEN is not None:
        try:
            return len(_TIKTOKEN.encode(text))
        except Exception:  # noqa: BLE001
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


def compact_messages(
    messages: list[dict],
    *,
    summarize_fn: Callable[[list[dict]], str],
    keep_recent_tokens: int = 4000,
    prefill_baseline_tokens: int = 0,
) -> tuple[list[dict], str]:
    """Compact a message list: summarize the older prefix, keep a verbatim tail.

    ``messages[0]`` (the system message) is ALWAYS preserved unchanged. Of the
    rest, a newest-first tail up to ``keep_recent_tokens`` is kept verbatim; the
    older prefix is summarized via ``summarize_fn``. Returns
    ``(new_messages, summary)`` where summary is "" if nothing was compacted.

    Mid-turn compaction (interrupting an in-flight agent loop) is a follow-up;
    this implements the pre-turn / between-turns case.
    """
    if not messages:
        return list(messages), ""

    # Split off the leading system message(s) (role == "system") — never compact them.
    sys_count = 0
    for m in messages:
        if m.get("role") == "system":
            sys_count += 1
        else:
            break
    sys_msgs = messages[:sys_count]
    body_msgs = messages[sys_count:]

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
    keep_ids = {id(m) for m in tail}
    older = [m for m in body_msgs if id(m) not in keep_ids]

    if not older:
        return list(messages), ""  # nothing to compact

    summary = summarize_fn(older) or ""
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
    return sys_msgs + [summary_msg] + tail, summary
