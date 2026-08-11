"""Task 7: the ONE ContextManager.

A single object used for:

  * **Budget partitioning** — split the Task-1 ``TokenBudget`` into prompt
    slices (protected prefix / recent-keep tail / compactable body budget).
  * **Tool-pair-aware mid-run compaction** — compact between agent-loop
    iterations (not only pre-turn), never splitting a tool_call from its
    tool_result. ``compact`` is sync (unit-test double summarizer);
    ``compact_async`` takes an async LLM-backed summarizer for production.
  * **Protected-fragment preservation** — leading protected blocks survive
    compaction verbatim.
  * **Attachment / memory folding** — assemble a COMPLETE effective system
    prompt from persisted fragments (pure — no process-local mutable world
    state, so any worker produces the same prompt).
  * **Output spill** — replace an oversized tool result with an opaque
    authorized ``ArtifactHandle`` (the full artifact service lands in Task 10).

Invocation sites (as of Task 7):
  * ``app.services.chat_service`` — ``assemble_system_prompt`` for the
    effective system prompt on BOTH the inline and durable paths.
  * ``app.agents.runtime.native_runtime`` — ``compact_async`` (gated by
    ``should_compact_midrun``) in the per-round loop, with an LLM-backed
    ``summarize_fn_async`` reusing ``planning.summarize_prefix``.
  * The workflow engine (Task 6) does NOT yet consume this manager.

The core is **pure + offline-testable**: the summarizer and spill writer are
injected, mirroring ``context_compaction.compact_messages``'s ``summarize_fn``
pattern. Nothing here reaches for a live LLM, DB, or Qdrant.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Awaitable, Callable

from app.agents.context_compaction import (
    _assemble_compacted,
    _split_for_compaction,
    compact_messages,
    estimate_messages_tokens,
    estimate_tokens,
    should_compact,
)
from app.agents.output_spill import ArtifactHandle, spill as _spill_text
from app.agents.token_budget import TokenBudget


# Default body-after-prefix compaction threshold (fraction of input budget).
_AUTO_COMPACT_FRACTION = 0.8
# Recent-keep tail = this fraction of the input budget (the verbatim tail that
# survives compaction; the older prefix is summarized).
_RECENT_KEEP_FRACTION = 0.4
# Safety headroom reserved inside the body budget before compaction triggers.
_BODY_HEADROOM_TOKENS = 256


@dataclass(frozen=True, slots=True)
class BudgetPartition:
    """How the ContextManager slices the Task-1 ``TokenBudget``."""

    input_tokens: int
    # Tokens reserved for the always-preserved protected prefix (system prompt,
    # pinned instructions). Measured from the actual protected block.
    protected_tokens: int
    # Token budget for the verbatim recent tail kept across compaction.
    recent_keep_tokens: int
    # Auto-compact trigger for the growable body (body-after-prefix).
    body_budget_tokens: int


@dataclass(frozen=True, slots=True)
class DownshiftDirective:
    """Result of a model-switch downshift: whether to recompact and how."""

    must_recompact: bool
    input_budget: int
    compacted_messages: list[dict]


class ContextManager:
    """ONE context manager for prompt partitioning, compaction, spill, assembly.

    Construct once with an injected ``summarize_fn`` (and optional
    ``spill_writer``); call ``compact`` / ``should_compact_midrun`` /
    ``spill_tool_result`` / ``assemble_system_prompt`` / ``partition_budget`` /
    ``downshift_compaction`` from chat or the workflow engine.
    """

    def __init__(
        self,
        *,
        summarize_fn: Callable[[list[dict]], str],
        spill_writer: Callable[[str, str], str] | None = None,
        auto_compact_fraction: float = _AUTO_COMPACT_FRACTION,
        recent_keep_fraction: float = _RECENT_KEEP_FRACTION,
    ) -> None:
        self._summarize_fn = summarize_fn
        self._spill_writer = spill_writer
        self._auto_compact_fraction = auto_compact_fraction
        self._recent_keep_fraction = recent_keep_fraction

    # ------------------------------------------------------------------ #
    # Budget partitioning
    # ------------------------------------------------------------------ #
    def partition_budget(
        self,
        budget: TokenBudget,
        *,
        protected_messages: list[dict] | None = None,
    ) -> BudgetPartition:
        """Partition the Task-1 ``TokenBudget`` into prompt slices.

        ``protected_messages`` (the leading system + pinned fragments) is
        measured exactly; the remainder is split into a recent-keep tail and a
        compactable body budget.
        """
        protected = protected_messages or []
        protected_tokens = estimate_messages_tokens(protected)
        remaining = max(0, budget.input_tokens - protected_tokens)
        recent_keep = max(
            256, int(remaining * self._recent_keep_fraction)
        )
        body_budget = max(
            0, int(budget.input_tokens * self._auto_compact_fraction)
        )
        return BudgetPartition(
            input_tokens=budget.input_tokens,
            protected_tokens=protected_tokens,
            recent_keep_tokens=recent_keep,
            body_budget_tokens=body_budget,
        )

    # ------------------------------------------------------------------ #
    # Mid-run compaction
    # ------------------------------------------------------------------ #
    def should_compact_midrun(
        self,
        messages: list[dict],
        *,
        input_budget: int,
        prefill_baseline_tokens: int = 0,
    ) -> bool:
        """True when the growable body exceeds the compact limit, or the hard
        input budget is hit. Safe to call between workflow steps / loop iters.
        """
        total = estimate_messages_tokens(messages)
        # The hard backstop is the input_budget (per-model window minus output
        # reserve); the soft trigger is the auto-compact fraction of it.
        auto_limit = max(0, int(input_budget * self._auto_compact_fraction))
        return should_compact(
            total_tokens=total,
            prefill_baseline_tokens=prefill_baseline_tokens,
            auto_compact_limit=auto_limit,
            hard_window_tokens=input_budget,
        )

    def compact(
        self,
        messages: list[dict],
        *,
        input_budget: int,
        prefill_baseline_tokens: int = 0,
        protected_count: int = 0,
    ) -> list[dict]:
        """Compact ``messages`` in-place for the given input budget.

        Tool-pair-aware: a tool_call and its tool_result are never split. If the
        transcript already fits, it is returned unchanged. ``protected_count``
        leading non-system messages are preserved verbatim (protected fragments).
        """
        if not messages:
            return list(messages)

        # Derive the recent-keep tail from the budget. The tail is what survives
        # verbatim; the older prefix is summarized.
        recent_keep = max(
            256, int(input_budget * self._recent_keep_fraction)
        )

        # If there's nothing to compact (transcript fits), short-circuit —
        # never invoke the summarizer needlessly.
        if not self.should_compact_midrun(
            messages,
            input_budget=input_budget,
            prefill_baseline_tokens=prefill_baseline_tokens,
        ):
            return list(messages)

        # Split off the leading system message(s) so protected_count applies to
        # the non-system body (matching compact_messages semantics).
        sys_count = 0
        for m in messages:
            if m.get("role") == "system":
                sys_count += 1
            else:
                break
        protected_body_count = max(0, protected_count)
        new_msgs, _summary = compact_messages(
            messages,
            summarize_fn=self._summarize_fn,
            keep_recent_tokens=recent_keep,
            prefill_baseline_tokens=prefill_baseline_tokens,
            preserve_tool_pairs=True,
            protected_count=protected_body_count,
        )
        return new_msgs

    async def compact_async(
        self,
        messages: list[dict],
        *,
        input_budget: int,
        summarize_fn_async: Callable[[list[dict]], Awaitable[str]],
        prefill_baseline_tokens: int = 0,
        protected_count: int = 0,
    ) -> list[dict]:
        """Async mid-run compaction with an LLM-backed summarizer.

        Same split / tool-pair atomicity / protected-fragment semantics as
        :meth:`compact`, but the older prefix is summarized by an async function
        (e.g. the provider-backed ``summarize_history`` path). This is the
        production entry point invoked from the native runtime's per-round loop:
        when ``should_compact_midrun`` is False the transcript is returned
        UNCHANGED, so runs that don't hit budget pressure behave identically to
        today. When it is True, the older prefix is summarized via the injected
        async summarizer and the verbatim recent tail + tool pairs are kept.
        """
        if not messages:
            return list(messages)

        if not self.should_compact_midrun(
            messages,
            input_budget=input_budget,
            prefill_baseline_tokens=prefill_baseline_tokens,
        ):
            return list(messages)

        recent_keep = max(256, int(input_budget * self._recent_keep_fraction))
        sys_msgs, protected_body, tail, older, used = _split_for_compaction(
            messages,
            keep_recent_tokens=recent_keep,
            preserve_tool_pairs=True,
            protected_count=protected_count,
        )
        if not older:
            return list(messages)

        summary = (await summarize_fn_async(older)) or ""
        return _assemble_compacted(
            sys_msgs, protected_body, tail, older, summary,
            prefill_baseline_tokens=prefill_baseline_tokens, used=used,
        )

    # ------------------------------------------------------------------ #
    # Output spill → opaque artifact handle
    # ------------------------------------------------------------------ #
    def spill_tool_result(
        self, text: str, *, budget_tokens: int, key: str = "tool_result"
    ) -> tuple[str, ArtifactHandle | None]:
        """Spill an oversized tool result to an opaque artifact handle.

        Returns ``(in_context_preview, handle)``. ``handle`` is None when the
        text fits the budget or the writer failed (best-effort — never blocks).
        The handle is opaque (``artifact:<id>`` + storage key), NOT a raw path.

        When the production writer (:func:`output_spill.production_spill_writer`)
        backed this manager, it persisted the blob as a real Artifact and stashed
        the row id; adopt it as the handle id so ``artifact:<id>`` resolves to a
        downloadable, tenant-scoped row (instead of the placeholder uuid the
        pure spill seam mints before the writer runs).
        """
        in_context, handle = _spill_text(
            text,
            budget_tokens=budget_tokens,
            write_fn=self._spill_writer,
            key=key,
        )
        if handle is not None and self._spill_writer is not None:
            real_id_getter = getattr(self._spill_writer, "last_artifact_id", None)
            real_id = real_id_getter() if callable(real_id_getter) else real_id_getter
            if real_id:
                handle = ArtifactHandle(id=f"artifact:{real_id}", storage_key=handle.storage_key)
        return in_context, handle

    # ------------------------------------------------------------------ #
    # Complete effective system prompt (pure — no process-local world state)
    # ------------------------------------------------------------------ #
    def assemble_system_prompt(
        self,
        *,
        base: str,
        rag_context: str = "",
        summary: str = "",
        goal: str = "",
        memories: list[str] | None = None,
        intent_block: str | None = None,
        behavior_blocks: list[str] | None = None,
        extra_blocks: list[str] | None = None,
    ) -> str:
        """Assemble a COMPLETE effective system prompt from persisted fragments.

        This is a PURE function of its inputs: the same arguments always yield
        the same prompt, with no module-level mutable cache that could diverge
        across workers. Every provided fragment is present in the output, so a
        cold worker produces the same prompt as a warm one.
        """
        parts: list[str] = []
        if rag_context:
            parts.append(
                "Use the following retrieved context to answer the user's question. "
                "If the context is insufficient, say so. Cite sources by their "
                "[source N] marker when relevant.\n\n"
                f"Context:\n{rag_context}"
            )
        if summary:
            parts.append(f"Earlier in this conversation (summary):\n{summary}")
        if goal:
            parts.append(f"User's ongoing goal: {goal}")
        if memories:
            joined = "\n".join(f"- {m}" for m in memories if m)
            if joined:
                parts.append(
                    "Remembered preferences about this user (user-approved):\n"
                    f"{joined}"
                )
        # The base conversation system prompt is always present.
        parts.append(base)
        if intent_block:
            parts.append(intent_block)
        for block in behavior_blocks or []:
            if block:
                parts.append(block)
        for block in extra_blocks or []:
            if block:
                parts.append(block)
        return "\n\n".join(parts)

    # ------------------------------------------------------------------ #
    # Model-switch downshift → mid-run compaction
    # ------------------------------------------------------------------ #
    def downshift_compaction(
        self,
        *,
        previous_window_tokens: int,
        current_window_tokens: int,
        active_messages: list[dict],
    ) -> DownshiftDirective:
        """On a context-window downshift, compact the active transcript to fit
        the NEW (smaller) window. Returns a directive carrying the compacted
        transcript and whether recompaction was required.

        Mirrors :func:`app.agents.model_switch.is_downshift`: the dangerous case
        is a smaller window where the active transcript already exceeds it.
        """
        from app.agents.model_switch import is_downshift

        active_tokens = estimate_messages_tokens(active_messages)
        # Input budget for the post-downshift compaction target. Output / tool
        # reserve is applied by TokenBudget upstream; here we reserve a
        # conservative output slice so compaction targets a safe input budget.
        new_input_budget = max(
            512, int(current_window_tokens * 0.75)
        )
        must_recompact = is_downshift(
            previous_window_tokens=previous_window_tokens,
            current_window_tokens=current_window_tokens,
            active_tokens=active_tokens,
        )
        if not must_recompact:
            return DownshiftDirective(
                must_recompact=False,
                input_budget=new_input_budget,
                compacted_messages=list(active_messages),
            )
        compacted = self.compact(active_messages, input_budget=new_input_budget)
        return DownshiftDirective(
            must_recompact=True,
            input_budget=new_input_budget,
            compacted_messages=compacted,
        )


__all__ = [
    "ArtifactHandle",
    "BudgetPartition",
    "ContextManager",
    "DownshiftDirective",
]
