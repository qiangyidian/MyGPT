"""RAG-15 的 Trace 契约：一次请求的**全阶段**关联记录。

设计文档对这一层的约束是「默认不记录原始用户 Query、答案或文档正文，只记录阶段 ID、
耗时、版本、数量、错误和脱敏摘要」。这不是一句建议，是数据治理要求：Trace 的留存
期（30 天）比消息短，且 Trace 会被导出到观测平台，正文一旦进去就再也收不回来。

所以这个模块在**类型层面**就只允许记数字、版本号、枚举和计数字典——没有给正文留
字段。想塞正文进去只能改这里的契约，那是一次需要评审的动作。

它与 ``app/observability.py`` 的分工：那边是进程级的 OTel span/Prometheus 指标（运维
看），这边是**绑定到这一条 assistant 消息**的、要落库供日后归因与评测对齐的记录
（产品与算法看）。两边共用同一个 ``trace_id``。
"""
from __future__ import annotations

import time
from contextlib import contextmanager
from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import Any

__all__ = ["PIPELINE_VERSION", "StageTimer", "TraceSummary", "build_trace_summary"]

#: 整条在线链路的版本号。离线评测与线上 Trace 必须用同一个号才能对齐（文档 1.2）。
PIPELINE_VERSION = "rag-online-v1"

#: 允许出现在 ``stage_timings_ms`` 里的阶段名。闭集，防止各调用点自己起名字导致
#: 同一个阶段在不同回合里叫两个名字、聚合指标直接作废。
STAGES: tuple[str, ...] = (
    "query_plan",
    "retrieval",
    "rerank",
    "context_pack",
    "generation",
    "validation",
    "grounding_audit",
    "total",
)


class StageTimer:
    """累计各阶段耗时（毫秒）。

    用 ``time.monotonic`` 而不是 wall clock：跨阶段累加不该被系统时间调整影响。
    """

    def __init__(self) -> None:
        self._timings: dict[str, int] = {}
        self._started: float | None = None

    def start(self) -> None:
        if self._started is None:
            self._started = time.monotonic()

    @contextmanager
    def stage(self, name: str) -> Iterator[None]:
        """给一个阶段计时，累加到同名阶段上（同名会被多次进出）。"""
        if name not in STAGES:  # pragma: no cover - 开发期哨兵
            raise ValueError(f"unknown trace stage {name!r}; add it to STAGES first")
        started = time.monotonic()
        try:
            yield
        finally:
            elapsed = int((time.monotonic() - started) * 1000)
            self._timings[name] = self._timings.get(name, 0) + elapsed

    def record(self, name: str, ms: int) -> None:
        """外部已测好的耗时（例如生成阶段由编排器自己测的）。"""
        if name not in STAGES:  # pragma: no cover
            raise ValueError(f"unknown trace stage {name!r}; add it to STAGES first")
        self._timings[name] = self._timings.get(name, 0) + max(0, int(ms))

    def timings(self) -> dict[str, int]:
        out = dict(self._timings)
        if self._started is not None:
            out["total"] = int((time.monotonic() - self._started) * 1000)
        return {k: v for k, v in out.items() if v >= 0}


