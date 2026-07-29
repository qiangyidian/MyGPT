"""Per-run cooperative controls (Phase 2): pause / resume / instructions.

A lightweight, in-process registry of :class:`RunControl` objects keyed by
run id. The orchestrator creates one at run start; the agent_runs control
endpoints (pause/resume/instructions) mutate it; the runtime honors pause and
reads appended instructions between stages / between streamed tokens.

This is deliberately in-process (matches ``BACKGROUND_WORKER=inprocess`` and the
approval coordinator). For multi-worker deployments a Redis-backed signal
(like approval_bus) would replace it; the Control surface stays the same.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Optional

_controls: dict[str, "RunControl"] = {}


@dataclass
class RunControl:
    run_id: str
    # SET while the run is user-paused; the runtime awaits while it is set.
    paused: asyncio.Event = field(default_factory=asyncio.Event)
    # SET to request a cooperative cancel (in addition to task cancellation).
    cancel: asyncio.Event = field(default_factory=asyncio.Event)
    # Instructions the user appended mid-run (newest last).
    instructions: list[str] = field(default_factory=list)

    def pause(self) -> None:
        self.paused.set()

    def resume(self) -> None:
        self.paused.clear()

    def is_paused(self) -> bool:
        return self.paused.is_set()

    def add_instruction(self, instruction: str) -> None:
        if instruction and instruction not in self.instructions:
            self.instructions.append(instruction)

    def drain_instructions(self) -> list[str]:
        """Return and clear pending instructions (the runtime injects them)."""
        if not self.instructions:
            return []
        pending = list(self.instructions)
        self.instructions = []
        return pending


def get_or_create(run_id: str | object) -> RunControl:
    key = str(run_id)
    ctl = _controls.get(key)
    if ctl is None:
        ctl = RunControl(run_id=key)
        _controls[key] = ctl
    return ctl


def get(run_id: str | object) -> Optional[RunControl]:
    return _controls.get(str(run_id))


def drop(run_id: str | object) -> None:
    _controls.pop(str(run_id), None)
