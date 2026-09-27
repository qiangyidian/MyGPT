"""Planner-executor-verifier workflow engine (Task 6).

:class:`WorkflowEngine` orchestrates the durable plan -> execute -> verify ->
(bounded) replan state machine over an arbitrary :class:`~app.agents.workflow.schemas.Plan`.

Execution model
---------------
Each step is scheduled as an asyncio task that:

  1. waits for its dependencies' observations (an ``asyncio.Event`` per step),
  2. acquires a bounded-concurrency semaphore and increments the in-flight
     counter (the engine records the peak -> ``result.max_concurrency``),
  3. runs the injected :class:`~app.agents.workflow.executor.StepExecutor` with
     ONLY its declared dependencies' observations as upstream (parallel siblings
     must never see each other's output — that would fake parallel independence),
     under a per-step wall-clock timeout =
     ``min(step.timeout_seconds, budget remaining)`` when either is known,
  4. retries only on transient errors up to the step's :class:`RetryPolicy`
     (a step timeout is transient, so it re-runs or fails within the budget),
  5. checkpoints its observation so downstream steps (and the verifier) see it.

Cancellation is NOT a step failure: a :class:`asyncio.CancelledError` reaching a
step (user cancel at the ``before_step`` boundary, the outer task being
cancelled, ...) is surfaced through ``on_step_cancel`` and re-raised, so the
caller settles the run as cancelled instead of retrying / failing it.

After every step completes, the :class:`~app.agents.workflow.verifier.Verifier`
inspects the accumulated observations. ``pass`` completes the run; ``fail``
terminates it; ``revise`` consumes one replan unit and the planner produces a
NEW versioned plan that RETAINS completed valid work and reworks only the
flagged steps. Exhausting ``max_replans`` terminates ``failed``.

Persistence (best-effort, optional)
-----------------------------------
When ``run_id`` + ``session_factory`` are provided, each attempt (initial +
each retry) persists an :class:`~app.models.AgentAttempt` row through the
:class:`~app.agents.workflow.attempts.AttemptRepository` and emits durable
``step.started`` / ``step.completed`` / ``step.failed`` / ``plan.revised``
events via :func:`~app.agents.events.append_event_safe`. Persistence NEVER
breaks the run: a DB failure is swallowed (best-effort). When those are
``None`` the engine runs in-memory, which is what makes the core logic fully
unit-testable with stubs.
"""
from __future__ import annotations

import asyncio
import inspect
import logging
import uuid
from typing import Any

from app.agents.events import append_event_safe
from app.agents.schemas import BudgetExceeded
from app.agents.workflow.attempts import AttemptRepository
from app.agents.workflow.checkpoints import (
    StepCheckpointStore,
    apply_checkpoints,
)
from app.agents.workflow.planner import revise_plan, validate_plan
from app.agents.workflow.revision import (
    ACTION_ACCEPT,
    ReplanBudget,
    ReviewTrace,
    ReviewVerdict,
    decide_revision,
)
from app.agents.workflow.schemas import (
    Plan,
    Step,
    StepError,
    StepObservation,
    VerificationVerdict,
    VerifierResult,
    WorkflowResult,
)
from app.agents.workflow.verifier import RuleBasedVerifier, ScriptedVerifier, Verifier
from app.observability import observe_counter, observe_span

logger = logging.getLogger(__name__)


