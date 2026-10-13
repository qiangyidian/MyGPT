"""生成后确定性门禁的验收测试（RAG-13）。

这是整条链路上唯一会在返回用户**之前**改写或拦下答案的地方，所以测试要覆盖两件事：

  * 该拦的拦住了（幻觉引用、无权限证据、内容已被重新索引、PII、高风险无依据）；
  * 不该动的一个字都没动（干净的答案原样返回，且**不重建 markdown**）。

每个「摘除」用例都必须同时断言「答案里真的没有那句话了」和「其余部分一字未改」——
只断言前者的话，一个把整段答案清空的实现也能过。
"""
from __future__ import annotations

import uuid


from app.models import Document, DocumentChunk, KnowledgeBase
from app.rag.contracts import ContextItem
from app.rag.postprocess import gate_answer

SEEDED_USER = uuid.UUID("00000000-0000-0000-0000-000000000001")
OTHER_USER = uuid.UUID("00000000-0000-0000-0000-0000000000ff")

async def _seed_chunk(
    db,
    *,
    owner=SEEDED_USER,
    content="E1001 表示网络不可达。",
    sha="sha-a",
    status="indexed",
):
    kb = KnowledgeBase(user_id=owner, name=f"kb-{uuid.uuid4().hex[:6]}")
    db.add(kb)
    await db.flush()
    doc = Document(
        knowledge_base_id=kb.id,
        filename="手册.md",
        file_path="/tmp/x.md",
        file_type=".md",
        status=status,
    )
    db.add(doc)
    await db.flush()
    chunk = DocumentChunk(
        document_id=doc.id,
        knowledge_base_id=kb.id,
        chunk_index=0,
        content=content,
        token_count=10,
        metadata_={},
        content_sha256=sha,
    )
    db.add(chunk)
    await db.commit()
    return kb, doc, chunk


def _item(chunk, *, context_id="ctx-1", sha="sha-a"):
    return ContextItem(
        context_id=context_id,
        chunk_id=str(chunk.id),
        text=chunk.content,
        document_id=str(chunk.document_id),
        document_name="手册.md",
        content_sha256=sha,
    )


async def _gate(db, answer, source_index, **over):
    kwargs = dict(
        request_id="req-1",
        trace_id="tr-1",
        source_index=source_index,
        user_id=SEEDED_USER,
        is_high_risk=False,
        kb_explicit=False,
        retrieval_insufficient=False,
        max_answer_chars=0,
    )
    kwargs.update(over)
    return await gate_answer(db, answer=answer, **kwargs)


# --------------------------------------------------------------------------- #
# 干净答案一动不动
# --------------------------------------------------------------------------- #
async def test_a_clean_answer_is_returned_untouched(db_session):
    _kb, _doc, chunk = await _seed_chunk(db_session)
    answer = "## 故障处理\n\nE1001 表示网络不可达 [source 1]。\n\n- 检查网线\n"
    out = await _gate(db_session, answer, {1: _item(chunk)})

    assert out.validation.action == "return_answer"
    assert out.status == "answered"
    assert out.answer == answer
    assert out.dropped_claim_count == 0
    assert all(c.status in ("pass", "skip") for c in out.validation.checks)


async def test_the_validation_result_reports_every_check(db_session):
    _kb, _doc, chunk = await _seed_chunk(db_session)
    out = await _gate(db_session, "结论 [source 1]。", {1: _item(chunk)})
    names = {c.name for c in out.validation.checks}
    assert {
        "schema",
        "citation_ids_exist",
        "authorization_recheck",
        "pii_policy",
        "length",
        "high_risk_grounding",
    } <= names


# --------------------------------------------------------------------------- #
# 引用不属于本次请求
# --------------------------------------------------------------------------- #
async def test_a_fabricated_citation_is_dropped_and_the_rest_survives(db_session):
    _kb, _doc, chunk = await _seed_chunk(db_session)
    answer = "有依据的结论 [source 1]。凭空捏造的结论 [source 9]。"
    out = await _gate(db_session, answer, {1: _item(chunk)})

    assert out.validation.action == "drop_claims"
    assert out.validation.check("citation_ids_exist").status == "fail"
    assert "凭空捏造" not in out.answer
    assert "有依据的结论" in out.answer


