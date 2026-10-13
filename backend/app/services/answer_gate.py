"""回合级的答案门禁：风险分级 → 检索带契约 → 生成 → 校验 → 降级/拒答。

这是设计文档 RAG-13 的**执行位置**问题的答案。文档要求「引用 ID 不属于当前请求、
ACL 失败或 PII 命中 → 删除受影响主张并重新校验，否则拒答」，而本产品的生成是流式的
—— 吐出去的 token 收不回来。于是分成两档：

  * **高风险回合（文档 1.3 的七类）**：缓冲。中间事件（步骤、工具卡片）照常实时转发，
    只有**答案正文**被扣住；生成结束后跑门禁，通过了才整体吐出，不通过就换成拒答。
    这样「拒答」才真的成立：用户从没看到那条没有依据的答案。
  * **普通回合**：继续流式。门禁照跑，但只能在事后纠正 —— ``validation`` 帧会把经
    校验的文本交给前端覆盖渲染，并说明摘掉了什么。

档位由 ``rag.risk.classify_risk`` 决定，它宁可误杀（文档：「分类器不确定时按高风险
处理」）；``RAG_HIGH_RISK_BUFFER`` 是它唯一的开关。

**为什么这一层在 ``services/`` 而不是 ``rag/``**：它要同时改 message metadata、发 SSE
帧、决定持久化内容——这些是编排层的职责。``rag/`` 里的模块保持纯粹的阶段计算，没有
任何一个知道「消息」和「事件」的存在。
"""
from __future__ import annotations

import logging
import random
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.agents.schemas import AgentEvent, ev_validation
from app.core.config import get_settings
from app.model_capabilities import capabilities_from_config
from app.rag.budget import BudgetBreakdown, compute_context_budget
from app.rag.contracts import AnswerStatus, ContextItem, ValidationResult, new_trace_id
from app.rag.grounding import GroundingAudit, audit_grounding, should_audit
from app.rag.postprocess import PostprocessOutcome, gate_answer
from app.rag.rag_service import RetrievalOutcome, rag_service
from app.rag.risk import RiskAssessment, classify_risk, extra_terms_from_settings
from app.rag.trace import StageTimer
from app.schemas import Citation

logger = logging.getLogger(__name__)

__all__ = [
    "GateResult",
    "TurnGate",
    "REFUSAL_INSUFFICIENT_EVIDENCE",
    "REFUSAL_HIGH_RISK",
    "context_budget_for",
    "open_turn_gate",
    "retrieve_for_gate",
    "stream_gated",
]

# 拒答文案。用户看到的是**为什么**而不是一片空白，而且不承诺「再试一次就好」——
# 检索为空时重试同一个问题不会有不同结果。
REFUSAL_INSUFFICIENT_EVIDENCE = (
    "根据当前知识库内容无法确定。\n\n"
    "可以试着补充更具体的线索（文档名、章节、错误码、时间范围），"
    "或者把相关文件直接发给我。"
)
REFUSAL_HIGH_RISK = (
    "这类问题需要逐条核对依据（合规、财务、权限、安全等），"
    "而当前检索到的资料不足以支撑一个可核验的回答，因此我不作答复。\n\n"
    "建议查阅原始文件，或转由对应的负责人确认。"
)

#: 答案格式的 prompt/schema 版本，写进 trace。改 prompt 必须改它，否则「换了 prompt
#: 之后质量变了」这件事在数据里不可见（文档 RAG-10 要求 prompt 可追踪）。
PROMPT_VERSION = "rag-answer-v1"


