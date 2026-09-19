"""Per-run cooperative controls (Phase 2): pause / resume / instructions.

A lightweight, in-process registry of :class:`RunControl` objects keyed by
run id. The orchestrator creates one at run start; the agent_runs control
endpoints (pause/resume/instructions) mutate it; the runtime honors pause and
reads appended instructions between stages / between streamed tokens.

This is deliberately in-process (matches ``BACKGROUND_WORKER=inprocess`` and the
approval coordinator). For multi-worker deployments a Redis-backed signal
(like approval_bus) would replace it; the Control surface stays the same.

**回收只看「超时」，绝不按 LRU 淘汰活跃 run。** 注册表有软上限，是为了让崩溃
或被丢弃的 run 不漏掉 control。旧实现淘汰「最老的一条」，可以正好淘汰掉
**此刻正在运行**的 run 的 control：用户点暂停/取消时拿不到 control，信号被静默
丢弃（只剩持久命令，要等下一个边界才生效 —— 而卡死的步骤永远到不了那个边界）。
现在只回收很久没人碰的条目（正常 run 在 ``ChatOrchestrator.stream`` 的 finally
里就 ``drop()`` 掉了，只有崩溃/被丢弃的才会留在这里）；宁可临时超过软上限，
也不丢还在跑的信号。
"""
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)

_controls: dict[str, RunControl] = {}
# Soft cap so runs that crash/abort without reaching their drop() cleanup can't
# leak RunControl objects (and their asyncio.Events) into the registry forever.
_MAX_CONTROLS = 256
# 一条 control 多久没人碰就算「那次 run 已经不在跑了」（孤儿回收线）。活跃 run
# 每跨一步、每被前端点一次都会刷新，所以这条线永远碰不到它。
_IDLE_RECLAIM_SECONDS = 1800.0


@dataclass
class RunControl:
    run_id: str
    # SET while the run is user-paused; the runtime awaits while it is set.
    paused: asyncio.Event = field(default_factory=asyncio.Event)
    # SET to request a cooperative cancel (in addition to task cancellation).
    cancel: asyncio.Event = field(default_factory=asyncio.Event)
    # Instructions the user appended mid-run (newest last).
    instructions: list[str] = field(default_factory=list)
    # 用户主动请求计划门禁（「暂停执行」按钮）。默认 False = 计划先行不阻塞。
    gate_requested: bool = False
    # 最近一次被读/写的单调时钟，用于「只回收超时项」。
    last_seen: float = field(default_factory=time.monotonic)

    def touch(self) -> None:
        """刷新活性：有人还在读/写它 = 这次 run 仍然活着。"""
        self.last_seen = time.monotonic()

    @property
    def reclaimable(self) -> bool:
        """可以回收了吗 —— 很久没人碰（= 那次 run 已经不在跑了）。

        正常 run 结束时 ``ChatOrchestrator.stream`` 的 finally 会 ``drop()``
        掉它，所以走到这里的只有崩溃/被丢弃的 run。
        """
        return (time.monotonic() - self.last_seen) > _IDLE_RECLAIM_SECONDS

    def pause(self) -> None:
        self.paused.set()
        self.touch()

    def resume(self) -> None:
        self.paused.clear()
        self.touch()

    def is_paused(self) -> bool:
        return self.paused.is_set()

    def add_instruction(self, instruction: str) -> None:
        self.touch()
        if instruction and instruction not in self.instructions:
            self.instructions.append(instruction)

    def drain_instructions(self) -> list[str]:
        """Return and clear pending instructions (the runtime injects them)."""
        self.touch()
        if not self.instructions:
            return []
        pending = list(self.instructions)
        self.instructions = []
        return pending

    def request_gate(self) -> None:
        """用户请求在下一个 step 边界进入计划门禁。"""
        self.gate_requested = True
        self.touch()

    def clear_gate(self) -> None:
        self.gate_requested = False


def get_or_create(run_id: str | object) -> RunControl:
    key = str(run_id)
    ctl = _controls.get(key)
    if ctl is None:
        # 崩溃/被丢弃的 run 不会调 drop()，所以按「超时」回收，而不是淘汰最老的
        # 一条（那会淘汰正在运行的 run，见模块文档）。
        _reclaim()
        ctl = RunControl(run_id=key)
        _controls[key] = ctl
        return ctl
    # 同一个 run id 重新跑起来（恢复/重放）：刷新活性。
    ctl.touch()
    return ctl


def _reclaim() -> None:
    """腾出槽位：只回收 reclaimable 的条目，全活跃时保持软上限被临时突破。"""
    if len(_controls) < _MAX_CONTROLS:
        return
    for key in [k for k, c in _controls.items() if c.reclaimable]:
        _controls.pop(key, None)
        if len(_controls) < _MAX_CONTROLS:
            return
    logger.warning(
        "run_controls at soft cap (%d) with no reclaimable entry; keeping all "
        "live controls rather than dropping a running run's signals",
        _MAX_CONTROLS,
    )


def get(run_id: str | object) -> RunControl | None:
    ctl = _controls.get(str(run_id))
    if ctl is not None:
        # 控制端点的一次读写 = 这条 run 还在被操作，刷新活性。
        ctl.touch()
    return ctl


def drop(run_id: str | object) -> None:
    _controls.pop(str(run_id), None)