async def test_an_out_of_range_marker_left_after_dropping_is_stripped(db_session):
    _kb, _doc, chunk = await _seed_chunk(db_session)
    out = await _gate(db_session, "保留 [source 1]。删掉 [source 7]。", {1: _item(chunk)})
    assert "[source 7]" not in out.answer


async def test_every_claim_fabricated_means_there_is_nothing_to_say(db_session):
    _kb, _doc, chunk = await _seed_chunk(db_session)
    out = await _gate(db_session, "全是编的 [source 9]。", {1: _item(chunk)})
    assert out.status == "insufficient_evidence"
    assert out.answer == ""
    assert out.refusal_reason == "all_claims_dropped"


# --------------------------------------------------------------------------- #
# 权限 / 存在性复核
# --------------------------------------------------------------------------- #
async def test_evidence_from_another_users_kb_is_dropped(db_session):
    _kb, _doc, chunk = await _seed_chunk(db_session, owner=OTHER_USER)
    answer = "别人的资料 [source 1]。没有引用的普通结论。"
    out = await _gate(db_session, answer, {1: _item(chunk)})

    assert out.validation.check("authorization_recheck").status == "fail"
    assert "别人的资料" not in out.answer
    assert out.authz[0].reason == "unauthorized"


async def test_a_deleted_chunk_is_no_longer_evidence(db_session):
    _kb, _doc, chunk = await _seed_chunk(db_session)
    item = _item(chunk)
    await db_session.delete(chunk)
    await db_session.commit()

    out = await _gate(db_session, "引用已删的块 [source 1]。留在原地的一句。", {1: item})
    assert "引用已删的块" not in out.answer
    assert out.authz[0].reason == "evidence_missing"


async def test_a_reindexed_chunk_invalidates_the_old_citation(db_session):
    """内容哈希对不上 = 这份引用指向的版本已经不存在了。"""
    _kb, _doc, chunk = await _seed_chunk(db_session, sha="sha-new")
    out = await _gate(db_session, "引用旧版本 [source 1]。另一句。", {1: _item(chunk, sha="sha-old")})
    assert "引用旧版本" not in out.answer
    assert out.authz[0].reason == "revision_changed"


async def test_a_document_still_reindexing_cannot_back_a_citation(db_session):
    _kb, _doc, chunk = await _seed_chunk(db_session, status="embedding")
    out = await _gate(db_session, "引用半成品 [source 1]。另一句。", {1: _item(chunk)})
    assert "引用半成品" not in out.answer
    assert out.authz[0].reason == "document_embedding"


async def test_authz_leaves_healthy_evidence_alone(db_session):
    _kb, _doc, chunk = await _seed_chunk(db_session)
    out = await _gate(db_session, "正常结论 [source 1]。", {1: _item(chunk)})
    assert out.validation.check("authorization_recheck").status == "pass"
    assert out.authz[0].ok is True


async def test_without_a_db_the_check_is_skipped_not_faked(db_session):
    """附件/网页引用没有 KB 行可查 —— 记 ``skip``，不是假装通过。"""
    out = await _gate(None, "网页来源 [source 1]。", {})
    assert out.validation.check("authorization_recheck").status == "skip"


# --------------------------------------------------------------------------- #
# PII
# --------------------------------------------------------------------------- #
async def test_pii_inside_a_claim_removes_that_claim(db_session):
    _kb, _doc, chunk = await _seed_chunk(db_session)
    answer = "身份证号是 110101199003071234 [source 1]。其余结论正常。"
    out = await _gate(db_session, answer, {1: _item(chunk)})

    assert out.validation.check("pii_policy").status == "fail"
    assert "110101199003071234" not in out.answer
    assert "其余结论正常" in out.answer


async def test_pii_outside_any_claim_refuses_instead_of_leaking_it(db_session):
    """落在代码块里的 PII 摘不掉（代码块不是主张）→ 只能拒答。

    文档要求 PII 命中不能靠格式修复绕过，所以这里不允许「原样返回 + 记一笔」。
    """
    _kb, _doc, chunk = await _seed_chunk(db_session)
    answer = "配置如下：\n\n```\ncard=6222021234567890123\n```\n\n其余结论正常。"
    out = await _gate(db_session, answer, {1: _item(chunk)})
    assert out.status == "refused"
    assert out.answer == ""
    assert "pii_policy" in out.validation.failed_names()


