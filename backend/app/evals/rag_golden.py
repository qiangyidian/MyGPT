"""Golden set 的加载与自检。

「评测集坏掉」比「检索退步」更早发生：标注写错一个 id、或者查询和标准答案根本
对不上，指标会一路绿灯而什么都没测到。所以加载时就把这些结构性问题当异常抛出来，
让门禁红在加载阶段，而不是红在一个谁也解释不了的分数上。
"""
from __future__ import annotations

import json
import os
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from app.rag.keyword import _tokenize

__all__ = [
    "DEFAULT_GOLDEN_PATH",
    "GoldenCorpus",
    "GoldenQuery",
    "GoldenSetError",
    "load_golden_corpus",
]

DEFAULT_GOLDEN_PATH = Path(__file__).with_name("golden") / "rag_retrieval.v1.json"

#: 换一份更大的、贴近真实知识库的标注集时用它指路（CI 不需要，默认走仓库内这份）。
GOLDEN_PATH_ENV = "EVAL_RAG_GOLDEN_PATH"


class GoldenSetError(ValueError):
    """Golden set 自身不合法——先修标注，再谈检索质量。"""


@dataclass(frozen=True)
class GoldenQuery:
    query_id: str
    query: str
    k: int
    #: 理想顺序的相关块（二值相关，顺序只作说明用）。
    relevant: tuple[str, ...]
    #: 相关块所属文档的文件名。在线评测只能按名字对齐——生产切片 id 是索引时生成
    #: 的，标注里没有；文件名是上传时唯一还留得住的标识。
    relevant_document_names: tuple[str, ...]


@dataclass(frozen=True)
class GoldenCorpus:
    version: int
    description: str
    chunks: tuple[dict[str, Any], ...]
    queries: tuple[GoldenQuery, ...]

    @property
    def texts(self) -> dict[str, str]:
        return {c["chunk_id"]: c["text"] for c in self.chunks}

    @property
    def doc_of_chunk(self) -> dict[str, str]:
        return {c["chunk_id"]: c["doc_id"] for c in self.chunks}

    @property
    def document_names(self) -> dict[str, str]:
        return {c["doc_id"]: c["document_name"] for c in self.chunks}


def resolve_golden_path(override: str | os.PathLike[str] | None = None) -> Path:
    if override:
        return Path(override)
    from_env = os.environ.get(GOLDEN_PATH_ENV)
    if from_env:
        return Path(from_env)
    return DEFAULT_GOLDEN_PATH


def _dedup(items: Iterable[str]) -> tuple[str, ...]:
    return tuple(dict.fromkeys(items))


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise GoldenSetError(message)


def load_golden_corpus(path: str | os.PathLike[str] | None = None) -> GoldenCorpus:
    """Read and self-check one golden set file.

    The self-check is the point of this function; a corpus that loads is safe to
    score without re-validating downstream.
    """
    file_path = resolve_golden_path(path)
    if not file_path.exists():
        raise GoldenSetError(f"golden set 不存在: {file_path}")
    raw = json.loads(file_path.read_text(encoding="utf-8"))

    chunks: list[dict[str, Any]] = []
    seen_chunks: set[str] = set()
    doc_names: dict[str, str] = {}
    doc_of_chunk: dict[str, str] = {}
    for doc in raw.get("docs") or []:
        doc_id = str(doc.get("doc_id") or "")
        document_name = str(doc.get("document_name") or doc_id)
        _require(bool(doc_id), f"{file_path.name}: 文档缺少 doc_id")
        _require(
            doc_id not in doc_names,
            f"{file_path.name}: doc_id 重复 {doc_id}",
        )
        doc_names[doc_id] = document_name
        doc_chunks = doc.get("chunks") or []
        _require(bool(doc_chunks), f"{file_path.name}: 文档 {doc_id} 没有任何切片")
        for chunk in doc_chunks:
            chunk_id = str(chunk.get("chunk_id") or "")
            text = str(chunk.get("text") or "")
            _require(bool(chunk_id), f"{file_path.name}: 切片缺少 chunk_id（文档 {doc_id}）")
            _require(
                chunk_id not in seen_chunks,
                f"{file_path.name}: chunk_id 重复 {chunk_id}",
            )
            _require(bool(text.strip()), f"{file_path.name}: 切片 {chunk_id} 正文为空")
            seen_chunks.add(chunk_id)
            doc_of_chunk[chunk_id] = doc_id
            chunks.append(
                {
                    "chunk_id": chunk_id,
                    "doc_id": doc_id,
                    "document_name": document_name,
                    "text": text,
                }
            )
    _require(bool(chunks), f"{file_path.name}: golden set 没有语料")

    queries: list[GoldenQuery] = []
    seen_queries: set[str] = set()
    for item in raw.get("queries") or []:
        query_id = str(item.get("query_id") or "")
        query = str(item.get("query") or "")
        relevant = _dedup(str(r) for r in item.get("relevant") or [])
        _require(bool(query_id), f"{file_path.name}: 查询缺少 query_id")
        _require(
            query_id not in seen_queries,
            f"{file_path.name}: query_id 重复 {query_id}",
        )
        _require(bool(query.strip()), f"{file_path.name}: 查询 {query_id} 文本为空")
        _require(bool(relevant), f"{file_path.name}: 查询 {query_id} 没有标注相关切片")
        for chunk_id in relevant:
            _require(
                chunk_id in seen_chunks,
                f"{file_path.name}: 查询 {query_id} 标注了不存在的切片 {chunk_id}",
            )
        k = int(item.get("k") or 0)
        _require(k >= 1, f"{file_path.name}: 查询 {query_id} 的 k 必须 >= 1")
        # 标注对了，查询和答案至少该共享一个词；一个都不共享时要么是标错了，
        # 要么是这条查询根本考不了检索（同义词改写属于另一个评测）。
        latin, grams = _tokenize(query)
        terms = set(latin) | set(grams)
        for chunk_id in relevant:
            chunk = next(c for c in chunks if c["chunk_id"] == chunk_id)
            cl, cg = _tokenize(chunk["text"])
            if terms.isdisjoint(set(cl) | set(cg)):
                raise GoldenSetError(
                    f"{file_path.name}: 查询 {query_id} 与标注 {chunk_id} 无任何共同词，"
                    "请核对标注或改写查询"
                )
        seen_queries.add(query_id)
        queries.append(
            GoldenQuery(
                query_id=query_id,
                query=query,
                k=k,
                relevant=relevant,
                relevant_document_names=_dedup(doc_names[doc_of_chunk[c]] for c in relevant),
            )
        )
    _require(bool(queries), f"{file_path.name}: golden set 没有查询")

    return GoldenCorpus(
        version=int(raw.get("version") or 1),
        description=str(raw.get("description") or ""),
        chunks=tuple(chunks),
        queries=tuple(queries),
    )
