"""Durable workflow layer (Task 4): transactional repositories for commands and leases.

  * :class:`~app.agents.workflow.repository.CommandStore` — the exactly-once
    control-command queue (pause/resume/cancel/instruction/approve/reject).
  * :class:`~app.agents.workflow.repository.LeaseStore` — run execution leases
    with optimistic fencing (consumed by Task 5).
  * :mod:`~app.agents.workflow.controls` — high-level persist-first writers the
    API layer calls before publishing the live wake-up signal.

The in-memory ``run_controls`` + ``approval_bus`` remain the live-runtime
signal; this package is the durable source of truth beneath them.
"""
from __future__ import annotations