@dataclass
class TraceSummary:
    """落进 message metadata 的 Trace 摘要。

    字段刻意保守：全是 ID、版本、计数、耗时、枚举。**没有任何一处能放 Query 或答案
    正文**——见模块 docstring。
    """

    trace_id: str
    pipeline_version: str = PIPELINE_VERSION
    index_version: str = ""
    prompt_version: str = ""
    model: str = ""
    stage_timings_ms: dict[str, int] = field(default_factory=dict)
    #: 召回 / 去重 / 上下文 / 引用 的数量，供「结果变差时定位到哪一阶段」。
    retrieval_summary: dict[str, Any] = field(default_factory=dict)
    query_summary: dict[str, Any] = field(default_factory=dict)
    #: 回退与重试的事实。丢了这个，「为什么这轮变慢了/变差了」无从归因。
    fallbacks: list[str] = field(default_factory=list)
    retries: int = 0
    outcome: str = "answered"
    risk: dict[str, Any] = field(default_factory=dict)
    validation: dict[str, Any] = field(default_factory=dict)
    #: 恒为 True：本模块的契约决定了摘要里不可能有正文，这个字段是给审计看的断言。
    pii_redacted: bool = True

    def to_metadata(self) -> dict[str, Any]:
        return {
            "trace_id": self.trace_id,
            "pipeline_version": self.pipeline_version,
            "index_version": self.index_version,
            "prompt_version": self.prompt_version,
            "model": self.model,
            "stage_timings_ms": dict(self.stage_timings_ms),
            "retrieval_summary": dict(self.retrieval_summary),
            "query_summary": dict(self.query_summary),
            "fallbacks": list(self.fallbacks),
            "retries": self.retries,
            "outcome": self.outcome,
            "risk": dict(self.risk),
            "validation": dict(self.validation),
            "pii_redacted": self.pii_redacted,
        }

    def to_log_fields(self) -> dict[str, Any]:
        """一行结构化日志用的扁平字段（不嵌套，便于日志系统直接索引）。"""
        fields: dict[str, Any] = {
            "trace_id": self.trace_id,
            "pipeline_version": self.pipeline_version,
            "outcome": self.outcome,
            "retries": self.retries,
        }
        for stage, ms in self.stage_timings_ms.items():
            fields[f"t_{stage}_ms"] = ms
        if self.fallbacks:
            fields["fallbacks"] = ",".join(self.fallbacks)
        return fields


def build_trace_summary(
    *,
    trace_id: str,
    timings: dict[str, int] | None = None,
    snapshot: Any = None,
    risk: Any = None,
    validation: Any = None,
    outcome: str = "answered",
    model: str = "",
    prompt_version: str = "",
) -> TraceSummary:
    """从检索快照 / 风险判定 / 校验结果组装摘要。

    入参用 ``Any`` 并逐项取属性，是为了让本模块不反向依赖 contracts —— 它要被
    chat_service、评测 runner 和调试面板共用，绑死构造方会让任何一方改动都牵动全部。
    """
    summary = TraceSummary(
        trace_id=trace_id,
        stage_timings_ms=dict(timings or {}),
        outcome=outcome,
        model=model,
        prompt_version=prompt_version,
    )
    if snapshot is not None:
        plan = getattr(snapshot, "query_plan", None)
        candidates = getattr(snapshot, "candidate_set", None)
        pack = getattr(snapshot, "context_pack", None)
        summary.index_version = str(getattr(candidates, "index_version", "") or "")
        if plan is not None:
            summary.query_summary = {
                "intent": getattr(plan, "intent", ""),
                "language": getattr(plan, "language", ""),
                "plan_version": getattr(plan, "plan_version", ""),
                "variant_count": len(getattr(plan, "query_variants", []) or []),
                "entity_count": len(getattr(plan, "entities", []) or []),
                "applied_filters": len(getattr(plan, "filters", []) or []),
                "rejected_filters": len(getattr(plan, "rejected_filters", []) or []),
                "requires_exact_match": bool(getattr(plan, "requires_exact_match", False)),
            }
        if candidates is not None:
            summary.retrieval_summary = {
                "retriever_names": list(getattr(candidates, "retrievers", []) or []),
                "candidate_count": int(getattr(candidates, "candidate_count", 0) or 0),
                "selected_count": len(getattr(pack, "items", []) or []) if pack is not None else 0,
                "dropped_count": len(getattr(pack, "dropped", []) or []) if pack is not None else 0,
                "latency_ms": int(getattr(candidates, "latency_ms", 0) or 0),
            }
            summary.fallbacks = list(getattr(candidates, "fallbacks", []) or [])
    if risk is not None:
        summary.risk = (
            risk.to_metadata() if hasattr(risk, "to_metadata") else dict(risk)
        )
    if validation is not None:
        summary.validation = (
            validation.to_metadata() if hasattr(validation, "to_metadata") else dict(validation)
        )
    return summary
