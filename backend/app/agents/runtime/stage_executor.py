"""Per-stage agent execution, abstracted for testability.

A "stage" is one agent running one task. The :class:`CrewAIRuntime` orchestrates
stages sequentially or in parallel and drives the
:class:`~app.agents.lifecycle.AgentLifecycleEmitter` around each. The actual
execution — calling the LLM, running tools — is hidden behind the
:class:`StageExecutor` protocol so the lifecycle ordering can be unit-tested
with :class:`FakeStageExecutor` (no live LLM, no CrewAI).

Threading note: CrewAI's ``aexecute_task`` may invoke the adapted tools in
worker threads. Those adapters read :attr:`StageContext.agent_id` (set here
before each stage) and forward ``tool_call``/``tool_result`` events back to the
main-loop queue via :meth:`StageContext.emit` (thread-safe). So tool events
arrive in real time, attributed to the right agent, even though the tool runs
off-loop.
"""
from __future__ import annotations

import asyncio
import inspect
import logging
import uuid
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

from app.agents.continuation import aggregate_usage
from app.agents.stage_context import StageContext
from app.agents.token_budget import (
    MESSAGE_TOO_LARGE,
    PromptAdmissionError,
    calculate_prompt_budget,
)
from app.model_capabilities import capabilities_from_config

logger = logging.getLogger(__name__)

_STAGE_SYSTEM_TOKEN_RESERVE = 512
_STAGE_TOOL_TOKEN_RESERVE = 256
_TRUNCATION_MARKER = "[Earlier dependency context truncated]\n"


def safe_positive_int(value: Any, default: int) -> int:
    """Coerce ``value`` to a positive int, falling back to ``default`` otherwise.

    Shared so the Native runtime and the multi-agent Writer derive the output
    token budget the same way (no inconsistent copies across modules).
    """
    try:
        v = int(value)
        return v if v > 0 else default
    except (TypeError, ValueError):
        return default


@dataclass
class StageResult:
    """Outcome of one agent stage."""

    agent_id: str
    raw: str = ""
    output_summary: str = ""
    # Structured payload (e.g. parsed evidence) — optional, passed as context
    # to downstream stages.
    structured: Any = None
    # Tool calls that occurred during this stage (for the activity feed /
    # audit). Populated by the executor from events it observed.
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    # Aggregate for every model attempt performed by this stage.
    usage: dict[str, int | float] | None = None


@runtime_checkable
class StageExecutor(Protocol):
    """Execute one agent stage. Raises on failure (runtime emits agent_failed)."""

    async def execute(
        self,
        *,
        agent_id: str,
        agent: Any,
        task: Any,
        context: str | None,
        stage_ctx: StageContext,
    ) -> StageResult: ...


# --------------------------------------------------------------------------- #
# Real executor: CrewAI aexecute_task
# --------------------------------------------------------------------------- #
class CrewAIStageExecutor:
    """Runs a single agent's task via ``Agent.aexecute_task``.

    Real LLM call, real tools (through the gateway adapters). The agent's
    tools were built with the shared ``StageContext`` so tool events are
    attributed and forwarded in real time.
    """

    def __init__(self, *, summarize_chars: int = 160) -> None:
        self._summarize_chars = summarize_chars

    async def execute(
        self,
        *,
        agent_id: str,
        agent: Any,
        task: Any,
        context: str | None,
        stage_ctx: StageContext,
    ) -> StageResult:
        admitted_context = admit_stage_dispatch(
            agent=agent, task=task, context=context, stage_ctx=stage_ctx
        )
        stage_ctx.set_stage(agent_id=agent_id, task_id=getattr(task, "id", "") or "")
        llm = getattr(agent, "llm", None)
        metered = await stage_ctx.llm_usage.begin(
            llm, lambda: _llm_usage_snapshot(agent)
        )
        completed = False
        llm_usage: dict[str, int | float] | None = None
        try:
            output = await agent.aexecute_task(task, context=admitted_context)
            completed = True
        finally:
            if metered:
                llm_usage = await stage_ctx.llm_usage.claim(
                    llm, lambda: _llm_usage_snapshot(agent)
                )
            if not completed and llm_usage:
                stage_ctx.record_usage(
                    f"model:{agent_id}:{uuid.uuid4().hex}", llm_usage
                )
        raw = _extract_raw(output)
        # CrewAI's installed Agent.aexecute_task returns a raw string. Its LLM
        # owns cumulative counters, so the before/after delta is authoritative
        # and must not be added again from duplicate output/agent snapshots.
        usage = (
            llm_usage
            if metered
            else aggregate_usage(_extract_usage_rounds(output))
        )
        return StageResult(
            agent_id=agent_id,
            raw=raw,
            output_summary=_summarize(raw, self._summarize_chars),
            structured=output,
            usage=usage,
        )


