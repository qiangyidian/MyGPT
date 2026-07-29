"""Stage specs: the declarative description of a multi-agent flow.

A :class:`StageSpec` is one agent + its task + which prior agents' outputs feed
it. The :class:`~app.agents.runtime.crewai_runtime.CrewAIRuntime` walks the list
(either sequentially or with parallel groups for same-stage specs) and drives
the :class:`~app.agents.lifecycle.AgentLifecycleEmitter` around each execution.

Keeping this declarative (separate from the executor) is what makes the
parallel-research profile trivial: it just emits more specs at the same
``stage``, and the runtime runs them concurrently via ``asyncio.gather``.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class StageSpec:
    """One agent stage in a multi-agent flow."""

    agent_id: str
    agent: Any
    task: Any
    depends_on: list[str] = field(default_factory=list)
    # The graph ``stage`` (layer) this node belongs to — same-stage specs with
    # no dependency between them run in parallel.
    stage: int = 0