@dataclass
class GateResult:
    """门禁的最终结论（``TurnGate.finalize`` 的产物）。"""

    answer: str
    status: AnswerStatus
    outcome: PostprocessOutcome
    refused: bool = False
    grounding: GroundingAudit | None = None
    corrected_text: str | None = None
    message: str = ""

    @property
    def validation(self) -> ValidationResult:
        return self.outcome.validation

    def event(self, *, trace_id: str, risk: RiskAssessment) -> AgentEvent:
        return ev_validation(
            trace_id=trace_id,
            status=self.status,
            action=self.validation.action,
            checks=[c.model_dump(mode="json") for c in self.validation.checks],
            message=self.message,
            corrected_text=self.corrected_text,
            dropped_claim_count=self.outcome.dropped_claim_count,
            refusal_reason=self.outcome.refusal_reason,
            risk=risk.to_metadata(),
        )

    def metadata(self, *, trace_id: str, risk: RiskAssessment) -> dict[str, Any]:
        """并进 assistant message metadata 的那部分（不含答案全文）。"""
        out: dict[str, Any] = {
            "trace_id": trace_id,
            "answer_status": self.status,
            "validation": self.outcome.to_metadata(),
            "risk": risk.to_metadata(),
        }
        if self.grounding is not None:
            out["grounding_audit"] = self.grounding.to_metadata()
        return out


@dataclass
class TurnGate:
    """一个回合的门禁状态。``open_turn_gate`` 造它，``stream_gated`` 用它的档位。"""

    trace_id: str
    risk: RiskAssessment
    kb_explicit: bool
    buffered: bool
    cfg: Any = None
    timer: StageTimer = field(default_factory=StageTimer)
    #: 审计抽样是否命中。在开门时定一次，回合内不再变（否则同一回合的判定会漂移）。
    grounding_sampled: bool = False
    retrieval: RetrievalOutcome | None = None
    budget: BudgetBreakdown | None = None
    result: GateResult | None = None
    #: 检索是否为空/弱证据 —— 决定「要不要在生成前就拒答」。
    retrieval_insufficient: bool = True
    rag_attempted: bool = False

    # ---- 生成前 --------------------------------------------------------- #
    @property
    def source_index(self) -> dict[int, ContextItem]:
        if self.retrieval is None:
            return {}
        return self.retrieval.source_index

    @property
    def citations(self) -> list[Citation]:
        return list(self.retrieval.citations) if self.retrieval is not None else []

    @property
    def context_text(self) -> str:
        return self.retrieval.context_text if self.retrieval is not None else ""

    def pre_generation_refusal(self) -> str | None:
        """生成前就该拒答吗？返回拒答文案，或 ``None`` 表示照常生成。

        文档 5.1：ACL 过滤后候选为空时必须回 ``insufficient_evidence``，**不得根据
        模型常识补写答案**。但这条只在用户**显式要求从知识库找答案**时生效
        （``kb_explicit``）：那是明确的「从这里面找」，此时答通用知识是欺骗。会话默认
        绑定知识库、用户没有显式引用时，按通用助手回答是产品定位，不归本门禁管。
        高风险类别无论是否显式引用都 fail-closed（文档 1.3）。
        """
        if not self.rag_attempted or not self.retrieval_insufficient:
            return None
        if self.kb_explicit:
            return REFUSAL_INSUFFICIENT_EVIDENCE
        if self.risk.is_high_risk:
            return REFUSAL_HIGH_RISK
        return None

    # ---- 生成后 --------------------------------------------------------- #
    async def finalize(
        self,
        db: AsyncSession | None,
        *,
        answer: str,
        finish_reason: str = "stop",
        usage: dict[str, Any] | None = None,
        user_id: uuid.UUID | str | None = None,
        provider: Any = None,
    ) -> GateResult:
        """跑门禁。**只读答案、只写结论**；返回的 ``answer`` 可能是降级版本。"""
        settings = get_settings()
        max_chars = int(getattr(settings, "RAG_MAX_ANSWER_CHARS", 0) or 0)
        with self.timer.stage("validation"):
            outcome = await gate_answer(
                db,
                answer=answer,
                # 请求 id 与 trace id 在没有独立请求标识时同源；两者都进 trace，
                # 所以不能一个为空。
                request_id=self.trace_id,
                trace_id=self.trace_id,
                source_index=self.source_index,
                is_high_risk=self.risk.is_high_risk,
                kb_explicit=self.kb_explicit,
                retrieval_insufficient=self.retrieval_insufficient,
                user_id=uuid.UUID(str(user_id)) if user_id else None,
                max_answer_chars=max_chars,
            )

        grounding = await self._maybe_audit(outcome, provider)
        if grounding is not None:
            # 审计结论只是**替换掉 checks 里那一条**，绝不参与 action 决策 ——
            # 未校准的判官没有资格改写答案（见 rag/grounding.py 的 AUDIT_ONLY）。
            outcome.validation.checks = [
                *[c for c in outcome.validation.checks if c.name != "grounding"],
                grounding.check,
            ]

        refused = outcome.status in ("refused", "insufficient_evidence")
        message = ""
        corrected: str | None = None
        if outcome.status == "insufficient_evidence":
            message = REFUSAL_INSUFFICIENT_EVIDENCE
        elif refused:
            message = REFUSAL_HIGH_RISK
        elif outcome.dropped_claim_count:
            message = (
                f"有 {outcome.dropped_claim_count} 条主张没有通过依据核查，已从回答中移除。"
            )
            # 低风险回合是流式的，用户已经看到了原文 —— 把这个版本交给前端覆盖渲染，
            # 否则界面显示的和库里存的会不一致（刷新一下就变）。
            corrected = outcome.answer

        # 拒答时把文案当作答案返回：用户需要知道「为什么没有答案」，而不是一片空白。
        display = message if refused else outcome.answer
        return GateResult(
            answer=display,
            status=outcome.status,
            outcome=outcome,
            refused=refused,
            grounding=grounding,
            corrected_text=corrected,
            message=message,
        )

    async def _maybe_audit(
        self, outcome: PostprocessOutcome, provider: Any
    ) -> GroundingAudit | None:
        settings = get_settings()
        if str(getattr(settings, "RAG_GROUNDING_AUDIT", "off")).lower() != "audit":
            return None
        if provider is None:
            return None
        live = [c for c in outcome.claims if not c.dropped]
        if not should_audit(
            is_high_risk=self.risk.is_high_risk,
            claim_count=len(live),
            sample_ratio=float(getattr(settings, "RAG_GROUNDING_SAMPLE_RATIO", 0.0) or 0.0),
            sampled=self.grounding_sampled,
        ):
            return None
        with self.timer.stage("grounding_audit"):
            try:
                return await audit_grounding(
                    provider,
                    claims=list(outcome.claims),
                    context_pack=list(self.source_index.values()),
                )
            except Exception as exc:  # pragma: no cover - 审计绝不阻断回答
                logger.warning("grounding audit skipped: %s", exc)
                return None


