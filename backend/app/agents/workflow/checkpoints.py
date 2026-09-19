"""跨进程的步骤级检查点（引擎 resume）。

引擎在本地内存里把每步的 :class:`~app.agents.workflow.schemas.StepObservation`
存进 ``observations``，所以**同一次运行内的重规划**从不重跑已完成步骤。但进程
崩了、租约丢了、worker 被回收之后就没有这份内存了：恢复调度会把同一个 ``run_id``
重新入队，worker 从头再跑一遍整轮 —— 已经花掉的钱和已经拿到的结果一起作废。

这里把「已完成的步骤产出」落到 ``agent_steps``（``step_type="plan"`` +
``agent_id=<步骤 id>``），它本来就是逐步产出的审计表，所以**不需要迁移**。引擎
每成功一步就地写一条；新一轮执行开始前把它们读回来，塞进
``plan.carry_observations`` 并把对应步骤标成 ``skip``。

只认**同一 run_id** 的检查点，因此首轮（表里没有行）天然是空操作。
"""
from __future__ import annotations

import logging
import uuid
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.agents.workflow.schemas import StepObservation
from app.models import AgentStep

logger = logging.getLogger(__name__)

#: 检查点写在这张审计表的 step_type 上（网关写的是 llm/tool，互不覆盖）。
CHECKPOINT_STEP_TYPE = "plan"

# 单步产出落库的上限。步产出是给下游当上下文的文本，正常在几 KB 内；超出这个
# 尺寸的更可能是异常回显，宁可丢掉检查点（退化成重跑）也不把审计表撑爆。
_MAX_OUTPUT_CHARS = 200_000


def _as_uuid(value: uuid.UUID | str) -> uuid.UUID:
    return value if isinstance(value, uuid.UUID) else uuid.UUID(str(value))


class StepCheckpointStore:
    """``agent_steps`` 上的步骤检查点读写（只 flush，调用方提交）。"""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def save(
        self,
        run_id: uuid.UUID | str,
        observation: StepObservation,
        *,
        sequence: int = 0,
    ) -> AgentStep | None:
        """写一条 done 检查点。产出超长/空产出返回 ``None``（不写）。"""
        output = observation.output or ""
        if not output or len(output) > _MAX_OUTPUT_CHARS:
            return None
        step = AgentStep(
            run_id=_as_uuid(run_id),
            sequence=sequence,
            step_type=CHECKPOINT_STEP_TYPE,
            agent_name=observation.step_id,
            agent_id=observation.step_id,
            status="done",
            output_redacted={
                "output": output,
                "usage": observation.usage,
                "attempts": observation.attempts,
            },
        )
        self._session.add(step)
        await self._session.flush()
        return step

    async def load(self, run_id: uuid.UUID | str) -> dict[str, StepObservation]:
        """读出本 run 已成功落库的步骤产出（同一步取最后一条）。"""
        uid = _as_uuid(run_id)
        result = await self._session.execute(
            select(
                AgentStep.agent_id,
                AgentStep.output_redacted,
                AgentStep.sequence,
            )
            .where(
                AgentStep.run_id == uid,
                AgentStep.step_type == CHECKPOINT_STEP_TYPE,
                AgentStep.status == "done",
            )
            .order_by(AgentStep.sequence.asc())
        )
        observations: dict[str, StepObservation] = {}
        for agent_id, payload, _seq in result.all():
            if not agent_id or not isinstance(payload, dict):
                continue
            output = payload.get("output")
            if not isinstance(output, str) or not output:
                continue
            observations[agent_id] = StepObservation(
                step_id=agent_id,
                output=output,
                usage=_safe_usage(payload.get("usage")),
                attempts=int(payload.get("attempts") or 1),
                status="done",
            )
        return observations

    async def clear(self, run_id: uuid.UUID | str) -> int:
        """删掉本 run 的检查点（一轮成功收尾后调用，避免留下可被复用的陈旧行）。"""
        uid = _as_uuid(run_id)
        result = await self._session.execute(
            AgentStep.__table__.delete().where(
                AgentStep.run_id == uid,
                AgentStep.step_type == CHECKPOINT_STEP_TYPE,
            )
        )
        return int(getattr(result, "rowcount", 0) or 0)


def _safe_usage(value: Any) -> dict[str, Any] | None:
    return dict(value) if isinstance(value, dict) and value else None


# --------------------------------------------------------------------------- #
def apply_checkpoints(plan: Any, checkpoints: dict[str, StepObservation]) -> list[str]:
    """把检查点灌进 ``plan``：命中的步骤标 ``skip`` 并携带其产出。

    只复用**依赖齐全**的步骤：skip 步的产出是它下游的唯一上游来源，若上游本身
    没有检查点（新一轮换了拓扑 / 上游那步当时没跑成），复用下游就会带着半截上下
    文直接「完成」，下游拿到的还是拼接过的假结果。所以这里按不动点迭代，从叶子
    往回收敛。

    返回被复用的步骤 id（供上层埋点与告知用户「复用了 N 步」）。
    """
    if not checkpoints:
        return []
    declared = {s.id for s in plan.steps}
    reusable = set(checkpoints) & declared
    done: set[str] = {s.id for s in plan.steps if s.skip and s.id in plan.carry_observations}
    carried = dict(plan.carry_observations)
    reused: list[str] = []

    pending = set(reusable)
    while True:
        ready = {
            sid
            for sid in pending
            if all(dep in done for dep in _deps_of(plan, sid))
        }
        if not ready:
            break
        for sid in sorted(ready):
            step = plan.get(sid)
            step.skip = True
            carried[sid] = checkpoints[sid]
            done.add(sid)
            reused.append(sid)
        pending -= ready

    plan.carry_observations = carried
    return reused


def _deps_of(plan: Any, step_id: str) -> list[str]:
    for s in plan.steps:
        if s.id == step_id:
            return list(s.dependencies)
    return []


async def count_checkpoints(
    session_factory: Any, run_id: uuid.UUID | str
) -> int:
    """本 run 已有多少步检查点（供「第几次尝试」与埋点使用）。"""
    try:
        async with session_factory() as sess:
            uid = _as_uuid(run_id)
            return int(
                (
                    await sess.execute(
                        select(func.count())
                        .select_from(AgentStep)
                        .where(
                            AgentStep.run_id == uid,
                            AgentStep.step_type == CHECKPOINT_STEP_TYPE,
                            AgentStep.status == "done",
                        )
                    )
                ).scalar_one()
            )
    except Exception:  # pragma: no cover - 埋点绝不 veto 执行
        logger.debug("step checkpoint count failed", exc_info=True)
        return 0
