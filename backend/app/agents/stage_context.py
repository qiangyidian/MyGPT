"""Per-run shared context for the CrewAI multi-agent path.

Two problems this solves:

1. **Tool → agent attribution.** CrewAI runs a stage by calling
   ``agent.aexecute_task``; the agent's adapted tools may execute in worker
   threads (see ``adapters.tool_adapter._bridge_async``). The current agent id
   varies per stage, but adapters are built once per run. A mutable
   :class:`StageContext` holder is shared between the executor (which sets
   ``agent_id`` before each stage) and every adapter (which reads it at call
   time). String assignment is atomic in CPython, and we never switch the
   agent mid-stage, so a tool always reads the correct id.

2. **Real-time tool events across threads.** The runtime drains an
   ``asyncio.Queue`` on the main loop while ``aexecute_task`` runs. Tool
   adapters run in worker threads and must forward ``tool_call``/``tool_result``
   events back to that main-loop queue *thread-safely*. ``StageContext.emit``
   uses ``loop.call_soon_threadsafe`` to do exactly that.

The native runtime does not use this — it emits tool events directly on the
main loop and attributes everything to the implicit single agent.
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Callable

if TYPE_CHECKING:
    from app.agents.schemas import AgentEvent

logger = logging.getLogger(__name__)

# Soft cap on the per-run event queue. Tools are not high-frequency; if this
# ever fills we drop (logged) rather than block a worker thread.
_QUEUE_MAX = 512


@dataclass
class StageContext:
    """Shared, mutable per-run context for CrewAI tool attribution + events.

    The executor sets :attr:`agent_id` (and :attr:`task_id`) before each stage;
    adapters read them inside ``_run``. :meth:`emit` forwards an event to the
    main-loop queue thread-safely.
    """

    run_id: str
    loop: asyncio.AbstractEventLoop
    # Bounded but generous: structural events (agent_status/edge/run_status) are
    # few; tool events are the only high-frequency source. If this ever fills
    # (thousands of tool calls in one run), we coalesce + drop noisily-once
    # rather than block a worker thread.
    queue: "asyncio.Queue[AgentEvent | None]" = field(
        default_factory=lambda: asyncio.Queue(maxsize=_QUEUE_MAX)
    )
    # Set by the executor before each stage; read by adapters at tool-call time.
    agent_id: str = ""
    task_id: str = ""
    # Optional cross-thread approval bridge — when set, dangerous tools that
    # need approval pause the worker thread until the user decides.
    approval_bridge: Any = None
    # ---- Phase 1: streaming writer support ----
    # Populated by CrewAIRuntime before the stage walk so StreamingWriterExecutor
    # can call the provider directly and mutate the assistant message. All
    # Optional so FakeStageExecutor/DemoStageExecutor tests are unaffected.
    provider: Any = None
    model_config: Any = None
    assistant_msg: Any = None
    user_content: str = ""
    cancel_event: Any = None
    # Phase 2: user instructions appended mid-run, drained into the next stage.
    pending_instructions: list = field(default_factory=list)
    # Set True by StreamingWriterExecutor once the writer has streamed its
    # tokens; the runtime reads this to skip the legacy bulk-token emission.
    writer_streamed: bool = False
    # A sink the gateway can call to record that an AgentStep was written for
    # the current agent (used to keep graph_state's current_tool fresh). Optional.
    on_step_persisted: Callable[[str, str, str], None] | None = None  # (agent_id, tool_name, status)
    # Coalesce: track active tool_call ids per agent so a duplicate TOOL_STARTED
    # for the same (agent, call) is a no-op (defensive against adapter retries).
    _seen_tool_calls: set = field(default_factory=set)
    # Counted drops (logged once per run when non-zero, not per drop).
    _dropped: int = 0
    _drop_warned: bool = False

    def set_stage(self, *, agent_id: str, task_id: str = "") -> None:
        self.agent_id = agent_id
        self.task_id = task_id

    def emit(self, event: "AgentEvent") -> None:
        """Thread-safe forward to the main-loop queue. Never blocks."""
        # Coalesce duplicate tool_call events for the same (agent, call_id).
        if getattr(event, "kind", None) == "tool_call":
            key = (event.data.get("agent_id"), event.data.get("id"))
            if key in self._seen_tool_calls:
                return  # already announced this exact tool call
            self._seen_tool_calls.add(key)
        try:
            self.loop.call_soon_threadsafe(self._put_nowait, event)
        except RuntimeError:
            # Loop closed (run ended) — drop silently.
            pass

    def _put_nowait(self, event: "AgentEvent") -> None:
        try:
            self.queue.put_nowait(event)
        except asyncio.QueueFull:
            self._dropped += 1
            if not self._drop_warned:
                self._drop_warned = True
                logger.warning(
                    "agent event queue full (run=%s); coalescing further tool "
                    "events (first drop logged only)",
                    self.run_id,
                )

    def close(self) -> None:
        """Signal the drainer to stop (sentinel)."""
        if self._dropped and not self._drop_warned:
            pass
        try:
            self.loop.call_soon_threadsafe(self._put_nowait, None)
        except RuntimeError:
            pass


def make_stage_context(run_id: str) -> StageContext:
    """Build a StageContext bound to the currently-running event loop."""
    return StageContext(run_id=run_id, loop=asyncio.get_running_loop())
