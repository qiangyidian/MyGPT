"""RAG prompt assembly.

Kept separate from RagService so prompt engineering changes live in one place.
The template is knowledge-first (cite sources, do not fabricate), matching the
platform's reliability guarantees.
"""
from __future__ import annotations

RAG_SYSTEM_PREAMBLE = (
    "你是一个严谨、可靠的 AI 助手。请优先根据提供的知识库内容回答用户问题。\n\n"
    "要求：\n"
    "1. 如果知识库内容足以回答，请基于知识库内容回答；\n"
    "2. 如果知识库内容不足，请明确说明“根据当前知识库内容无法确定”；\n"
    "3. 不要编造知识库中不存在的信息；\n"
    "4. 回答时尽量引用来源标记（如 [source 1]）；\n"
    "5. 如果用户问题和知识库无关，可使用通用知识回答，但需说明这不是来自知识库。\n\n"
    "知识库内容：\n{context}\n"
)


def build_rag_context(context: str) -> str:
    """Wrap retrieved context in the knowledge-first preamble."""
    if not context:
        return ""
    return RAG_SYSTEM_PREAMBLE.format(context=context)


def format_context_block(hits: list) -> str:
    """Turn retrieved hits into a numbered context string with source markers.

    Each hit's payload is expected to carry ``document_name`` and ``text``.
    """
    if not hits:
        return ""
    lines: list[str] = []
    for i, hit in enumerate(hits, start=1):
        payload = getattr(hit, "payload", None) or {}
        name = payload.get("document_name") or payload.get("source") or "未知来源"
        text = payload.get("text") or payload.get("content") or ""
        lines.append(f"[source {i}] {name}\n{text}")
    return "\n\n".join(lines)
