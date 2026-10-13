"""在线 RAG 各阶段的强类型契约（设计文档 RAG-06 / 07 / 09 / 11 / 13）。

此前这条链路上流动的全是裸 ``list`` 和裸 ``str``：检索出的命中一路被 in-place
改写（重排原地覆盖 ``rerank_score`` 再重排，重排前后名次就此不可回看），上下文
是一个拼好的字符串，引用是一个 pydantic 对象，而「这轮到底做了什么」只散落在
几条 log 里。结果是设计文档要求的每一个判定——过滤条件是否越权、引用 ID 是否属于
**本次**请求、预算用掉多少、哪一步拒答——都没有一个地方能查到。

这里定义阶段之间传递的对象。三条硬约束：

1. **每个对象都带 ``trace_id`` / ``created_at`` / 各自的 ``*_version``**，因为离线
   评测与线上 Trace 必须能用同一套 ID 和版本号对齐（文档 1.2「可度量」）。
2. **原始 Query 永远保留**（``QueryPlan.original_query``），改写只进 ``query_variants``。
3. **不可信数据不进指令位**：这些对象里的 ``text`` 全部来自文档正文，消费者必须
   当数据处理，不得当指令执行。

对象刻意保持「可 JSON 化」：它们要同时进 SSE 事件、进 message metadata、进结构化
日志，任何一处需要 ``model_dump(mode="json")`` 都得能跑通。
"""
from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

__all__ = [
    "AnswerStatus",
    "AnswerResult",
    "CandidateSet",
    "Candidate",
    "Claim",
    "ContextItem",
    "ContextPack",
    "DroppedItem",
    "Entity",
    "QueryFilter",
    "QueryPlan",
    "RejectedFilter",
    "RetrievalTrace",
    "RetrievalSnapshot",
    "ValidationAction",
    "ValidationCheck",
    "ValidationResult",
    "new_trace_id",
]


def new_trace_id() -> str:
    """一次用户请求的 trace id（文档 RAG-15：每请求一个 trace）。"""
    return "tr_" + uuid.uuid4().hex


def _now() -> datetime:
    return datetime.now(UTC)


class _Contract(BaseModel):
    """所有阶段契约的共同底座：允许额外字段，序列化时保留 None。"""

    model_config = ConfigDict(extra="forbid")


# --------------------------------------------------------------------------- #
# RAG-06 查询理解
# --------------------------------------------------------------------------- #
class Entity(BaseModel):
    """从 Query 里确定性抽出的实体。``type`` 是闭集，便于下游按类型分派。"""

    model_config = ConfigDict(extra="forbid")

    type: Literal[
        "error_code", "product_model", "system", "date_range", "quoted_phrase", "number"
    ]
    value: str
    #: 原文里的出现位置，用于「为什么抽到了它」的可解释性；抽不到时 None。
    span: tuple[int, int] | None = None


class QueryFilter(BaseModel):
    """一个**将被下推**的过滤条件。只允许白名单字段与白名单操作符。"""

    model_config = ConfigDict(extra="forbid")

    field: str
    op: Literal["eq", "in", "gt", "gte", "lt", "lte", "between"]
    value: Any = None


class RejectedFilter(BaseModel):
    """一个**被拒绝**的过滤条件，连同拒绝原因。

    这不是调试残渣：文档 1.3 把「过滤器越权尝试数」列为 RAG-06 的质量指标，而
    ``reason`` 正是下一轮该补哪一列 metadata 的输入（``field_not_indexed`` 出现
    得最多，就说明该给那个字段建索引，而不是放宽白名单）。
    """

    model_config = ConfigDict(extra="forbid")

    field: str | None = None
    op: str | None = None
    value: Any = None
    #: 用户原话里的触发片段，便于人工归因；不含整段 Query。
    raw_text: str = ""
    reason: Literal[
        "field_not_in_schema",   # 模型/规则想过滤一个 schema 里没有的字段
        "field_not_indexed",      # 字段在 schema 里，但索引层还查不了（今天只有 document_id 能下推）
        "op_not_allowed",         # 该字段不允许这个操作符
        "unparseable_value",      # 值不是合法值（如 2025-13-45）
        "no_grounded_value",      # 值无法由原问题确定 —— 不推测
    ]


