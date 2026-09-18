"""RunEnvironment：一次 run 的共享执行环境。

两个 walker —— :class:`~app.agents.runtime.crewai_runtime.CrewAIRuntime` 的
静态 stage walker 与 :class:`~app.agents.workflow.engine.WorkflowEngine` 的
DAG 调度器 —— 共用本类作为唯一入口。

它**持有**而非继承已有的 :class:`~app.agents.stage_context.StageContext` 与
:class:`~app.agents.lifecycle.AgentLifecycleEmitter`；那两个类保持原样。
本类的职责只是收归「谁在什么时候调它们」——在此之前，这套装配逻辑内联在
``CrewAIRuntime._run_multi_agent`` 里，导致引擎路径只能拿到一个裸的
StageContext，从而缺失审批桥、流式字段、工具归属与 usage 归集。

命名说明：``app/agents/environments.py`` 是 Codex 风格的 workspace 环境
（cwd / shell / ready 状态），与本类无关。
"""
from __future__ import annotations

import asyncio
import logging
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from app.agents.graph import AgentGraph
from app.agents.lifecycle import AgentLifecycleEmitter
from app.agents.schemas import AgentTurnContext
from app.agents.stage_context import StageContext, make_stage_context
from app.db import AsyncSessionLocal

logger = logging.getLogger(__name__)


