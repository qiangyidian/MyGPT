"""RAG-13 生成后校验：返回用户之前的确定性门禁。

设计文档对这一阶段只有一句硬话：**不能把「LLM 自称已核验」当作核验通过**。所以
这里的每一条检查都是确定性的、可复现的、不依赖模型的：

  ``schema`` → ``citation_ids_exist`` → ``authorization_recheck`` → ``pii_policy``
  → ``length`` → （``grounding`` 由 grounding.py 以**仅审计**方式补上）

语义审查（Judge）不在决策链里。原因写在 ``grounding.py``：未校准的 Judge 没有资格
改写用户答案。所以本模块的判定**完全由确定性证据决定**。

三个刻意的设计选择：

**一、按坐标摘除主张，不重建答案。** 摘除走 ``claims.remove_claim_spans``，代码块、
标题、列表符号原样保留。用「把幸存 claim 拼回去」的方式重建会毁掉排版，而排版不是
校验的职责。

**二、降级动作分级，且不可绕过的失败绝不降级。** 文档规定「权限失败、引用不存在、
PII 命中不能通过格式修复绕过」。所以只有 ``schema`` 失败才可能 ``retry_format_once``；
``authorization_recheck`` / ``pii_policy`` 失败要么摘除受影响主张，要么拒答。

**三、fail-closed 的边界是「用户显式要求从知识库找答案」。** 检索为空时是否拒答，
取决于 ``kb_explicit``：用户显式 @ 了知识库就是明确的「从这里面找」，此时答通用知识
是欺骗；会话默认绑定 KB 时按通用助手回答是产品定位，不属于本门禁的范围。高端风险
类别无论是否显式引用都 fail-closed（文档 1.3）。
"""
from __future__ import annotations

import logging
import uuid
from collections.abc import Sequence
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Document, DocumentChunk, KnowledgeBase
from app.rag.citations import sanitize_unbacked_source_markers
from app.rag.claims import build_claims, cited_source_indexes, remove_claim_spans, uncited_claim_ratio
from app.rag.contracts import (
    AnswerResult,
    AnswerStatus,
    Claim,
    ContextItem,
    ValidationCheck,
    ValidationResult,
)
from app.rag.risk import scan_pii

logger = logging.getLogger(__name__)

__all__ = [
    "AuthzVerdict",
    "POLICY_VERSION",
    "PostprocessOutcome",
    "authorization_recheck",
    "gate_answer",
]

POLICY_VERSION = "policy-v1"

#: 控字符白名单：只留换行与制表符。NUL / 退格 / 转义符进答案说明上游有东西坏了。
_ALLOWED_CONTROL = {"\n", "\r", "\t"}


@dataclass(frozen=True)
class AuthzVerdict:
    """一条引用的权限/存在性复核结论。"""

    context_id: str
    chunk_id: str
    ok: bool
    reason: str = ""

    def to_dict(self) -> dict[str, object]:
        return {
            "context_id": self.context_id,
            "chunk_id": self.chunk_id,
            "ok": self.ok,
            "reason": self.reason,
        }


@dataclass(frozen=True)
class PostprocessOutcome:
    """门禁的完整产物。``answer`` 是**可能被降级过**的最终文本。"""

    answer: str
    status: AnswerStatus
    claims: tuple[Claim, ...]
    validation: ValidationResult
    refusal_reason: str | None = None
    authz: tuple[AuthzVerdict, ...] = ()
    #: 被摘掉引用标记的事实（``sanitize_unbacked_source_markers`` 的返回值）。
    markers_stripped: bool = False

    @property
    def dropped_claim_count(self) -> int:
        return sum(1 for c in self.claims if c.dropped)

    def to_metadata(self) -> dict[str, object]:
        """进 message metadata 的精简形式。**不含答案全文**（默认不落原文）。"""
        return {
            "policy_version": self.validation.policy_version,
            "status": self.status,
            "action": self.validation.action,
            "checks": [c.model_dump(mode="json") for c in self.validation.checks],
            "dropped_claim_count": self.dropped_claim_count,
            "uncited_claim_ratio": round(uncited_claim_ratio(self.claims), 4),
            "refusal_reason": self.refusal_reason,
            "authz_failures": [v.to_dict() for v in self.authz if not v.ok],
            "markers_stripped": self.markers_stripped,
        }


