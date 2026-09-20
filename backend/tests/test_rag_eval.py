"""RAG 检索质量评测（条目 25）的单测：指标算术、golden set 自检、门禁可判别性。

这里最关键的一组是「判别性」测试：把一个明显更差的检索器喂给同一份 golden set，
分数必须掉、必须触发 failures。否则门禁只是一个永远绿的装饰。
"""
from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Any

import pytest

from app.evals.rag_golden import GoldenSetError, load_golden_corpus
from app.evals.rag_metrics import (
    QueryMetrics,
    citation_precision,
    dcg_at_k,
    evaluate_query,
    mean_reciprocal_rank,
    ndcg_at_k,
    precision_at_k,
    recall_at_k,
    summarize,
    ungrounded_citations,
)
from app.evals.rag_retrieval import (
    Retrieved,
    Thresholds,
    bm25_retriever,
    evaluate_corpus,
    run_live_rag_retrieval_eval,
    run_rag_retrieval_eval,
)
from app.evals.runner import run_evals


@pytest.fixture(autouse=True)
def _hermetic_eval_env(monkeypatch: pytest.MonkeyPatch):
    """评测的门禁值不能受本机环境变量影响：谁都不会在自己的 shell 里设这些。"""
    for name in ("EVAL_RAG_KB_IDS", "EVAL_RAG_GOLDEN_PATH", "EVAL_LIVE_MODEL_API_KEY"):
        monkeypatch.delenv(name, raising=False)



# --------------------------------------------------------------------------- #
# Metric arithmetic.
# --------------------------------------------------------------------------- #
def test_recall_and_precision_truncate_at_k():
    relevant = ["a", "b"]
    ranked = ["x", "a", "y", "b", "z"]
    assert recall_at_k(ranked, relevant, k=2) == 0.5  # 只有 a 进了前 2
    assert recall_at_k(ranked, relevant, k=4) == 1.0
    assert precision_at_k(ranked, relevant, k=2) == 0.5
    assert precision_at_k(ranked, relevant, k=1) == 0.0
    assert recall_at_k(["a"], ["a", "a", "b"], k=3) == pytest.approx(0.5)  # 标注去重


def test_metrics_treat_an_empty_label_as_zero_not_perfect():
    # 「没标注」绝不等于「答对了」：否则漏标会静默刷高分数。
    assert recall_at_k(["a"], [], k=3) == 0.0
    assert ndcg_at_k(["a"], [], k=3) == 0.0
    assert mean_reciprocal_rank(["a", "b"], ["b"]) == 0.5
    assert mean_reciprocal_rank(["a"], ["zz"]) == 0.0


def test_ndcg_rewards_the_relevant_chunk_being_first():
    relevant = ["a"]
    assert ndcg_at_k(["a", "x", "y"], relevant, k=3) == 1.0
    # 位置 2 的折扣是 1/log2(3)，正好等于单次命中的 DCG。
    assert ndcg_at_k(["x", "a", "y"], relevant, k=3) == pytest.approx(1 / math.log2(3))
    assert ndcg_at_k(["x", "y", "a"], relevant, k=2) == 0.0
    assert dcg_at_k(["a", "a", "b"], ["a", "b"], k=3) == pytest.approx(
        1 / math.log2(2) + 1 / math.log2(4)
    )  # 重复 id 只计一次


def test_ndcg_of_the_ideal_ordering_is_one_for_multi_relevance():
    relevant = ["a", "b", "c"]
    assert ndcg_at_k(["c", "b", "a"], relevant, k=3) == 1.0  # 二值相关：顺序无所谓
    # 前 k 里全是相关块 => 归一化后仍是满分，缺的那一条由 recall 去惩罚。
    assert ndcg_at_k(["a", "b"], relevant, k=2) == 1.0
    assert recall_at_k(["a", "b"], relevant, k=2) == pytest.approx(2 / 3)
    # 只召回一条、且排在第二位：nDCG 只有 0.5（理想是两条各占一位）。
    assert ndcg_at_k(["x", "a"], relevant, k=2) == pytest.approx((1 / math.log2(3)) / 1.6309297535714577)


def test_citation_precision_uses_the_cited_window():
    assert citation_precision(["a", "x"], ["a"]) == 0.5
    assert citation_precision([], ["a"]) == 0.0
    assert citation_precision(["x", "y"], ["a"]) == 0.0


def test_evaluate_query_reports_every_axis():
    m = evaluate_query("q1", ["a", "b"], ["x", "a", "b"], k=3)
    assert (m.query_id, m.k, m.first_relevant_rank) == ("q1", 3, 2)
    assert m.recall == 1.0
    assert m.precision == pytest.approx(2 / 3)
    assert m.mrr == 0.5
    assert m.average_precision == pytest.approx((1 / 2 + 2 / 3) / 2)
    assert m.to_dict()["relevant_count"] == 2