# --------------------------------------------------------------------------- #
# 结构
# --------------------------------------------------------------------------- #
async def test_an_empty_answer_is_a_format_failure_worth_one_retry(db_session):
    out = await _gate(db_session, "   ", {})
    assert out.validation.action == "retry_format_once"
    assert out.validation.check("schema").status == "fail"


async def test_control_characters_are_a_format_failure(db_session):
    out = await _gate(db_session, "正常句子\x00还有内容。", {})
    assert out.validation.check("schema").status == "fail"


async def test_an_overlong_answer_blocks_rather_than_truncating(db_session):
    """静默截断和「生成被截断」在用户眼里没有区别，而后者文档要求当失败处理。"""
    out = await _gate(db_session, "很长的答案。", {}, max_answer_chars=2)
    assert out.status == "refused"
    assert "length" in out.validation.failed_names()


# --------------------------------------------------------------------------- #
# 高风险
# --------------------------------------------------------------------------- #
async def test_a_high_risk_answer_without_evidence_is_refused(db_session):
    out = await _gate(db_session, "我认为这个合同条款没问题。", {}, is_high_risk=True)
    assert out.status == "refused"
    assert out.answer == ""
    assert "high_risk_grounding" in out.validation.failed_names()


async def test_a_high_risk_answer_with_sound_evidence_passes(db_session):
    _kb, _doc, chunk = await _seed_chunk(db_session)
    out = await _gate(
        db_session, "合同违约金按日万分之五计算 [source 1]。", {1: _item(chunk)},
        is_high_risk=True,
    )
    assert out.status == "answered"
    assert out.validation.check("high_risk_grounding").status == "pass"


async def test_high_risk_grounding_is_skipped_for_ordinary_turns(db_session):
    _kb, _doc, chunk = await _seed_chunk(db_session)
    out = await _gate(db_session, "普通结论 [source 1]。", {1: _item(chunk)})
    assert out.validation.check("high_risk_grounding").status == "skip"


# --------------------------------------------------------------------------- #
# 空检索
# --------------------------------------------------------------------------- #
async def test_empty_retrieval_with_an_explicit_kb_refuses_without_an_answer(db_session):
    out = await _gate(
        db_session, "", {}, kb_explicit=True, retrieval_insufficient=True
    )
    assert out.status == "insufficient_evidence"
    assert out.validation.action == "refuse"
    assert out.answer == ""


async def test_empty_retrieval_without_an_explicit_kb_does_not_block(db_session):
    """会话默认绑定知识库时，按通用助手回答是产品定位，不归本门禁管。"""
    out = await _gate(
        db_session, "通用知识回答。", {}, kb_explicit=False, retrieval_insufficient=True
    )
    assert out.status == "answered"


async def test_empty_retrieval_on_a_high_risk_turn_refuses_even_without_an_explicit_kb(db_session):
    out = await _gate(
        db_session, "", {}, kb_explicit=False, is_high_risk=True, retrieval_insufficient=True
    )
    assert out.status == "insufficient_evidence"


# --------------------------------------------------------------------------- #
# metadata
# --------------------------------------------------------------------------- #
async def test_metadata_carries_no_answer_text(db_session):
    _kb, _doc, chunk = await _seed_chunk(db_session)
    secret = "这段话一个字都不该进 metadata"
    out = await _gate(db_session, f"{secret} [source 1]。", {1: _item(chunk)})
    assert secret not in str(out.to_metadata())


async def test_metadata_names_the_policy_version(db_session):
    out = await _gate(db_session, "结论 [source 1]。", {})
    assert out.to_metadata()["policy_version"] == "policy-v1"


# --------------------------------------------------------------------------- #
# 引用校验字段的初值
# --------------------------------------------------------------------------- #
def test_citation_validation_defaults_to_unchecked_not_passed():
    """``None`` = 还没查，``True`` = 查过了。默认 ``True`` 等于把「没查」伪装成「查过了」。"""
    from app.schemas import Citation

    citation = Citation(document_name="x")
    assert citation.validation.exists is None
    assert citation.validation.authorized is None
    assert citation.validation.supports_claim is None
