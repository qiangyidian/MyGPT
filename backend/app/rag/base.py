"""RAG pipeline abstractions. Concrete impls (Qdrant store, pdf/docx parsers, ...) are
pluggable. RagService orchestrates these; nothing in business code reaches below it.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any


@dataclass
class VectorPoint:
    id: str
    vector: list[float]
    payload: dict[str, Any] = field(default_factory=dict)


@dataclass
class SearchHit:
    id: str
    score: float
    payload: dict[str, Any] = field(default_factory=dict)
    # Set by a real reranker (None when no reranker ran). Kept separate from
    # ``score`` (the raw vector similarity) so the UI can show vector vs rerank
    # scores independently in debug mode.
    rerank_score: float | None = None


class DocumentParser(ABC):
    @abstractmethod
    def parse(self, file_path: str, file_type: str) -> str: ...


class TextSplitter(ABC):
    @abstractmethod
    def split(self, text: str) -> list[str]: ...


class Embedder(ABC):
    @abstractmethod
    async def embed(self, texts: list[str]) -> list[list[float]]: ...

    @property
    @abstractmethod
    def dim(self) -> int: ...


class VectorStore(ABC):
    @abstractmethod
    async def ensure_collection(self, collection: str, dim: int) -> None: ...

    @abstractmethod
    async def upsert(self, collection: str, points: list[VectorPoint]) -> None: ...

    @abstractmethod
    async def search(
        self, collection: str, query: list[float], top_k: int = 5,
        filters: dict[str, Any] | None = None,
    ) -> list[SearchHit]: ...

    @abstractmethod
    async def delete_by_filter(self, collection: str, filters: dict[str, Any]) -> None: ...


class Reranker(ABC):
    @abstractmethod
    async def rerank(self, query: str, hits: list[SearchHit], top_k: int = 5) -> list[SearchHit]: ...