def test_summarize_averages_per_query_and_flags_zero_recall():
    metrics = [
        evaluate_query("good", ["a"], ["a"], k=1),
        evaluate_query("bad", ["b"], ["x"], k=1),
    ]
    s = summarize(metrics)
    assert s["queries"] == 2 and s["k"] == 1
    assert s["recall"] == pytest.approx(0.5)
    assert s["zero_recall_queries"] == ["bad"]
    assert summarize([]) == {"queries": 0}


def test_ungrounded_citations_catches_mismatched_and_unknown_sources():
    corpus = {"a": "住宿每晚上限 600 元", "b": "餐补按自然日包干"}
    assert ungrounded_citations([("a", "住宿每晚")], corpus) == []
    assert ungrounded_citations([("a", "餐补")], corpus) == ["a"]
    assert ungrounded_citations([("zz", "任意")], corpus) == ["zz"]


# --------------------------------------------------------------------------- #
# Golden set integrity.
# --------------------------------------------------------------------------- #
def test_shipped_golden_set_loads_and_is_annotated():
    golden = load_golden_corpus()
    assert golden.version == 1
    assert len(golden.queries) >= 10
    assert len(golden.chunks) >= 10
    ids = {c["chunk_id"] for c in golden.chunks}
    names = {c["document_name"] for c in golden.chunks}
    for query in golden.queries:
        assert set(query.relevant) <= ids
        assert set(query.relevant_document_names) <= names


def _write(tmp_path: Path, payload: dict[str, Any]) -> Path:
    path = tmp_path / "golden.json"
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return path


def _payload(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "version": 1,
        "docs": [
            {
                "doc_id": "d1",
                "document_name": "d1.md",
                "chunks": [{"chunk_id": "d1:0", "text": "住宿每晚上限 600 元"}],
            }
        ],
        "queries": [
            {"query_id": "q1", "query": "住宿上限是多少", "k": 1, "relevant": ["d1:0"]}
        ],
    }
    base.update(overrides)
    return base


def test_golden_loader_rejects_annotations_that_point_at_nothing(tmp_path: Path):
    payload = _payload(
        queries=[{"query_id": "q1", "query": "住宿上限", "k": 1, "relevant": ["nope:0"]}]
    )
    with pytest.raises(GoldenSetError, match="不存在的切片"):
        load_golden_corpus(_write(tmp_path, payload))


def test_golden_loader_rejects_a_query_with_no_answer(tmp_path: Path):
    payload = _payload(queries=[{"query_id": "q1", "query": "住宿上限", "k": 1, "relevant": []}])
    with pytest.raises(GoldenSetError, match="没有标注"):
        load_golden_corpus(_write(tmp_path, payload))


def test_golden_loader_rejects_duplicate_ids(tmp_path: Path):
    payload = _payload(
        docs=[
            {
                "doc_id": "d1",
                "document_name": "d1.md",
                "chunks": [
                    {"chunk_id": "d1:0", "text": "住宿上限"},
                    {"chunk_id": "d1:0", "text": "重复的切片 id"},
                ],
            }
        ]
    )
    with pytest.raises(GoldenSetError, match="chunk_id 重复"):
        load_golden_corpus(_write(tmp_path, payload))


def test_golden_loader_rejects_an_answer_that_shares_no_word_with_the_query(tmp_path: Path):
    # 标注错位（查询问住宿、答案讲备份）必须在加载阶段就炸掉。
    payload = _payload(
        docs=[
            {
                "doc_id": "d1",
                "document_name": "d1.md",
                "chunks": [{"chunk_id": "d1:0", "text": "数据库每天凌晨全量备份"}],
            }
        ]
    )
    with pytest.raises(GoldenSetError, match="无任何共同词"):
        load_golden_corpus(_write(tmp_path, payload))


def test_golden_loader_rejects_a_missing_file(tmp_path: Path):
    with pytest.raises(GoldenSetError, match="不存在"):
        load_golden_corpus(tmp_path / "nope.json")