class QueryPlan(BaseModel):
    """RAG-06 的输出：一份可解释、可回放的查询计划。

    本期 ``query_variants`` 恒为空列表：文档 1.3 规定扩展必须先在 EvalSet 上证明
    「对应类别 Recall@30 相对基线提升 ≥5 个百分点且改写保真抽检 ≥95%」，两条都还
    没有数据，所以改写是关闭的。字段留着，是为了开启时不必改契约。
    """

    model_config = ConfigDict(extra="forbid")

    request_id: str
    trace_id: str
    original_query: str
    normalized_query: str
    #: 仅当某类别 Recall@30 未达标且归因到术语错配时才会非空（本期恒空）。
    query_variants: list[str] = Field(default_factory=list)
    intent: Literal["lookup", "explain", "compare", "cause_analysis", "no_answer_candidate"] = "explain"
    entities: list[Entity] = Field(default_factory=list)
    filters: list[QueryFilter] = Field(default_factory=list)
    rejected_filters: list[RejectedFilter] = Field(default_factory=list)
    language: Literal["zh-CN", "en", "mixed", "unknown"] = "unknown"
    #: 纯编号/错误码查询必须同时走词项检索（文档 RAG-06 安全与回退）。
    requires_exact_match: bool = False
    plan_version: str = "query-plan-v1"
    created_at: datetime = Field(default_factory=_now)

    def filter_map(self) -> dict[str, Any]:
        """下推给检索器的 ``{field: value}``。多值字段折叠成列表。"""
        out: dict[str, Any] = {}
        for f in self.filters:
            if f.field in out:
                existing = out[f.field]
                merged = list(existing) if isinstance(existing, list) else [existing]
                merged.extend(f.value if isinstance(f.value, list) else [f.value])
                out[f.field] = merged
            else:
                out[f.field] = f.value
        return out


# --------------------------------------------------------------------------- #
# RAG-07 多路召回
# --------------------------------------------------------------------------- #
class Candidate(BaseModel):
    """一路召回给出的一个候选，连同它在这一路里的原始名次与原始分。

    保留 ``raw_score``／``rank`` 而不是只留融合分，是文档 7「排序要求」的硬要求：
    只有融合分时，无法判断某一路是否在做负贡献。
    """

    model_config = ConfigDict(extra="forbid")

    chunk_id: str
    retriever: Literal["dense", "keyword", "document_inventory", "sql", "graph"] | str
    rank: int
    raw_score: float
    score_type: str = "cosine"
    #: tenant + 授权范围 + 过滤条件的稳定哈希。缓存键与「两路是否真的同范围」都靠它。
    filter_hash: str = ""


class CandidateSet(BaseModel):
    """RAG-07 的输出。``candidate_count`` 是去重后的数量。"""

    model_config = ConfigDict(extra="forbid")

    request_id: str
    trace_id: str
    index_version: str = ""
    retrievers: list[str] = Field(default_factory=list)
    candidates: list[Candidate] = Field(default_factory=list)
    fusion: dict[str, Any] = Field(default_factory=dict)
    candidate_count: int = 0
    latency_ms: int = 0
    #: 单路失败/超时后回退的事实，必须留痕，否则「结果变差」无从归因。
    fallbacks: list[str] = Field(default_factory=list)
    created_at: datetime = Field(default_factory=_now)


# --------------------------------------------------------------------------- #
# RAG-09 上下文构建
# --------------------------------------------------------------------------- #
class ContextItem(BaseModel):
    """Context Pack 里的一项。``context_id`` 是**本次请求内**的引用锚点。

    模型只被允许引用 ``context_id``（映射到 ``[source N]`` 供人读），引用校验也
    只认「这个 id 属于本次请求」——这是 RAG-12「不接受模型自由生成 URL/页码」的
    落地方式。
    """

    model_config = ConfigDict(extra="forbid")

    context_id: str
    chunk_id: str
    text: str
    document_id: str | None = None
    document_name: str = ""
    source_spans: list[dict[str, Any]] = Field(default_factory=list)
    selection_reason: Literal[
        "high_rerank_score", "fused_rank", "complementary_source", "neighbor_expansion"
    ] = "fused_rank"
    score: float = 0.0
    #: 索引时算出的内容哈希。引用校验用它判断「这块内容在检索之后被重新索引过」。
    content_sha256: str | None = None


class DroppedItem(BaseModel):
    """被丢弃的候选及原因。丢弃必须可解释，否则「为什么没引用那份文档」查不出来。"""

    model_config = ConfigDict(extra="forbid")

    chunk_id: str
    reason: Literal[
        "duplicate_or_budget", "below_threshold", "per_document_cap", "per_kb_cap", "empty_text"
    ]


class ContextPack(BaseModel):
    """RAG-09 的输出：真的进了 Prompt 的那批证据。"""

    model_config = ConfigDict(extra="forbid")

    request_id: str
    trace_id: str
    budget_tokens: int = 0
    used_tokens: int = 0
    items: list[ContextItem] = Field(default_factory=list)
    dropped: list[DroppedItem] = Field(default_factory=list)
    #: 检索为空 / 证据不足时置位。下游据此回 insufficient_evidence，而不是让模型
    #: 用常识补写（文档 5.1「空检索/弱证据」）。
    insufficient_evidence: bool = False
    created_at: datetime = Field(default_factory=_now)

    def by_context_id(self) -> dict[str, ContextItem]:
        return {item.context_id: item for item in self.items}