@dataclass
class RunEnvironment:
    """一次 run 的共享执行环境。"""

    run_id: str
    ctx: AgentTurnContext
    stage_ctx: StageContext
    guard: Any = None
    _emitter: AgentLifecycleEmitter | None = field(default=None, repr=False)

    # ------------------------------------------------------------------ #
    @classmethod
    def for_turn(cls, ctx: AgentTurnContext) -> RunEnvironment:
        """装配 stage_ctx。此时 graph 尚未构建（它依赖 tools，tools 依赖
        stage_ctx），emitter 与审批桥由 :meth:`attach_graph` 建。"""
        from app.agents.runtime.crewai_runtime import _guard_for_context

        guard = _guard_for_context(ctx)
        stage_ctx = make_stage_context(ctx.run_id, budget_guard=guard)

        # 流式 writer 字段：writer stage 直接调 provider 并增量改写助手消息。
        # 全部 Optional；对非 writer stage 与 fake/demo 无副作用。
        stage_ctx.provider = cls._build_provider(ctx)
        stage_ctx.model_config = ctx.model_config
        stage_ctx.assistant_msg = ctx.assistant_msg
        stage_ctx.user_content = ctx.user_content
        stage_ctx.cancel_event = asyncio.Event()
        stage_ctx.db = ctx.db
        stage_ctx.persistence_session_factory = (
            ctx.extra.get("persistence_session_factory") or AsyncSessionLocal
        )
        stage_ctx.persistence_lock = ctx.extra.get("persistence_lock")
        stage_ctx.persist_continuation_checkpoint = cls._resolve_checkpoint(ctx)
        return cls(run_id=ctx.run_id, ctx=ctx, stage_ctx=stage_ctx, guard=guard)

    # ------------------------------------------------------------------ #
    @staticmethod
    def _build_provider(ctx: AgentTurnContext) -> Any:
        """构建流式 writer 用的 provider，容忍测试替身的旧签名。

        会话 id 同时充当 provider 的 session identity（OpenCode 网关要求每会话
        稳定；Hermes 用它划定服务端记忆范围）。与 native runtime 同约。
        """
        from app.providers.registry import get_provider_for_config

        try:
            return get_provider_for_config(
                ctx.model_config, session_id=str(ctx.conversation.id)
            )
        except TypeError:
            # 注入的测试替身可能仍是单参签名。
            try:
                return get_provider_for_config(ctx.model_config)
            except Exception as exc:
                logger.warning(
                    "could not build provider for streaming writer: %s", exc
                )
                return None
        except Exception as exc:
            logger.warning("could not build provider for streaming writer: %s", exc)
            return None

    # ------------------------------------------------------------------ #
    @staticmethod
    def _resolve_checkpoint(ctx: AgentTurnContext):
        """注入优先；否则装好回退实现（长回答续写检查点）。"""
        injected = ctx.extra.get("persist_continuation_checkpoint")
        if callable(injected):
            return injected

        async def _fallback(checkpoint: dict[str, Any]) -> None:
            from app.services.chat_service import _persist_continuation_checkpoint

            session_factory = (
                ctx.extra.get("persistence_session_factory") or AsyncSessionLocal
            )
            await _persist_continuation_checkpoint(
                session_factory, ctx.assistant_msg, ctx.run_id, checkpoint
            )

        return _fallback

    # ------------------------------------------------------------------ #
    # 装配
    # ------------------------------------------------------------------ #
    def attach_graph(self, graph: AgentGraph) -> AgentLifecycleEmitter:
        """建 emitter + 审批桥。审批桥依赖 emitter（等待态由它发），故在此时建。"""
        from app.agents.approval_bridge import ApprovalBridge

        graph.run_id = self.run_id
        emitter = AgentLifecycleEmitter(
            run_id=self.ctx.run_id, graph=graph, stage_ctx=self.stage_ctx
        )
        bridge = ApprovalBridge(
            loop=self.stage_ctx.loop,
            stage_ctx=self.stage_ctx,
            emitter=emitter,
            run_id=self.ctx.run_id,
        )
        self.stage_ctx.approval_bridge = bridge
        self._emitter = emitter
        return emitter

    @property
    def emitter(self) -> AgentLifecycleEmitter:
        if self._emitter is None:
            raise RuntimeError(
                "RunEnvironment.attach_graph() must be called before emitter is used"
            )
        return self._emitter

    # ------------------------------------------------------------------ #
    # 生命周期
    # ------------------------------------------------------------------ #
    def begin(self) -> None:
        """发出 agent_graph + run_status(running)。"""
        self.emitter.emit_graph_initialized()
        self.emitter.emit_run_status("running")

    def step_started(self, step_id: str, *, title: str | None = None) -> bool:
        return self.emitter.emit_agent_started(step_id, task_title=title)

    def step_completed(
        self,
        step_id: str,
        *,
        output: str | None = None,
        output_summary: str | None = None,
        usage: dict | None = None,
        usage_charged: bool = False,
    ) -> None:
        """记完成。usage 的计费只在此处发生一次（usage_charged 表示调用方
        已实时计费，不得重复累加）。"""
        cost: float | None = usage.get("cost_usd") if usage else None
        if usage and self.guard is not None and not usage_charged:
            if cost is None and self.stage_ctx.model_config is not None:
                from app.core.pricing import usage_cost

                cost = usage_cost(
                    getattr(self.stage_ctx.model_config, "model_name", None), usage
                )
            self.guard.add_usage(
                usage, cost_usd=cost, usage_id=f"crewai:stage:{step_id}"
            )
            self.guard.check()
        self.emitter.emit_agent_completed(
            step_id,
            output_summary=output_summary or None,
            usage=usage,
            cost_usd=cost,
        )

    def step_failed(self, step_id: str, *, error: str) -> None:
        self.emitter.emit_agent_failed(step_id, error=error)
        self.emitter.cancel_downstream(step_id)

    def step_cancelled(self, step_id: str) -> None:
        self.emitter.emit_agent_cancelled(step_id)

    def finish(self, status: str) -> None:
        self.emitter.emit_run_status(status)

    # ------------------------------------------------------------------ #
    # 持久化与归集
    # ------------------------------------------------------------------ #
    async def persist_graph(self, *, definition: bool) -> None:
        """把图快照写到 AgentRun 行（best-effort）。"""
        from app.agents.db_mutation import db_mutation_scope
        from app.agents.persistence import persist_graph_snapshot

        session_factory = (
            self.ctx.extra.get("persistence_session_factory") or AsyncSessionLocal
        )
        try:
            async with db_mutation_scope(self.ctx.extra.get("persistence_lock")):
                await persist_graph_snapshot(
                    session_factory,
                    run_id=self.ctx.run_id,
                    snapshot=self.emitter.snapshot(),
                    definition=definition,
                )
        except BaseException as exc:
            if isinstance(exc, Exception):
                logger.warning("failed to persist agent graph snapshot", exc_info=True)
                return
            raise

    def aggregate_usage(self, results: Mapping[str, Any]) -> dict | None:
        from app.agents.continuation import aggregate_usage

        rounds: list[dict[str, Any] | None] = []
        charged_prefixes: list[str] = []
        for step_id, result in results.items():
            if result is None:
                continue
            if getattr(result, "usage", None):
                rounds.append(result.usage)
                if getattr(result, "usage_charged", False):
                    charged_prefixes.append(f"model:{step_id}:")
            elif isinstance(getattr(result, "structured", None), dict):
                rounds.append(result.structured.get("usage"))
        rounds.extend(
            usage
            for key, usage in self.stage_ctx.usage_records.items()
            if not any(key.startswith(p) for p in charged_prefixes)
        )
        return aggregate_usage(rounds)
