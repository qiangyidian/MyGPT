"""RAG prompt assembly.

Kept separate from RagService so prompt engineering changes live in one place.
The template is knowledge-first (cite sources, do not fabricate), matching the
platform's reliability guarantees.
"""
from __future__ import annotations

from dataclasses import replace

from app.core.config import get_settings
from app.security.prompt_boundary import apply_untrusted_boundary

# A remainder smaller than this is noise: dropping the chunk outright is more
# honest than appending a sentence fragment to the prompt.
_MIN_KEEP_TOKENS = 64

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

    正文是外部数据，所以每个 chunk 单独过一次不可信内容围栏：来源行（``[source i]``
    及章节 / 页码）留在围栏之外，引用对齐与 ``[source N]`` 标记不受影响，只有正文
    进围栏。围栏同时中和正文里出现的定界标记，避免文档自己提前闭合上下文。
    """
    if not hits:
        return ""
    lines: list[str] = []
    for i, hit in enumerate(hits, start=1):
        payload = getattr(hit, "payload", None) or {}
        name = payload.get("document_name") or payload.get("source") or "未知来源"
        text = payload.get("text") or payload.get("content") or ""
        heading = payload.get("heading")
        location = f" · 章节：{heading}" if heading else ""
        page = payload.get("page")
        if page:
            location += f" · 第 {page} 页"
        body = apply_untrusted_boundary("rag", text)
        lines.append(f"[source {i}] {name}{location}\n{body}")
    return "\n\n".join(lines)


def _token_counter():
    """tiktoken-backed counter, or a char heuristic when the encoding is missing.

    Imported lazily on purpose: this module is on the chat hot path and must not
    make prompt assembly depend on tiktoken being loadable.
    """
    try:
        from app.rag.splitter import _count_tokens
    except Exception:  # pragma: no cover
        return lambda text: max(1, len(text) // 4)
    return lambda text: _count_tokens(text, "gpt-3.5-turbo")


def _head_to_tokens(text: str, budget: int, counter) -> str:
    """Longest prefix of ``text`` within ``budget`` tokens (binary search on chars)."""
    if budget <= 0 or not text or counter(text) <= budget:
        return text
    low, high = 0, len(text)
    while low < high:
        mid = (low + high + 1) // 2
        if counter(text[:mid]) <= budget:
            low = mid
        else:
            high = mid - 1
    return text[:low]


def fit_context(hits: list, max_tokens: int | None = None) -> list:
    """Pack hits into the context window, most relevant first.

    Retrieval was bounded by chunk *count* only, so the prompt size grew with
    whoever configured the biggest ``top_k`` (or the longest chunks): 12 chunks
    of 1.5k tokens is an 18k-token context paid for on every turn, and the
    "lost in the middle" degradation that comes with it.

    Hits are dropped whole from the tail; only the one hit that straddles the
    remaining budget is truncated, and it is replaced (not mutated) so the
    citation rendered from the returned list shows exactly what the model saw.
    ``max_tokens`` <= 0 disables the budget.
    """
    budget = int(max_tokens if max_tokens is not None else get_settings().RAG_CONTEXT_TOKENS)
    if not hits or budget <= 0:
        return list(hits)
    counter = _token_counter()
    out: list = []
    used = 0
    for hit in hits:
        payload = getattr(hit, "payload", None) or {}
        text = payload.get("text") or payload.get("content") or ""
        cost = counter(text)
        if used + cost <= budget:
            out.append(hit)
            used += cost
            continue
        remaining = budget - used
        # Keep a prefix of this hit only when it still leaves something usable in
        # the window; a two-token remainder is noise worth dropping.
        if remaining >= _MIN_KEEP_TOKENS and text:
            trimmed = _head_to_tokens(text, remaining, counter)
            try:
                out.append(replace(hit, payload={**payload, "text": trimmed}))
            except TypeError:  # not a dataclass — keep it whole rather than lose it
                out.append(hit)
        break
    return out