async def open_turn_gate(
    *,
    question: str,
    cfg: Any,
    kb_ids: list[Any],
    kb_explicit: bool = False,
    trace_id: str | None = None,
    sampled: bool | None = None,
) -> TurnGate:
    """开门：定档位、定 trace id、定审计抽样。

    定档必须在**生成之前**（否则缓冲没有意义），抽样必须在**同一时刻**定死
    （生成后再抽会让同一回合的判定漂移）。``sampled`` 可注入，便于把抽检规则钉死在
    单测里而不是靠概率碰运气。
    """
    settings = get_settings()
    risk = classify_risk(question, extra_terms=extra_terms_from_settings(settings))
    return TurnGate(
        trace_id=trace_id or new_trace_id(),
        risk=risk,
        kb_explicit=bool(kb_explicit),
        # 缓冲只在「真的会去检索」时才开：没有知识库的回合本来就没有引用要校验，
        # 扣住答案只是白白增加首字延迟。
        buffered=bool(settings.RAG_HIGH_RISK_BUFFER) and risk.is_high_risk and bool(kb_ids),
        cfg=cfg,
        grounding_sampled=(
            sampled
            if sampled is not None
            else random.random()
            < float(getattr(settings, "RAG_GROUNDING_SAMPLE_RATIO", 0.0) or 0.0)
        ),
    )