# --------------------------------------------------------------------------- #
# RAG-11 答案生成
# --------------------------------------------------------------------------- #
class Claim(BaseModel):
    """答案里的一个可核验主张。

    粒度是**句子**，不是语义原子。这是刻意的取舍：让模型吐出结构化 claim 会杀掉
    流式（用户要等完整 JSON 才看到第一个字）、多一次模型调用，而且模型可以编造
    claim 结构本身。服务端切句 + 把句内的 ``[source N]`` 归属给该句，是本期唯一
    能同时满足「保流式」和「claim 可核验」的路子。代价写在契约里，不假装它更细。
    """

    model_config = ConfigDict(extra="forbid")

    claim_id: str
    text: str
    evidence_context_ids: list[str] = Field(default_factory=list)
    #: 该主张在**原始答案文本**里的字符区间 ``[start, end)``。
    #: 有了它，摘除主张才是按坐标切除而不是 ``str.replace`` —— 后者遇到重复句会删错
    #: 位置，而这里删错位置意味着用户看到的答案和校验结论对不上。
    span: tuple[int, int] | None = None
    #: 校验未通过时被摘除；被摘除的主张不会再出现在最终答案里。
    dropped: bool = False
    drop_reason: str | None = None


AnswerStatus = Literal[
    "answered",                 # 有依据地回答了
    "insufficient_evidence",    # 检索为空/证据不足 —— 不得用模型常识补写
    "clarification_needed",     # 关键信息缺失（指代无法绑定、时间范围不完整）
    "refused",                  # 安全门禁拒绝
]


class AnswerResult(BaseModel):
    """RAG-11 的输出。

    注意：这个对象由**服务端在生成结束后派生**（从最终文本 + ContextPack +
    usage + finish_reason），不是模型吐出来的。让模型输出它就要牺牲流式，而
    「流式」是这个产品不可让渡的体验；服务端派生的代价是 claim 粒度只能到句子。
    """

    model_config = ConfigDict(extra="forbid")

    request_id: str
    trace_id: str
    status: AnswerStatus = "answered"
    answer: str = ""
    claims: list[Claim] = Field(default_factory=list)
    #: 模型引用的 context_id → 关联哪几个 claim。
    citation_candidates: list[dict[str, Any]] = Field(default_factory=list)
    refusal_reason: str | None = None
    model: dict[str, Any] = Field(default_factory=dict)
    prompt_version: str = ""
    usage: dict[str, Any] = Field(default_factory=dict)
    finish_reason: str = "stop"
    created_at: datetime = Field(default_factory=_now)


# --------------------------------------------------------------------------- #
# RAG-13 生成后校验
# --------------------------------------------------------------------------- #
ValidationAction = Literal[
    "return_answer",   # 全部通过 → 原样返回
    "drop_claims",     # 摘掉不支持的主张后返回（答案降级为 partial）
    "refuse",          # 拒答
    "retry_format_once",  # **仅** JSON 语法错可重试一次
]


class ValidationCheck(BaseModel):
    """确定性检查链里的一项。``detail`` 不放大段原文，只放坐标与计数。"""

    model_config = ConfigDict(extra="forbid")

    name: Literal[
        "schema",
        "citation_ids_exist",
        "authorization_recheck",
        "pii_policy",
        "length",
        "grounding",
        "high_risk_grounding",
    ]
    status: Literal["pass", "fail", "skip", "audit"]
    detail: str = ""


class ValidationResult(BaseModel):
    """RAG-13 的输出。"""

    model_config = ConfigDict(extra="forbid")

    request_id: str
    trace_id: str
    status: Literal["pass", "fail"] = "pass"
    checks: list[ValidationCheck] = Field(default_factory=list)
    action: ValidationAction = "return_answer"
    #: 需要从答案里摘除的主张 id。
    dropped_claim_ids: list[str] = Field(default_factory=list)
    #: 按主张归属出的、权限或存在性校验未过的 citation 序号（1-based，对应 [source N]）。
    invalid_citation_indexes: list[int] = Field(default_factory=list)
    policy_version: str = "policy-v1"
    created_at: datetime = Field(default_factory=_now)

    def check(self, name: str) -> ValidationCheck | None:
        return next((c for c in self.checks if c.name == name), None)

    def failed_names(self) -> list[str]:
        return [c.name for c in self.checks if c.status == "fail"]


# --------------------------------------------------------------------------- #
# RAG-15 Trace
# --------------------------------------------------------------------------- #
class RetrievalSnapshot(BaseModel):
    """一次检索的完整留痕，供 trace / 评测 / 调试面板共用同一份事实。"""

    model_config = ConfigDict(extra="forbid")

    trace_id: str
    query_plan: QueryPlan
    candidate_set: CandidateSet
    context_pack: ContextPack
    #: 传给生成阶段的成稿上下文（已含系统前言）。
    context_text: str = ""
    latency_ms: int = 0


class RetrievalTrace(BaseModel):
    """``RagService.retrieve_with_trace`` 的返回值。"""

    model_config = ConfigDict(extra="forbid")

    snapshot: RetrievalSnapshot
    #: ``[source N]`` 序号 → ContextItem，生成后校验的「引用 ID 属于本次请求」判定表。
    source_index: dict[int, ContextItem] = Field(default_factory=dict)