def admit_stage_dispatch(
    *,
    agent: Any,
    task: Any,
    context: str | None,
    stage_ctx: StageContext,
    fixed_prompt_tokens: int = 0,
) -> str | None:
    """Bound a stage's exact task/context immediately before model dispatch.

    ``fixed_prompt_tokens`` reserves caller-owned system/user scaffolding that
    is not represented by the CrewAI task description.
    """
    cfg = stage_ctx.model_config
    if cfg is None:
        return context

    caps = capabilities_from_config(cfg)
    tool_count = len(getattr(agent, "tools", None) or [])
    budget = calculate_prompt_budget(
        caps,
        requested_output=caps.max_output_tokens,
        tool_schema_tokens=(
            _STAGE_SYSTEM_TOKEN_RESERVE + tool_count * _STAGE_TOOL_TOKEN_RESERVE
        ),
    )
    # One character per token is deliberately conservative for mixed CJK,
    # source code, and serialized dependency payloads.
    description = str(getattr(task, "description", "") or "")
    fixed_tokens = len(description) + max(0, fixed_prompt_tokens)
    if fixed_tokens > budget.input_tokens:
        raise PromptAdmissionError(
            MESSAGE_TOO_LARGE,
            (
                "The stage task or fixed prompt is too large for this model's "
                "prompt budget"
            ),
        )

    if context is None:
        return None
    remaining = budget.input_tokens - fixed_tokens
    if len(context) <= remaining:
        return context
    if remaining <= len(_TRUNCATION_MARKER):
        raise PromptAdmissionError(
            MESSAGE_TOO_LARGE,
            "The stage task leaves no room for dependency context",
        )
    return _TRUNCATION_MARKER + context[-(remaining - len(_TRUNCATION_MARKER)) :]


# --------------------------------------------------------------------------- #
# Fake executor: deterministic, no LLM — for tests + offline demos
# --------------------------------------------------------------------------- #
class FakeStageExecutor:
    """Simulates a stage: optional tool calls, a delay, then a canned result.

    The ``behavior`` map is ``{agent_id: FakeBehavior}``. Unknown agents get a
    default success with a short delay. This lets tests script scenarios A/B/C/D
    (serial, parallel, approval, failure) precisely.
    """

    @dataclass
    class Behavior:
        delay: float = 0.05
        output: str = ""
        summary: str = ""
        tools: list[dict[str, Any]] = field(default_factory=list)  # [{name, args, ok, result}]
        fail: str | None = None  # error message -> raise
        # When set, the named tool simulates a dangerous-tool approval cycle:
        # emits tool_call, pauses via stage_ctx.approval_bridge, then on approve
        # emits a success tool_result. {"tool": "db_query", "approval_id": <uuid>}
        approval: dict[str, Any] | None = None

    def __init__(self, behaviors: dict[str, "FakeStageExecutor.Behavior"] | None = None) -> None:
        self.behaviors = behaviors or {}
        # Track which agents started/finished, for assertion helpers in tests.
        self.started: list[str] = []
        self.finished: list[str] = []

    async def execute(
        self,
        *,
        agent_id: str,
        agent: Any,
        task: Any,
        context: str | None,
        stage_ctx: StageContext,
    ) -> StageResult:
        from app.agents.schemas import ev_tool_call, ev_tool_result

        b = self.behaviors.get(agent_id, self.Behavior())
        stage_ctx.set_stage(agent_id=agent_id, task_id="fake-task")
        self.started.append(agent_id)
        # Emit tool events in real time (routed through the stage ctx queue).
        tool_calls: list[dict[str, Any]] = []
        for t in b.tools:
            call_id = f"call-{agent_id}-{uuid.uuid4().hex[:6]}"
            stage_ctx.emit(ev_tool_call(
                id=call_id, name=t["name"], arguments=t.get("args", {}),
                agent_id=agent_id, task_id="fake-task",
            ))
            await asyncio.sleep(0.01)  # observable as "running"

            # Simulate a dangerous-tool approval pause if configured. The fake
            # executor runs on the main loop, so it uses the async entry point
            # (the real adapter, off-loop, uses the sync request_pause).
            if b.approval and b.approval.get("tool") == t["name"] and stage_ctx.approval_bridge is not None:
                decision, _reason = await stage_ctx.approval_bridge.request_pause_async(
                    approval_id=b.approval["approval_id"],
                    agent_id=agent_id,
                    tool_name=t["name"],
                )
                ok = decision == "approved"
                stage_ctx.emit(ev_tool_result(
                    id=call_id, name=t["name"], ok=ok,
                    result=t.get("result") if ok else None,
                    error=None if ok else f"approval {decision}",
                    agent_id=agent_id, task_id="fake-task",
                ))
                tool_calls.append({"name": t["name"], "ok": ok})
                continue

            stage_ctx.emit(ev_tool_result(
                id=call_id, name=t["name"], ok=t.get("ok", True),
                result=t.get("result"), error=t.get("error"),
                agent_id=agent_id, task_id="fake-task",
            ))
            tool_calls.append({"name": t["name"], "ok": t.get("ok", True)})

        if b.delay:
            await asyncio.sleep(b.delay)

        if b.fail is not None:
            raise RuntimeError(b.fail)

        self.finished.append(agent_id)
        return StageResult(
            agent_id=agent_id,
            raw=b.output or f"[{agent_id}] done",
            output_summary=b.summary or (b.output or f"[{agent_id}] done")[:160],
            tool_calls=tool_calls,
        )