class WorkflowEngine:
    """Drive a :class:`Plan` through execute -> verify -> replan."""

    def __init__(
        self,
        *,
        executor: Any,
        verifier: Verifier | None = None,
        run_id: uuid.UUID | str | None = None,
        session_factory: Any = None,
        max_concurrency: int = 8,
        on_step_start: Any = None,
        on_step_end: Any = None,
        on_step_error: Any = None,
        on_step_retry: Any = None,
        on_step_cancel: Any = None,
        before_step: Any = None,
        budget_remaining_seconds: Any = None,
        replan_units_remaining: Any = None,
        on_replan: Any = None,
    ) -> None:
        self._executor = executor
        self._verifier = verifier
        self._run_id = _opt_uuid(run_id)
        self._session_factory = session_factory
        self._max_concurrency = max(1, int(max_concurrency))
        self._on_step_start = on_step_start
        self._on_step_end = on_step_end
        self._on_step_error = on_step_error
        self._on_step_retry = on_step_retry
        self._on_step_cancel = on_step_cancel
        self._before_step = before_step
        # 预算剩余秒数的取值器（``Callable[[], float | None]``）。引擎不认识
        # BudgetGuard，只用它把每步的挂钟超时压进整轮预算内 —— 否则一个卡死的
        # 步骤可以越过 AGENT_MAX_RUNTIME_SECONDS 一直挂着，而暂停/取消要等它让出
        # 控制权才进得来（before_step 在其之后）。
        self._budget_remaining = budget_remaining_seconds
        # 返修额度（B13）：引擎照样**不认识** BudgetGuard —— 只经这两个回调读
        # 与花额度。``replan_units_remaining`` 返回 ``int | None``（None = 这一
        # 路没有预算闸门，只按计划上限走）；``on_replan`` 在真要返修之前调一次，
        # 由注入方落账（``BudgetGuard.enter_replan()``）。它抛 BudgetExceeded
        # 即预算收口，与计划上限耗尽走同一条终态。
        self._replan_units_remaining = replan_units_remaining
        self._on_replan = on_replan
        # 本轮从检查点复用的步骤 id（跨进程 resume 的成果，供结果与埋点读出）。
        self._reused_steps: list[str] = []

    # ------------------------------------------------------------------ #
    async def run(
        self,
        plan: Plan,
        *,
        verifier: Verifier | None = None,
        verifier_results: list | None = None,
        revise_step_ids: list[str] | None = None,
    ) -> WorkflowResult:
        """Execute ``plan`` to completion (or bounded failure).

        ``verifier_results`` is a convenience that wires a
        :class:`~app.agents.workflow.verifier.ScriptedVerifier` from bare
        verdict strings (``"pass"`` / ``"revise"`` / ``"fail"``) in one call;
        ``revise_step_ids`` names the steps a ``"revise"`` verdict reworks. The
        explicit ``verifier`` kwarg or the constructor verifier is used
        otherwise, falling back to :class:`RuleBasedVerifier`.
        """
        validate_plan(plan)
        verifier_impl = self._resolve_verifier(
            verifier=verifier,
            verifier_results=verifier_results,
            revise_step_ids=revise_step_ids,
        )

        # 跨进程 resume：崩溃/丢租约后同一个 run_id 会被重新入队，这里把上一轮
        # 已经成功落库的步骤产出读回来、标成 skip 并携带进本轮。首轮没有检查点，
        # 读回来是空 dict —— 零副作用。
        self._reused_steps = apply_checkpoints(plan, await self._load_checkpoints())
        if self._reused_steps:
            observe_counter("workflow.steps", len(self._reused_steps), outcome="resumed")
            await self._emit("plan.resumed", {"step_ids": list(self._reused_steps)})

        observations: dict[str, StepObservation] = dict(plan.carry_observations)
        replans = 0
        peak_concurrency = 0
        verifier_history: list[VerifierResult] = []
        # 审阅台账（B13）：verdict + 由它做出的决定，一路记到最后，进
        # WorkflowResult.review_log → 终态事件 → run.output。
        review_trace = ReviewTrace()
        current = plan

        while True:
            step_observations, peak, failed_step = await self._execute_plan(
                current, observations
            )
            observations.update(step_observations)
            peak_concurrency = max(peak_concurrency, peak)

            # An execution failure short-circuits straight to ``failed``.
            if failed_step is not None:
                return WorkflowResult(
                    status="failed",
                    replans=replans,
                    max_concurrency=peak_concurrency,
                    observations=observations,
                    verifier_results=verifier_history,
                    error=f"step {failed_step!r} failed",
                )

            verdict = await verifier_impl.verify(current, observations)
            verifier_history.append(verdict)
            # 审 → 改的**判定**收在纯函数 decide_revision 里（B15 的「一处声明」
            # 同思路）：引擎原先内联的那条 ``replans >= max_replans`` 判据原样
            # 搬过去，另加两条它本来就缺的：预算余量、以及「结论要落账」。
            decision = decide_revision(
                ReviewVerdict.from_verifier_result(verdict, round_index=replans),
                ReplanBudget(
                    plan_max=int(current.max_replans or 0),
                    replans_used=replans,
                    guard_remaining=self._replan_units_left(),
                ),
            )
            review_trace.record(verdict, decision, round_index=replans)

            if verdict.verdict == VerificationVerdict.pass_:
                # 整轮成功 → 检查点没有复用价值了，清掉，免得终态 run 的行了无
                # 人烟地占着审计表（失败/取消路径**不清**，那正是下一轮要用的）。
                await self._clear_checkpoints()
                return WorkflowResult(
                    status="completed",
                    replans=replans,
                    max_concurrency=peak_concurrency,
                    observations=observations,
                    findings=verdict.findings,
                    verifier_results=verifier_history,
                    review_log=review_trace.as_list(),
                    reused_steps=self._reused_steps,
                )
            if verdict.verdict == VerificationVerdict.fail:
                return WorkflowResult(
                    status="failed",
                    replans=replans,
                    max_concurrency=peak_concurrency,
                    observations=observations,
                    findings=verdict.findings,
                    verifier_results=verifier_history,
                    review_log=review_trace.as_list(),
                    error=verdict.note or "verification failed",
                )
            if decision.action == ACTION_ACCEPT:
                # 审阅要求返修却没点名任何步骤：本轮产出按现状收。
                await self._clear_checkpoints()
                return WorkflowResult(
                    status="completed",
                    replans=replans,
                    max_concurrency=peak_concurrency,
                    observations=observations,
                    findings=verdict.findings,
                    verifier_results=verifier_history,
                    review_log=review_trace.as_list(),
                    reused_steps=self._reused_steps,
                )
            if decision.stops:
                # 计划上限耗尽 / 返修预算耗尽 —— 与既有行为同一条终态，只是
                # 理由现在来自判定，且会带着台账一起出去。
                return WorkflowResult(
                    status="failed",
                    replans=replans,
                    max_concurrency=peak_concurrency,
                    observations=observations,
                    findings=verdict.findings,
                    verifier_results=verifier_history,
                    review_log=review_trace.as_list(),
                    error=decision.reason or "replan budget exhausted",
                )
            # Consume one replan unit: produce a NEW versioned plan retaining
            # completed valid work and reworking only the flagged steps.
            #
            # 先花额度再改计划：``on_replan`` 是注入方的账本（BudgetGuard），
            # 花不掉就不该动计划 —— 反过来会让「预算已耗尽」的 run 白改一版
            # plan 才报错。BudgetExceeded 与上限耗尽走同一条 failed 终态。
            if self._on_replan is not None:
                try:
                    await self._call_on_replan(decision)
                except BudgetExceeded as exc:
                    review_trace.entries[-1]["decision"]["budget_blocked"] = True
                    review_trace.entries[-1]["decision"]["reason"] = str(exc)
                    return WorkflowResult(
                        status="failed",
                        replans=replans,
                        max_concurrency=peak_concurrency,
                        observations=observations,
                        findings=verdict.findings,
                        verifier_results=verifier_history,
                        review_log=review_trace.as_list(),
                        error=str(exc),
                    )
            current = revise_plan(
                current,
                revise_step_ids=verdict.revise_step_ids,
                observations=observations,
                findings=decision.findings,
            )
            replans += 1
            observe_counter("workflow.replans", 1, version=current.version)
            await self._emit("plan.revised", {
                "version": current.version,
                "revise_step_ids": list(verdict.revise_step_ids),
                "replan_count": current.replan_count,
                # 结论与决定一起发：面板/trace 上要看得见「是谁说要改、改哪些、
                # 依据是什么」，而不是只有一个版本号跳变。
                "findings": list(decision.findings),
                "decision": decision.as_trace(),
            })

    # ------------------------------------------------------------------ #
    def _replan_units_left(self) -> int | None:
        """预算里的返修余量读数；没注入取值器 = 不设预算闸门。"""
        if self._replan_units_remaining is None:
            return None
        try:
            value = self._replan_units_remaining()
        except Exception:  # pragma: no cover - 读数失败不该打挂执行
            logger.debug("replan units read failed", exc_info=True)
            return None
        return None if value is None else int(value)

    async def _call_on_replan(self, decision: Any) -> None:
        """``on_replan`` 既可能是协程也可能是同步落账函数。"""
        result = self._on_replan(decision.as_trace())
        if inspect.isawaitable(result):
            await result

    # ------------------------------------------------------------------ #
    def _resolve_verifier(
        self,
        *,
        verifier: Verifier | None,
        verifier_results: list | None,
        revise_step_ids: list[str] | None,
    ) -> Verifier:
        if verifier_results is not None:
            return ScriptedVerifier(
                verifier_results, revise_step_ids=list(revise_step_ids or [])
            )
        if verifier is not None:
            return verifier
        if self._verifier is not None:
            return self._verifier
        return RuleBasedVerifier()

    # ------------------------------------------------------------------ #
    async def _execute_plan(
        self,
        plan: Plan,
        seed_observations: dict[str, StepObservation],
    ) -> tuple[dict[str, StepObservation], int, str | None]:
        """Run every step respecting dependencies + bounded concurrency.

        Returns ``(observations, peak_concurrency, failed_step_id)``. When
        ``failed_step_id`` is not None the plan could not complete (a step
        failed permanently / exhausted its retries); the caller maps that to a
        terminal ``failed`` result.
        """
        observations: dict[str, StepObservation] = dict(seed_observations)
        # Carry-over (skip) observations must already be present (seeded from
        # plan.carry_observations by the caller).
        for s in plan.steps:
            if s.skip and s.id not in observations and s.id in plan.carry_observations:
                observations[s.id] = plan.carry_observations[s.id]

        events: dict[str, asyncio.Event] = {s.id: asyncio.Event() for s in plan.steps}
        state = _RunState()
        # Step ids that failed permanently. A downstream step whose dependency
        # failed is NOT ready — it would run with a missing upstream observation
        # and emit a misleading secondary error — so it is short-circuited.
        failed: set[str] = set()

        semaphore = asyncio.Semaphore(self._max_concurrency)

        async def run_step(step: Step) -> tuple[str, StepObservation | None]:
            # Wait for all dependencies to finish (success OR failure).
            for dep in step.dependencies:
                await events[dep].wait()
            if any(dep in failed for dep in step.dependencies):
                # A dependency failed permanently; do not execute this step.
                # (The dep adds itself to ``failed`` before setting its event, so
                # by the time we pass ``wait()`` the membership check is sound.)
                failed.add(step.id)
                events[step.id].set()
                return step.id, None
            if self._before_step is not None:
                # 与 on_step_start 不同：这里**不吞异常**。暂停阻塞与取消
                # 都依赖它能中断执行（CancelledError 必须向上传播）。
                await self._before_step(step.id)
            if step.skip:
                # Already-done retained work: nothing to execute.
                events[step.id].set()
                carried = observations.get(step.id)
                if step.id in self._reused_steps and carried is not None:
                    # 跨进程 resume 复用的步骤：面板上的节点还停在 pending，必须
                    # 补一对 started/completed，否则用户看到的是「这一步没跑」，
                    # 而它的产出其实正在被下游使用。用量照原样入账一次 —— 上一轮
                    # 进程死在结算之前，这些 token 从没进过用户账单。
                    await self._call_hook(self._on_step_start, step.id)
                    await self._call_hook(
                        self._on_step_end, step.id, carried.output, carried.usage
                    )
                return step.id, carried
            obs = await self._run_with_retries(step, observations, semaphore, state)
            if obs is None:
                # Step failed permanently; unblock waiters and surface failure.
                failed.add(step.id)
                events[step.id].set()
                return step.id, None
            observations[step.id] = obs
            events[step.id].set()
            return step.id, obs

        tasks = [asyncio.create_task(run_step(s)) for s in plan.steps]
        failed_step: str | None = None
        try:
            results = await asyncio.gather(*tasks)
            for sid, obs in results:
                if obs is None and sid is not None:
                    failed_step = sid
        except asyncio.CancelledError:
            # 取消不是失败：撤掉所有在途步骤再原样上抛，让调用方按 cancelled 收尾
            # （与 walker 的 gather + cancel  siblings + raise 同构）。gather 不会
            # 自行取消其余子任务，不显式收就会泄漏一批悬挂的 step。
            for t in tasks:
                if not t.done():
                    t.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            raise
        except Exception:  # pragma: no cover - run_step traps its own errors
            # Defensive: cancel anything still in flight.
            for t in tasks:
                t.cancel()
            failed_step = next((s.id for s in plan.steps if not events[s.id].is_set()), None)

        return observations, state.peak, failed_step

    # ------------------------------------------------------------------ #
    async def _run_with_retries(
        self,
        step: Step,
        observations: dict[str, StepObservation],
        semaphore: asyncio.Semaphore,
        state: _RunState,
    ) -> StepObservation | None:
        """Run ``step`` with retry + bounded concurrency + persistence.

        Returns the observation on success, or ``None`` when the step failed
        permanently / exhausted its retries.
        """
        # Observability (Task 11b): one span per step attempt. The span captures
        # the step id + attempt (NOT step input, which may carry prompt text).
        # Inert when exporters are off.
        policy = step.retry_policy
        attempt_number_base = await self._next_attempt_number(step.id)
        attempt = 0
        # 上游 = **仅本步声明的依赖**，不是全部累计观测。并行兄弟（debate 的两个
        # advocate、task_decomposition 的并行 worker）一旦互相看到对方产出，并行
        # 独立性就是假的，且会把 prompt 撑爆。对照 walker 的
        # ``CrewAIRuntime._run_one_stage``（它按 spec.depends_on 拼 context）。
        upstream = _dependency_view(step, observations)
        while True:
            attempt += 1
            attempt_number = attempt_number_base + attempt - 1
            try:
                with observe_span(
                    "workflow.step", step_id=step.id, attempt=attempt_number
                ) as _sp:
                    await self._call_hook(self._on_step_start, step.id)
                    await self._open_attempt(step.id, attempt_number)
                    async with semaphore:
                        state.in_flight += 1
                        state.peak = max(state.peak, state.in_flight)
                        try:
                            obs = await self._execute_within_deadline(step, upstream)
                        finally:
                            state.in_flight -= 1
                    if obs.usage is None:
                        obs.usage = {"attempts": attempt}
                    else:
                        obs.usage = {**dict(obs.usage), "attempts": attempt}
                    obs.attempts = attempt
                    await self._close_attempt(step.id, attempt_number, obs)
                    await self._save_checkpoint(obs)
                    await self._call_hook(
                        self._on_step_end, step.id, obs.output, obs.usage
                    )
                observe_counter("workflow.steps", 1, outcome="done")
                return obs
            except asyncio.CancelledError:
                # 取消（用户取消 / 外层任务被撤 / 步骤被 cancel）既不是失败也不
                # 是 transient 错误：不发 failed 事件、不写 error attempt、不进
                # 重试循环 —— 打一个 cancelled 观测后原样上抛，由调用方把整轮
                # 收成 cancelled。旧实现把它当成「永久失败」，于是引擎返回
                # failed，orchestrator 又把取消当成异常去回退 CrewAI，用户点了
                # 停止反而把整轮重跑一遍。
                await self._call_hook(self._on_step_cancel, step.id)
                raise
            except BaseException as exc:
                transient = policy.is_transient(exc) and attempt <= policy.max_retries
                await self._error_attempt(step.id, attempt_number, exc, transient)
                if transient:
                    observe_counter("workflow.steps", 1, outcome="retry")
                    observe_counter(
                        "workflow.step.retry", 1, step=step.id, attempt=attempt + 1
                    )
                    await self._call_hook(
                        self._on_step_retry, step.id, attempt + 1, str(exc)
                    )
                    continue
                logger.warning(
                    "workflow step %s failed permanently: %s", step.id, exc
                )
                await self._call_hook(self._on_step_error, step.id, str(exc))
                observe_counter("workflow.steps", 1, outcome="failed")
                return None

    # ------------------------------------------------------------------ #
    async def _execute_within_deadline(
        self, step: Step, upstream: dict[str, StepObservation]
    ) -> StepObservation:
        """Run one attempt under ``min(step.timeout_seconds, 预算剩余)`` 挂钟超时。

        超时按 :class:`StepError` 的 transient 语义抛出（可重试），而不是让整轮
        挂在一个不返回的步骤上。``asyncio.timeout`` 通过 cancel 内部 await 点来
        打断，所以取消能一直穿透到 inner executor（真步骤 = CrewAI 的
        ``aexecute_task``）。
        """
        limit = self._step_timeout_seconds(step)
        if limit is None:
            return await self._executor.execute(step, upstream)
        try:
            async with asyncio.timeout(limit):
                return await self._executor.execute(step, upstream)
        except TimeoutError as exc:
            # 消息里带 "timeout"：即使某步的重试策略没声明 StepError，字符串
            # 匹配也能把它认成 transient（模板里的 _TRANSIENT 就含该关键字）。
            raise StepError(
                f"step {step.id!r} timeout after {limit:.1f}s", transient=True
            ) from exc

    # ------------------------------------------------------------------ #
    def _step_timeout_seconds(self, step: Step) -> float | None:
        """本步的挂钟上限；None = 不限时（无预算取值器且步骤没声明超时）。"""
        limits: list[float] = []
        if step.timeout_seconds is not None and step.timeout_seconds > 0:
            limits.append(float(step.timeout_seconds))
        if self._budget_remaining is not None:
            try:
                remaining = float(self._budget_remaining())
            except asyncio.CancelledError:
                raise
            except BaseException:  # 预算读数失败不该打断执行（也不该吞取消）
                logger.debug("budget remaining_seconds failed", exc_info=True)
                remaining = None
            if remaining is not None:
                limits.append(max(remaining, 0.0))
        return min(limits) if limits else None

    # ------------------------------------------------------------------ #
    # 只读步骤回调
    #
    # 引擎的步骤生命周期以回调形式外泄，供 RunEnvironment 发 agent_status /
    # step_output / step_progress。这是**观测**通道：回调不得改变调度、重试或
    # 终止语义，其异常被吞掉并记日志 —— 与引擎 best-effort 的持久化策略一致
    # （观测绝不 veto 执行）。
    # ------------------------------------------------------------------ #
    async def _call_hook(self, hook: Any, *args: Any) -> None:
        if hook is None:
            return
        try:
            result = hook(*args)
            if inspect.isawaitable(result):
                await result
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.debug("workflow step hook failed", exc_info=True)

    # ------------------------------------------------------------------ #
    # Persistence helpers (best-effort, short-lived sessions)
    # ------------------------------------------------------------------ #
    async def _next_attempt_number(self, step_id: str) -> int:
        if not self._persistence_enabled():
            return 1
        try:
            async with self._session_factory() as sess:
                return await AttemptRepository(sess).next_attempt_number(
                    self._run_id, step_id
                )
        except Exception:  # pragma: no cover - best effort
            logger.debug("next_attempt_number failed for %s", step_id, exc_info=True)
            return 1

    async def _open_attempt(self, step_id: str, attempt_number: int) -> None:
        if not self._persistence_enabled():
            return
        try:
            async with self._session_factory() as sess:
                repo = AttemptRepository(sess)
                attempt = await repo.create_pending(
                    self._run_id, step_id, attempt_number=attempt_number
                )
                await repo.mark_running(attempt)
                await append_event_safe(
                    sess, self._run_id, "step.started",
                    {"step_id": step_id, "attempt": attempt_number},
                )
                await sess.commit()
        except Exception:  # pragma: no cover - best effort
            logger.debug("open_attempt failed for %s", step_id, exc_info=True)

    async def _close_attempt(
        self, step_id: str, attempt_number: int, obs: StepObservation
    ) -> None:
        if not self._persistence_enabled():
            return
        try:
            async with self._session_factory() as sess:
                from sqlalchemy import select

                from app.models import AgentAttempt

                repo = AttemptRepository(sess)
                # Re-fetch the running attempt for this step (highest number).
                result = await sess.execute(
                    select(AgentAttempt)
                    .where(
                        AgentAttempt.run_id == self._run_id,
                        AgentAttempt.step_key == step_id,
                    )
                    .order_by(AgentAttempt.attempt_number.desc())
                    .limit(1)
                )
                attempt = result.scalar_one_or_none()
                if attempt is not None and attempt.status == "running":
                    await repo.mark_done(attempt, usage=obs.usage)
                await append_event_safe(
                    sess, self._run_id, "step.completed",
                    {"step_id": step_id, "attempt": attempt_number},
                )
                await sess.commit()
        except Exception:  # pragma: no cover - best effort
            logger.debug("close_attempt failed for %s", step_id, exc_info=True)

    async def _error_attempt(
        self,
        step_id: str,
        attempt_number: int,
        exc: BaseException,
        transient: bool,
    ) -> None:
        if not self._persistence_enabled():
            return
        try:
            async with self._session_factory() as sess:
                from sqlalchemy import select

                from app.models import AgentAttempt

                result = await sess.execute(
                    select(AgentAttempt)
                    .where(
                        AgentAttempt.run_id == self._run_id,
                        AgentAttempt.step_key == step_id,
                    )
                    .order_by(AgentAttempt.attempt_number.desc())
                    .limit(1)
                )
                attempt = result.scalar_one_or_none()
                repo = AttemptRepository(sess)
                if attempt is not None and attempt.status == "running":
                    await repo.mark_error(attempt, str(exc))
                await append_event_safe(
                    sess, self._run_id, "step.failed",
                    {
                        "step_id": step_id,
                        "attempt": attempt_number,
                        "error": str(exc),
                        "transient": transient,
                    },
                )
                await sess.commit()
        except Exception:  # pragma: no cover - best effort
            logger.debug("error_attempt failed for %s", step_id, exc_info=True)

    # ------------------------------------------------------------------ #
    # 步骤检查点（跨进程 resume）
    #
    # 与 attempt 行同一套「短会话 + best-effort」纪律：检查点只是省钱的优化，
    # 任何一次读写失败都不能 veto 执行 —— 读失败等于没有可复用的活，写失败等于
    # 这步下次重跑。
    # ------------------------------------------------------------------ #
    async def _load_checkpoints(self) -> dict[str, StepObservation]:
        if not self._persistence_enabled():
            return {}
        try:
            async with self._session_factory() as sess:
                return await StepCheckpointStore(sess).load(self._run_id)
        except Exception:  # pragma: no cover - best effort
            logger.debug("load step checkpoints failed", exc_info=True)
            return {}

    async def _save_checkpoint(self, obs: StepObservation) -> None:
        if not self._persistence_enabled():
            return
        try:
            async with self._session_factory() as sess:
                await StepCheckpointStore(sess).save(self._run_id, obs)
                await sess.commit()
        except Exception:  # pragma: no cover - best effort
            logger.debug("save step checkpoint failed for %s", obs.step_id, exc_info=True)

    async def _clear_checkpoints(self) -> None:
        if not self._persistence_enabled():
            return
        try:
            async with self._session_factory() as sess:
                await StepCheckpointStore(sess).clear(self._run_id)
                await sess.commit()
        except Exception:  # pragma: no cover - best effort
            logger.debug("clear step checkpoints failed", exc_info=True)

    async def _emit(self, event_type: str, data: dict) -> None:
        if not self._persistence_enabled():
            return
        try:
            async with self._session_factory() as sess:
                await append_event_safe(sess, self._run_id, event_type, data)
                await sess.commit()
        except Exception:  # pragma: no cover - best effort
            logger.debug("emit %s failed", event_type, exc_info=True)

    def _persistence_enabled(self) -> bool:
        return self._run_id is not None and self._session_factory is not None


# --------------------------------------------------------------------------- #
def _dependency_view(
    step: Step, observations: dict[str, StepObservation]
) -> dict[str, StepObservation]:
    """本步的依赖产出（按 ``step.dependencies`` 声明序），不含其它步骤的观测。

    依赖尚未记录观测（例如被撤掉的 skip 步）就跳过；无依赖 → 空 dict，
    与 walker 给首个 stage 传 ``context=None`` 等价。
    """
    return {
        dep: observations[dep]
        for dep in step.dependencies
        if dep in observations
    }


# --------------------------------------------------------------------------- #
class _RunState:
    """Mutable holder for the in-flight counter + observed peak."""

    __slots__ = ("in_flight", "peak")

    def __init__(self) -> None:
        self.in_flight = 0
        self.peak = 0


def _opt_uuid(value: uuid.UUID | str | None) -> uuid.UUID | None:
    if value is None:
        return None
    if isinstance(value, uuid.UUID):
        return value
    try:
        return uuid.UUID(str(value))
    except (ValueError, TypeError):
        return None
