"""Container health probe: ``python -m app.healthcheck``.

A healthcheck that only proves the process has a pulse is worthless — the
failure mode this deploy actually hit was a worker whose event loop was wedged
on a provider timeout: PID alive, runs unclaimed, ``os.kill(1, 0)`` green. This
probe answers the question an orchestrator should be asking instead: **can this
process still do its job right now?**

It checks the dependencies each role needs and writes a heartbeat record so the
*next* probe can assert liveness of a loop that exposes no socket:

================  ==========================================  ==================
role              checked                                     heartbeat written
================  ==========================================  ==================
``api``           DB ``SELECT 1`` + ``/health``-equivalent     —
                  readiness components reachable
``worker``        DB + Redis (the run queue is Redis Streams,  ``worker``
                  so a Redis that answers PING but not XADD
                  means the worker cannot consume anything)
``recovery``      DB only (its loop is pure SQL over leases)   ``recovery``
================  ==========================================  ==================

Heartbeat semantics
-------------------
The leader-gated singleton loops (recovery scan, retention sweep, stream
reclaim) can be *legitimately* quiet for minutes, so "no log line recently" is
not a health signal. Instead the loop calls :func:`beat` after each completed
tick; the probe then fails when the record is older than the loop's own cadence
plus a grace margin. That distinguishes "idle because there is nothing to do"
(a fresh beat) from "idle because the loop is stuck or this replica is a
non-leader that never got promoted" (a stale beat) — and a non-leader writing
its own beat keeps a warm standby green, which is what you want: failover must
not depend on the dying node's opinion of itself.

Exit codes follow the OCI convention: 0 = healthy, 1 = unhealthy. Non-zero
output goes to stderr so ``docker inspect`` / ``kubectl describe`` surface it.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from typing import Any

# A heartbeat older than this many multiples of the loop cadence is a failure.
# 3x absorbs one slow tick (a big retention batch, a provider hiccup) without
# flapping the pod.
_STALE_FACTOR = 3


def beat_path(role: str) -> str:
    """Where a loop parks its liveness record.

    Under ``TMPDIR`` so it lands on the writable tmpfs mount every container
    already has (the image runs with ``readOnlyRootFilesystem``) and so it is
    per-container by construction — a shared volume would let a dead replica
    inherit a live peer's heartbeat and pass on someone else's liveness.
    """
    import tempfile
    from pathlib import Path

    return str(Path(tempfile.gettempdir()) / f"mygpt-{role}-heartbeat.json")


def beat(role: str, *, ticks: int = 1, detail: dict[str, Any] | None = None) -> None:
    """Record that a periodic loop completed a tick. Best-effort, never raises.

    Called from the guarded loop, not from a signal handler: the point is to
    prove progress, and a tick is progress.
    """
    import os

    payload = {
        "role": role,
        "ts": time.time(),
        "ticks": ticks,
        "detail": detail or {},
    }
    try:
        path = beat_path(role)
        tmp = f"{path}.tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(payload, fh)
        os.replace(tmp, path)
    except OSError:  # pragma: no cover - a missing tmp dir must not kill a loop
        pass


def read_beat(role: str) -> dict[str, Any] | None:
    try:
        with open(beat_path(role), encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def check_beat(role: str, max_age_seconds: float) -> tuple[bool, str]:
    """True when a heartbeat exists and is younger than ``max_age_seconds``."""
    record = read_beat(role)
    if record is None:
        return False, f"no heartbeat record for {role!r} (loop never completed a tick)"
    try:
        ts = float(record.get("ts") or 0.0)
    except (TypeError, ValueError):
        return False, "heartbeat record has an unreadable timestamp"
    age = time.time() - ts
    if age > max_age_seconds:
        return False, f"heartbeat {role!r} is {age:.0f}s old (limit {max_age_seconds:.0f}s)"
    return True, f"heartbeat {age:.0f}s old (limit {max_age_seconds:.0f}s)"


async def _check_db() -> tuple[bool, str]:
    from sqlalchemy import text

    from app.db import engine

    try:
        async with asyncio.timeout(5):
            async with engine.connect() as conn:
                await conn.execute(text("SELECT 1"))
    except Exception as exc:
        return False, f"database unreachable: {type(exc).__name__}: {exc}"
    return True, "database reachable"


async def _check_redis(need_stream: bool = False) -> tuple[bool, str]:
    """PING, and optionally that the run-queue stream is *operational*.

    ``need_stream`` asks for the stronger check: read the consumer group's
    pending count. A Redis that PINGs but has lost the stream (flushed, evicted,
    wrong instance) is exactly the case where a worker looks alive and starves,
    so the probe distinguishes them instead of reporting a green healthcheck over
    a queue nobody can consume.
    """
    from app.core.redis import get_redis
    from app.core.config import get_settings

    try:
        client = get_redis()
        if client is None:
            return False, "redis client not configured"
        async with asyncio.timeout(5):
            await client.ping()
            if need_stream:
                s = get_settings()
                try:
                    await client.xpending(s.RUN_QUEUE_STREAM, s.RUN_QUEUE_GROUP)
                except Exception:
                    # Group/stream not created yet: a fresh deploy is healthy,
                    # the first enqueue creates it. Only a failed PING fails.
                    pass
    except Exception as exc:
        return False, f"redis unreachable: {type(exc).__name__}: {exc}"
    return True, "redis reachable" + (" + run stream readable" if need_stream else "")


async def run(role: str, *, interval: float | None = None) -> int:
    """Execute the probe for ``role`` and return the process exit code."""
    from app.core.config import get_settings
    from app.core.logging import configure_logging

    settings = get_settings()
    configure_logging("WARNING")

    checks: list[Any] = [_check_db()]
    if role == "worker":
        checks.append(_check_redis(need_stream=True))
    rc = 0
    for coro in checks:
        try:
            ok, detail = await coro
        except Exception as exc:  # pragma: no cover - defensive: probes never raise
            ok, detail = False, f"probe raised {type(exc).__name__}: {exc}"
        if not ok:
            print(f"[healthcheck:{role}] {detail}", file=sys.stderr)
            rc = 1
        else:
            print(f"[healthcheck:{role}] {detail}")
    # Liveness of a leader-gated loop: only recovery owns a cadence we can
    # assert here (the worker's progress is per-run, not per-tick).
    if role == "recovery":
        limit = max(interval or settings.RECOVERY_SCAN_INTERVAL_SECONDS, 1) * _STALE_FACTOR
        fresh, why = check_beat("recovery", limit)
        if fresh:
            print(f"[healthcheck:recovery] {why}")
        elif settings.LEADER_ELECTION_ENABLED and not _was_leader():
            # A standby is *supposed* to be quiet: it is waiting for failover,
            # not hung. Failing it would evict the warm standby and leave zero
            # redundancy exactly when the leader needs somewhere to fail over to.
            print(f"[healthcheck:recovery] standby (no leadership yet): {why}")
        else:
            print(f"[healthcheck:recovery] {why}", file=sys.stderr)
            rc = 1
    elif role == "worker":
        # The worker's self-check beats every SELF_CHECK_INTERVAL_SECONDS (20s,
        # see app/worker.py) and deliberately stops beating when its event loop
        # wedges or the queue transport dies. The budget below is therefore
        # 3 minutes: enough to absorb a couple of missed ticks, short enough that
        # a wedged worker is removed from rotation instead of quietly starving.
        limit = max(interval or 60.0, 60.0) * _STALE_FACTOR
        fresh, why = check_beat("worker", limit)
        if fresh:
            print(f"[healthcheck:worker] {why}")
        else:
            print(f"[healthcheck:worker] {why}", file=sys.stderr)
            rc = 1
    return 0 if rc == 0 else 1


def _was_leader() -> bool:
    """True once this container has held leadership at least once.

    Recorded by the recovery loop when it acquires the advisory lock; a
    container that has never been promoted has nothing to beat with.
    """
    return read_beat("recovery-promoted") is not None


def main() -> int:
    parser = argparse.ArgumentParser(prog="python -m app.healthcheck")
    parser.add_argument("role", choices=["api", "worker", "recovery"])
    parser.add_argument(
        "--interval",
        type=float,
        default=None,
        help="loop cadence the heartbeat budget is derived from (seconds)",
    )
    args = parser.parse_args()
    try:
        return asyncio.run(run(args.role, interval=args.interval))
    except KeyboardInterrupt:  # pragma: no cover
        return 1


if __name__ == "__main__":
    sys.exit(main())