# --------------------------------------------------------------------------- #
# 权限 / 存在性复核
# --------------------------------------------------------------------------- #
async def authorization_recheck(
    db: AsyncSession,
    *,
    items: Sequence[ContextItem],
    user_id: uuid.UUID | None,
) -> list[AuthzVerdict]:
    """核实每条被引用的证据「现在」还该被这个用户看到。

    索引、检索、缓存、引用打开都要执行租户/用户授权控制（文档 5.2）。检索时的过滤
    不算数——文档在检索与**用户点开引用**之间可能被删、被重新索引、或换了归属，
    所以生成之后、返回之前必须再查一次。

    在没有 principal/ACL 模型的今天（ACL 属于另一个切片），这次复核落到三件能查的
    事实：分块仍在、内容哈希与检索时一致、它所属知识库仍归调用者。这三条各自对应一
    个真实故障：文档被删、文档被重索引（旧引用指向已不存在的版本）、KB 被转手。
    ``PrincipalResolver`` 的 seam 就在这里——切片 3 换真实现时本函数签名不变。
    """
    if not items:
        return []
    chunk_ids: list[uuid.UUID] = []
    by_chunk: dict[str, list[ContextItem]] = {}
    for item in items:
        try:
            cid = uuid.UUID(str(item.chunk_id))
        except (ValueError, AttributeError, TypeError):
            continue
        chunk_ids.append(cid)
        by_chunk.setdefault(str(cid), []).append(item)

    found: dict[str, tuple[DocumentChunk, Document, KnowledgeBase | None]] = {}
    if chunk_ids:
        rows = (
            await db.execute(
                select(DocumentChunk, Document, KnowledgeBase)
                .join(Document, Document.id == DocumentChunk.document_id)
                .outerjoin(KnowledgeBase, KnowledgeBase.id == Document.knowledge_base_id)
                .where(DocumentChunk.id.in_(chunk_ids))
            )
        ).all()
        for chunk, doc, kb in rows:
            found[str(chunk.id)] = (chunk, doc, kb)

    verdicts: list[AuthzVerdict] = []
    for item in items:
        key = str(item.chunk_id)
        row = found.get(key)
        if row is None:
            verdicts.append(AuthzVerdict(item.context_id, key, False, "evidence_missing"))
            continue
        chunk, doc, kb = row
        if item.content_sha256 and chunk.content_sha256 and chunk.content_sha256 != item.content_sha256:
            # 检索之后分块被重新切过：这份引用指向的版本已经不存在了。
            verdicts.append(AuthzVerdict(item.context_id, key, False, "revision_changed"))
            continue
        if doc.status != "indexed":
            # 重新索引中 / 已失败的文档不该继续被引用。
            verdicts.append(AuthzVerdict(item.context_id, key, False, f"document_{doc.status}"))
            continue
        if user_id is not None and (kb is None or kb.user_id != user_id):
            verdicts.append(AuthzVerdict(item.context_id, key, False, "unauthorized"))
            continue
        verdicts.append(AuthzVerdict(item.context_id, key, True))
    return verdicts


# --------------------------------------------------------------------------- #
# 门禁
# --------------------------------------------------------------------------- #
def _check(
    name: str, status: str, detail: str = ""
) -> ValidationCheck:
    return ValidationCheck(name=name, status=status, detail=detail)  # type: ignore[arg-type]


def _has_bad_control(text: str) -> bool:
    return any(ord(ch) < 32 and ch not in _ALLOWED_CONTROL for ch in text)