# --------------------------------------------------------------------------- #
# Demo executor: no LLM, but produces realistic per-role behaviour so the full
# multi-agent panel can be exercised live (real SSE, real graph, real tool
# attribution) without an external model endpoint.
#
# ⚠️ ISOLATION: this executor is reached ONLY when BOTH AGENT_DEMO_MODE is
# enabled AND the request carries an explicit demo=True flag (see
# CrewAIRuntime._run_multi_agent). It is NEVER a transparent substitute for the
# real executor on a normal /api/chat/stream turn — a plain mode=deep_research
# request runs the real executor (or falls back to native with a visible
# reason), never this canned one. The canned content itself lives in the
# demo-only module demo_content.py.
# --------------------------------------------------------------------------- #
class DemoStageExecutor(FakeStageExecutor):
    """FakeStageExecutor with sensible default behaviours keyed by agent role.

    Used ONLY on an explicit per-request demo opt-in (request.demo=True with
    AGENT_DEMO_MODE enabled) so the panel can be hand-verified without an LLM.
    The defaults simulate a research crew: researchers call web_search, the
    analyst cross-checks, the writer produces a short cited answer. Override
    per-agent via the ``behaviours`` map (e.g. to script a failure for scenario
    D). The canned text itself is in demo_content.py.
    """

    def __init__(self, behaviours: dict[str, "FakeStageExecutor.Behavior"] | None = None) -> None:
        defaults = self._defaults()
        if behaviours:
            for k, v in behaviours.items():
                merged = defaults.get(k, self.Behavior())
                # let caller override fields (e.g. fail) while keeping defaults
                merged = self.Behavior(
                    delay=v.delay if v.delay != 0.05 else merged.delay,
                    output=v.output or merged.output,
                    summary=v.summary or merged.summary,
                    tools=v.tools or merged.tools,
                    fail=v.fail,
                )
                defaults[k] = merged
        super().__init__(defaults)

    @staticmethod
    def _defaults() -> dict[str, "FakeStageExecutor.Behavior"]:
        # The canned demo content lives in a dedicated demo-only module so the
        # fabricated "writer" answer is NOT co-located with the real executors
        # (CrewAIStageExecutor / FakeStageExecutor) and cannot be picked up by
        # accident on a normal runtime path. See demo_content.py for the full
        # warning + isolation rationale.
        from app.agents.runtime.demo_content import build_demo_behaviours

        return build_demo_behaviours()


# --------------------------------------------------------------------------- #
def _extract_raw(output: Any) -> str:
    """Best-effort: pull a string out of a CrewAI TaskOutput / CrewOutput."""
    if output is None:
        return ""
    for attr in ("raw", "content", "output"):
        v = getattr(output, attr, None)
        if isinstance(v, str) and v:
            return v
    if isinstance(output, str):
        return output
    return str(output)


def _extract_usage_rounds(*sources: Any) -> list[dict[str, Any]]:
    """Extract provider usage snapshots from CrewAI outputs/agents.

    Output-owned usage is preferred because agent-level metrics commonly repeat
    the same totals. Lists are retained as individual retry/model attempts.
    """

    def mappings(value: Any) -> list[dict[str, Any]]:
        if value is None:
            return []
        if isinstance(value, (list, tuple)):
            out: list[dict[str, Any]] = []
            for item in value:
                out.extend(mappings(item))
            return out
        if isinstance(value, dict):
            return [value]
        dump = getattr(value, "model_dump", None)
        if callable(dump):
            dumped = dump()
            return [dumped] if isinstance(dumped, dict) else []
        attrs = getattr(value, "__dict__", None)
        return [dict(attrs)] if isinstance(attrs, dict) else []

    for source in sources:
        if source is None:
            continue
        for attr in ("token_usage", "usage", "usage_metrics"):
            found = mappings(getattr(source, attr, None))
            if found:
                return found
    return []


async def _llm_usage_snapshot(agent: Any) -> dict[str, int | float] | None:
    """Read and normalize CrewAI's cumulative LLM counters.

    CrewAI-compatible LLMs expose both synchronous and asynchronous summary
    methods, returning either dictionaries or UsageMetrics-like objects.
    Metering is observational, so an unavailable/broken summary must not mask
    the actual stage result or provider error.
    """
    summary = getattr(getattr(agent, "llm", None), "get_token_usage_summary", None)
    if not callable(summary):
        return None
    try:
        value = summary()
        if inspect.isawaitable(value):
            value = await value
        mappings = _extract_usage_rounds(SimpleUsageSource(value))
        return aggregate_usage(mappings)
    except Exception:  # noqa: BLE001 - usage collection is best-effort
        logger.debug("could not read CrewAI LLM usage summary", exc_info=True)
        return None


class SimpleUsageSource:
    """Adapter allowing the existing metrics normalizer to consume a value."""

    def __init__(self, usage: Any) -> None:
        self.usage = usage


def _summarize(text: str, n: int) -> str:
    text = (text or "").strip().replace("\n", " ")
    return text[:n] + ("…" if len(text) > n else "")