def context_budget_for(
    cfg: Any, *, reserved_system: int, reserved_query: int
) -> BudgetBreakdown:
    """按**生成模型**的窗口算本轮上下文预算。

    预算取决于生成模型，而 ``rag_service`` 解析的是知识库的**嵌入**模型 —— 两者不是
    同一个模型，所以预算只能在这里算好传进去（见 ``rag.budget``）。
    """
    settings = get_settings()
    caps = capabilities_from_config(cfg)
    ceiling = int(getattr(settings, "RAG_CONTEXT_TOKENS", 0) or 0)
    return compute_context_budget(
        context_window=getattr(caps, "context_window", None),
        reserved_system=reserved_system,
        reserved_query=reserved_query,
        reserved_output=int(getattr(caps, "max_output_tokens", 0) or 0),
        absolute_fallback=ceiling,
        # 窗口是「能不能」，运算是「付不付得起」——两者独立，取小。
        hard_ceiling=ceiling,
    )


async def retrieve_for_gate(
    gate: TurnGate,
    db: AsyncSession,
    *,
    question: str,
    kb_ids: list[Any],
    document_ids: list[Any] | None,
    user_id: uuid.UUID | str | None,
    top_k: int | None = None,
    reserved_system: int = 0,
    reserved_query: int = 0,
) -> RetrievalOutcome:
    """带 trace 检索，并把阶段耗时与结果记到门上。"""
    if gate.cfg is not None:
        gate.budget = context_budget_for(
            gate.cfg, reserved_system=reserved_system, reserved_query=reserved_query
        )
    with gate.timer.stage("retrieval"):
        outcome = await rag_service.retrieve_with_trace(
            db,
            question,
            kb_ids,
            top_k=top_k,
            document_ids=document_ids,
            trace_id=gate.trace_id,
            user_id=user_id,
            context_budget_tokens=gate.budget.budget if gate.budget else None,
        )
    gate.retrieval = outcome
    gate.rag_attempted = True
    pack = outcome.trace.snapshot.context_pack
    gate.retrieval_insufficient = pack.insufficient_evidence or not pack.items
    return outcome


async def stream_gated(
    events: AsyncIterator[AgentEvent],
    *,
    gate: TurnGate | None,
    finalize: Callable[[str, str, dict[str, Any]], Awaitable[GateResult | None]],
) -> AsyncIterator[AgentEvent]:
    """包住生成事件流，按档位决定答案正文什么时候出去。

    * **缓冲档**：扣住 ``token`` 帧，其余（步骤、工具、引用）照常转发——用户仍然看到
      进度，只是答案要等校验完。校验通过就整体吐出；不通过就换成拒答文案。
    * **流式档**：原样转发；``done`` 之前插入一帧 ``validation``，前端据此覆盖渲染
      经校验的文本。

    两档都**始终累计**观察到的正文，这样 ``finalize`` 拿到的就是模型真正产出的东西，
    而不是调用方另行维护的第二份（两份必然漂移）。

    ``validation`` 帧一定在 ``done`` **之前**发出：``done`` 是终态，终态之后再纠正就得
    靠第二次渲染，而前端 reducer 的终态闸门只让一条消息提交一次。
    """
    collected: list[str] = []
    buffering = gate is not None and gate.buffered
    async for evt in events:
        if evt.kind == "token":
            delta = str(evt.data.get("delta") or "")
            collected.append(delta)
            if buffering:
                continue
            yield evt
            continue
        if evt.kind != "done":
            yield evt
            continue

        result = await finalize(
            "".join(collected),
            str(evt.data.get("finish_reason") or "stop"),
            dict(evt.data.get("usage") or {}),
        )
        if result is not None and gate is not None:
            gate.result = result
            yield result.event(trace_id=gate.trace_id, risk=gate.risk)
            if buffering or result.refused:
                # ``replace``：门禁改写过的正文以**整段**形式交回，不是增量。缓冲档下
                # 这一帧是用户第一次看到答案；拒答档下它要顶掉已经流出去的原文。少了这
                # 个标记，前端会把「校验结论里那句 message」和这一帧**追加**到一起，
                # 同一段话显示两遍。
                yield AgentEvent(
                    kind="token", data={"delta": result.answer, "replace": True}
                )
        yield evt
        return