async def gate_answer(
    db: AsyncSession | None,
    *,
    answer: str,
    request_id: str,
    trace_id: str,
    source_index: dict[int, ContextItem],
    is_high_risk: bool = False,
    kb_explicit: bool = False,
    retrieval_insufficient: bool = False,
    user_id: uuid.UUID | None = None,
    max_answer_chars: int = 0,
) -> PostprocessOutcome:
    """跑确定性检查链，产出 ``(最终答案, 状态, 检查明细)``。

    ``db`` 可以为 ``None``：附件/网页引用没有 KB 行可查，此时跳过
    ``authorization_recheck`` 并记为 ``skip``（而不是假装通过）。
    """
    checks: list[ValidationCheck] = []
    items = list(source_index.values())
    text = answer or ""

    # ---- 0. 检索为空且用户显式要求从知识库找 → 直接 insufficient_evidence ---------
    # 文档 5.1：ACL 过滤后候选为空时必须回 insufficient_evidence，**不得根据模型常识
    # 补写答案**。这一步在文本为空时才是可判定的；有文本时继续走下面的检查链。
    if retrieval_insufficient and (kb_explicit or is_high_risk) and not text.strip():
        checks.append(_check("schema", "pass"))
        checks.append(_check("citation_ids_exist", "skip", "没有答案文本可校验"))
        checks.append(_check("authorization_recheck", "skip", "没有引用可复核"))
        checks.append(_check("pii_policy", "pass"))
        checks.append(_check("length", "pass"))
        validation = ValidationResult(
            request_id=request_id,
            trace_id=trace_id,
            status="fail",
            checks=checks,
            action="refuse",
            policy_version=POLICY_VERSION,
        )
        return PostprocessOutcome(
            answer="",
            status="insufficient_evidence",
            claims=(),
            validation=validation,
            refusal_reason="insufficient_evidence",
        )

    # ---- 1. schema -----------------------------------------------------------
    claims = build_claims(text, source_index)
    schema_problems: list[str] = []
    if not text.strip():
        schema_problems.append("答案为空")
    if _has_bad_control(text):
        schema_problems.append("含非法控制字符")
    if text.strip() and not claims:
        schema_problems.append("未能切出任何可核验主张")
    checks.append(
        _check("schema", "fail" if schema_problems else "pass", "；".join(schema_problems))
    )
    if schema_problems:
        # 文档：**只允许**对格式错误重试一次；其余失败不得靠格式修复绕过。
        validation = ValidationResult(
            request_id=request_id,
            trace_id=trace_id,
            status="fail",
            checks=checks,
            action="retry_format_once",
            policy_version=POLICY_VERSION,
        )
        return PostprocessOutcome(
            answer=text,
            status="answered",
            claims=tuple(claims),
            validation=validation,
            refusal_reason="schema_invalid",
        )

    # ---- 2. citation_ids_exist ----------------------------------------------
    # 模型可能引用一个本次请求根本没给过它的序号（幻觉引用）。这类主张必须先摘除，
    # 否则用户点开会看到一个不存在的来源。
    fabricated: list[str] = []
    for claim in claims:
        if claim.span is None:
            continue
        bad = [n for n in _markers_in(claim.text) if n not in source_index]
        if bad:
            claim.dropped = True
            claim.drop_reason = "citation_not_in_request"
            fabricated.append(f"{claim.claim_id}:{sorted(bad)}")
    checks.append(
        _check(
            "citation_ids_exist",
            "fail" if fabricated else "pass",
            f"越界引用 {len(fabricated)} 处" if fabricated else "",
        )
    )

    # ---- 3. authorization_recheck -------------------------------------------
    authz: list[AuthzVerdict] = []
    live_with_evidence = [c for c in claims if not c.dropped and c.evidence_context_ids]
    if db is None:
        checks.append(_check("authorization_recheck", "skip", "本次请求没有可复核的来源行"))
    elif not live_with_evidence:
        checks.append(_check("authorization_recheck", "skip", "没有带证据的主张"))
    else:
        wanted = [item for item in items if item.context_id in _evidence_ids(live_with_evidence)]
        authz = await authorization_recheck(db, items=wanted, user_id=user_id)
        bad_by_ctx = {v.context_id: v.reason for v in authz if not v.ok}
        for claim in live_with_evidence:
            reason = next(
                (bad_by_ctx[cid] for cid in claim.evidence_context_ids if cid in bad_by_ctx),
                None,
            )
            if reason:
                claim.dropped = True
                claim.drop_reason = reason
        # 只保留仍然有效的证据 id，摘掉的主张不该再撑着任何引用。
        for claim in claims:
            if claim.dropped:
                continue
            claim.evidence_context_ids = [
                cid
                for cid in claim.evidence_context_ids
                if cid not in bad_by_ctx
            ]
        checks.append(
            _check(
                "authorization_recheck",
                "fail" if bad_by_ctx else "pass",
                f"{len(bad_by_ctx)} 条证据未通过复核（{','.join(sorted(set(bad_by_ctx.values())))})"
                if bad_by_ctx
                else "",
            )
        )

    # ---- 4. pii_policy ------------------------------------------------------
    # 文档：PII 命中不能通过格式修复绕过，只能摘除受影响主张；摘不干净的拒答。
    pii_hits = scan_pii(text)
    if pii_hits:
        for claim in claims:
            if claim.dropped or claim.span is None:
                continue
            if scan_pii(claim.text):
                claim.dropped = True
                claim.drop_reason = "pii"
    checks.append(
        _check(
            "pii_policy",
            "fail" if pii_hits else "pass",
            f"命中 {','.join(pii_hits)}" if pii_hits else "",
        )
    )

    # ---- 5. length ----------------------------------------------------------
    if max_answer_chars > 0 and len(text) > max_answer_chars:
        checks.append(
            _check("length", "fail", f"答案 {len(text)} 字符超过上限 {max_answer_chars}")
        )
    else:
        checks.append(_check("length", "pass" if max_answer_chars > 0 else "skip"))

    # ---- 6. 高风险：必须有站得住的证据 --------------------------------------
    # 这是 claim-to-evidence 的**确定性替身**：判官未校准时唯一能强制的东西。
    surviving_evidence = [
        c for c in claims if not c.dropped and c.evidence_context_ids
    ]
    if is_high_risk:
        ok = bool(surviving_evidence)
        checks.append(
            _check(
                "high_risk_grounding",
                "pass" if ok else "fail",
                "" if ok else "高风险答案没有任何通过复核的证据支撑",
            )
        )
    else:
        checks.append(_check("high_risk_grounding", "skip"))

    # ---- 7. 摘除 + 重写 -----------------------------------------------------
    dropped_ids = [c.claim_id for c in claims if c.dropped]
    rewritten = remove_claim_spans(text, claims) if dropped_ids else text
    # 越界标记也一并剥掉（与 chat_service 的既有清洗同一套规则，只此一处口径）。
    rewritten, markers_stripped = sanitize_unbacked_source_markers(
        rewritten, len(source_index)
    )
    if markers_stripped:
        checks.append(
            _check("citation_ids_exist", "fail", "答案里存在越界 [source N] 标记，已剥离")
        )

    # ---- 8. 动作 -----------------------------------------------------------
    hard_failures = {"high_risk_grounding"}
    if max_answer_chars > 0 and len(text) > max_answer_chars:
        hard_failures.add("length")
    # 有 PII 且重写后仍有 PII（说明它落在主张之外，摘不掉）→ 拒答。
    residue_pii = bool(pii_hits) and bool(scan_pii(rewritten))
    if residue_pii:
        hard_failures.add("pii_policy")

    if hard_failures & set(validation_failed_names(checks)):
        action, status, refusal = "refuse", "refused", sorted(hard_failures)[0]
    elif pii_hits:
        # PII 全落在被摘除的主张里 → 摘干净了，但答案已残缺，标脏让人能看见。
        action, status, refusal = "drop_claims", "answered", None
    elif dropped_ids:
        action, status, refusal = "drop_claims", "answered", None
    else:
        action, status, refusal = "return_answer", "answered", None

    # 摘到一条主张都不剩 → 没有可交付的答案，回 insufficient_evidence 而不是空回答。
    if action in ("drop_claims",) and not any(not c.dropped for c in claims):
        action, status, refusal = "refuse", "insufficient_evidence", "all_claims_dropped"

    validation = ValidationResult(
        request_id=request_id,
        trace_id=trace_id,
        status="fail" if action in ("refuse", "retry_format_once") else "pass",
        checks=checks,
        action=action,  # type: ignore[arg-type]
        dropped_claim_ids=dropped_ids,
        invalid_citation_indexes=sorted(
            {n for c in claims if c.dropped for n in _markers_in(c.text)}
            - set(source_index)
        ),
        policy_version=POLICY_VERSION,
    )
    return PostprocessOutcome(
        answer=rewritten if action != "refuse" else "",
        status=status,  # type: ignore[arg-type]
        claims=tuple(claims),
        validation=validation,
        refusal_reason=refusal,
        authz=tuple(authz),
        markers_stripped=markers_stripped,
    )