def test_golden_path_env_override(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    from app.evals import rag_golden

    monkeypatch.setenv(rag_golden.GOLDEN_PATH_ENV, str(_write(tmp_path, _payload())))
    assert rag_golden.resolve_golden_path().parent == tmp_path
    assert load_golden_corpus().queries[0].query_id == "q1"


# --------------------------------------------------------------------------- #
# The driver: thresholds, grounding, and discrimination.
# --------------------------------------------------------------------------- #
def test_baseline_retriever_clears_the_gate_on_the_shipped_set():
    golden = load_golden_corpus()
    result = evaluate_corpus(golden, bm25_retriever(golden))
    assert result.ok, result.failures
    assert result.summary["recall"] >= 0.85
    assert result.summary["grounded"] == 1.0


def test_gate_fails_when_the_ranking_is_reversed():
    golden = load_golden_corpus()
    baseline = bm25_retriever(golden)

    def reversed_retriever(query: str) -> list[Retrieved]:
        return list(reversed(baseline(query)))

    result = evaluate_corpus(golden, reversed_retriever)
    assert not result.ok
    summary = result.summary
    assert summary["recall"] < 0.85 and summary["ndcg"] < 0.80
    assert any("nDCG" in f for f in result.failures)


def test_gate_fails_when_the_retriever_returns_nothing():
    golden = load_golden_corpus()
    result = evaluate_corpus(golden, lambda _query: [])
    assert not result.ok
    assert result.summary["recall"] == 0.0
    assert set(result.summary["zero_recall_queries"]) == {q.query_id for q in golden.queries}


def test_gate_catches_a_lost_cjk_tokenizer(monkeypatch: pytest.MonkeyPatch):
    """历史上真实发生过的故障：中文 bigram 一退化，词法召回整段变 0。"""
    import app.evals.rag_retrieval as module

    monkeypatch.setattr(
        module, "_terms", lambda text: [t for t in text.lower().split() if len(t) > 1]
    )
    golden = load_golden_corpus()
    result = evaluate_corpus(golden, bm25_retriever(golden))
    assert not result.ok
    # 只剩拉丁词元：中文查询几乎全线失联（少数含英文的查询侥幸命中）。
    assert result.summary["recall"] < 0.2


def test_grounding_check_flags_a_citation_from_another_chunk():
    golden = load_golden_corpus()
    first = golden.chunks[0]["chunk_id"]
    other_text = golden.chunks[1]["text"]

    def impostor(_query: str) -> list[Retrieved]:
        return [Retrieved(key=first, text=other_text)]

    result = evaluate_corpus(golden, impostor)
    assert first in result.ungrounded
    assert result.summary["grounded"] < 1.0
    assert any("溯源" in f for f in result.failures)
    # 关掉溯源检查时，同样的命中不该再报这个问题
    relaxed = evaluate_corpus(golden, impostor, check_grounding=False)
    assert not any("溯源" in f for f in relaxed.failures)


def test_thresholds_are_configurable():
    golden = load_golden_corpus()
    strict = evaluate_corpus(golden, bm25_retriever(golden), thresholds=Thresholds(recall=1.01))
    assert any("recall@k" in f for f in strict.failures)


def test_precision_axis_is_reported_but_not_gated_by_default():
    golden = load_golden_corpus()
    result = evaluate_corpus(golden, bm25_retriever(golden))
    # 小语料 + 单相关标注下 precision 天花板很低，只上报。
    assert 0 < result.summary["precision"] < 0.5
    assert 0 < result.summary["citation_accuracy"] < 0.5
    assert not any("precision" in f for f in result.failures)
    gated = evaluate_corpus(golden, bm25_retriever(golden), thresholds=Thresholds(precision=0.99))
    assert any("precision@k" in f for f in gated.failures)


def test_top_k_caps_the_window_scored():
    golden = load_golden_corpus()
    narrow = evaluate_corpus(golden, bm25_retriever(golden), top_k=1)
    assert all(len(m.retrieved) <= 1 for m in narrow.metrics)


# --------------------------------------------------------------------------- #
# Contracts as the release gate sees them.
# --------------------------------------------------------------------------- #
def test_offline_contract_passes_and_reports_metrics():
    result = run_rag_retrieval_eval()
    assert result["name"] == "rag_retrieval_quality"
    assert result["status"] == "pass", result["reason"]
    assert "recall@k" in result["reason"]


def test_offline_contract_fails_loudly_on_a_broken_golden_set(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    # 评测集坏掉必须红在门禁上，而不是把异常抛穿、连带整个 runner 一起炸掉。
    from app.evals import rag_retrieval as driver

    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps(_payload(queries=[])), encoding="utf-8")
    monkeypatch.setattr(driver, "load_golden_corpus", lambda: load_golden_corpus(bad))
    assert "评测未能运行" in run_rag_retrieval_eval()["reason"]

    # 还原后照常通过：证明上面那次红是坏数据造成的，不是自我污染。
    monkeypatch.undo()
    assert run_rag_retrieval_eval()["status"] == "pass"


def test_live_contract_skips_without_a_seeded_kb(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("EVAL_RAG_KB_IDS", raising=False)
    result = run_live_rag_retrieval_eval()
    assert result["status"] == "skip"
    assert result["kind"] == "live"
    assert "EVAL_RAG_KB_IDS" in result["reason"]


def test_live_contract_fails_on_an_unparseable_kb_list(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("EVAL_RAG_KB_IDS", "not-a-uuid")
    assert run_live_rag_retrieval_eval()["status"] == "fail"


def test_release_gate_includes_the_retrieval_contract():
    report = run_evals()
    by_name = {r["name"]: r for r in report["results"]}
    assert by_name["rag_retrieval_quality"]["status"] == "pass"
    assert by_name["rag_retrieval_live"]["status"] == "skip"
    assert report["failed"] == 0
    assert isinstance(report["passed"], int) and report["passed"] >= 5


def test_query_metrics_dataclass_is_frozen_and_serializable():
    m = QueryMetrics(
        query_id="q",
        k=3,
        relevant=("a",),
        retrieved=("a", "b"),
        recall=1.0,
        precision=0.5,
        ndcg=1.0,
        mrr=1.0,
        average_precision=1.0,
        first_relevant_rank=1,
    )
    assert m.to_dict()["precision"] == 0.5
    with pytest.raises(Exception):
        m.recall = 0.0  # type: ignore[misc]