def validation_failed_names(checks: Sequence[ValidationCheck]) -> list[str]:
    return [c.name for c in checks if c.status == "fail"]


def _markers_in(text: str) -> list[int]:
    from app.rag.claims import claim_source_indexes

    return claim_source_indexes(text)


def _evidence_ids(claims: Sequence[Claim]) -> set[str]:
    out: set[str] = set()
    for c in claims:
        out.update(c.evidence_context_ids)
    return out


def finalize_answer_result(
    outcome: PostprocessOutcome,
    *,
    request_id: str,
    trace_id: str,
    model: dict[str, object] | None = None,
    prompt_version: str = "",
    usage: dict[str, object] | None = None,
    finish_reason: str = "stop",
) -> AnswerResult:
    """把门禁产物组装成 ``AnswerResult``（RAG-11 的输出契约）。"""
    live = [c for c in outcome.claims if not c.dropped]
    citation_candidates: list[dict[str, object]] = []
    for cid in cited_source_indexes(live):
        claim_ids = [
            c.claim_id for c in live if cid in _markers_in(c.text) and not c.dropped
        ]
        citation_candidates.append({"source_index": cid, "claim_ids": claim_ids})
    return AnswerResult(
        request_id=request_id,
        trace_id=trace_id,
        status=outcome.status,
        answer=outcome.answer,
        claims=live,
        citation_candidates=citation_candidates,
        refusal_reason=outcome.refusal_reason,
        model=dict(model or {}),
        prompt_version=prompt_version,
        usage=dict(usage or {}),
        finish_reason=finish_reason,
    )
