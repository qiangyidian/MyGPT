"""把答案切成「可核验主张」并归属引用（RAG-12 的 claim 级映射）。

设计文档要求「将每个答案句拆为可核验的 Claim，逐项匹配 ``context_id``」。有两种
做法，这里选的是第二种，理由写在契约里也要写在这里：

1. **让模型吐出结构化 claim**（JSON schema 输出）。它能做到语义原子级，但会杀掉
   流式——用户要等完整 JSON 才看到第一个字；要多一次调用；而且模型可以编造 claim
   结构本身，去校验一个被编造出来的结构没有意义。
2. **服务端切句 + 引用标记归属**（本模块）。零额外调用、对模型零要求、忠实于用户
   真正看到的那段文本。代价是 claim 粒度＝句子，不是语义原子。

切分刻意是**保守**的（少切几刀而不是切碎），因为粒度最终决定「校验不过时摘掉哪一
段」——切碎了会把半句话摘掉，比不摘更糟。所以：

  * 代码块整体不参与切分（代码不是主张，更不该被校验改写）；
  * 表格行、列表项各自成一条（它们是 markdown 的结构单元，不是句子）；
  * ``3.14``／``v1.2.3`` 里的点**不切**（否则「版本 1.2.3」会变成三条互不成立的
    主张）；
  * 中文只在 ``。！？`` 切，不在 ``；`` ``，`` 切——那是同一句内部的停顿。

**每条主张都带原文坐标**（``Claim.span``）。这不是锦上添花：摘除主张必须按坐标切除，
``str.replace`` 遇到重复句会删错位置，而删错位置意味着用户看到的答案和校验结论对
不上——那比不校验更糟。
"""
from __future__ import annotations

import re
from collections.abc import Iterator, Sequence

from app.rag.contracts import Claim, ContextItem

__all__ = [
    "build_claims",
    "cited_source_indexes",
    "claim_source_indexes",
    "remove_claim_spans",
    "split_claim_spans",
    "split_claims",
    "uncited_claim_ratio",
]

#: 代码围栏（``` 或 ~~~，允许首行带语言标注）。
_RE_FENCE = re.compile(r"^[ \t]*(?:```|~~~)")
#: 表格行：以 | 开头或结尾的整行。
_RE_TABLE_ROW = re.compile(r"^[ \t]*\|.*\|[ \t]*$")
#: 列表项：-, *, +, 或 1. / 1) —— 整项一条，不再按句号切。
_RE_LIST_ITEM = re.compile(r"^[ \t]*(?:[-*+]|\d+[.)])[ \t]+\S")
#: markdown 行首前缀（``## `` / ``- `` / ``> ``），归属时用它拿到正文起点。
_RE_MD_PREFIX = re.compile(r"^[ \t]*(?:#{1,6}[ \t]+|[-*+][ \t]+|\d+[.)][ \t]+|>[ \t]*)")
#: 引用标记，与 ``rag/citations.py`` 的 ``[source N]`` 契约同源。
_RE_SOURCE_MARKER = re.compile(r"\[\s*(?:source|来源)[\s:：]*(\d+)\s*\]", re.IGNORECASE)

#: 句末标点之后允许追进来的收尾符号（引号、括号、以及重复的句末标点）。
_CLOSERS = "」』】”’\"')）〕》"
_EN_SENTENCE_ENDERS = ".!?"


def _sentence_spans(body: str) -> list[tuple[int, int]]:
    """在**单行内部**找句子区间。返回 ``[start, end)`` 列表，覆盖整行。"""
    spans: list[tuple[int, int]] = []
    start = 0
    i = 0
    n = len(body)
    while i < n:
        ch = body[i]
        end: int | None = None
        if ch in "。！？!?":
            end = i + 1
        elif ch == ".":
            prev = body[i - 1] if i > 0 else ""
            nxt = body[i + 1] if i + 1 < n else ""
            # 小数点与版本号：点两侧都是数字就不切。
            if not (prev.isdigit() or nxt.isdigit()):
                if nxt == "" or nxt.isspace():
                    end = i + 1
        if end is not None:
            # 吸收紧跟的收尾引号/括号与重复标点，避免它们被留成孤儿。
            while end < n and (body[end] in _CLOSERS or body[end] in "。！？!?"):
                end += 1
            spans.append((start, end))
            start = end
            i = end
            continue
        i += 1
    if start < n:
        spans.append((start, n))
    return spans


def split_claim_spans(text: str) -> list[tuple[int, int]]:
    """答案 → 主张区间（原文坐标，按文档顺序、互不重叠）。"""
    out: list[tuple[int, int]] = []
    if not text or not text.strip():
        return out

    offset = 0
    in_fence = False
    for raw_line in text.split("\n"):
        line_start = offset
        offset += len(raw_line) + 1  # +1 = 被 split 吃掉的 \n
        line = raw_line.rstrip()
        if _RE_FENCE.match(line):
            in_fence = not in_fence
            continue
        if in_fence or not line.strip():
            continue

        prefix = _RE_MD_PREFIX.match(line)
        body_start = prefix.end() if prefix else 0
        body = line[body_start:]
        lead = len(body) - len(body.lstrip())
        body_start += lead
        body = body[lead:]
        if not body:
            continue

        if _RE_TABLE_ROW.match(line) or _RE_LIST_ITEM.match(line):
            out.append((line_start + body_start, line_start + body_start + len(body)))
            continue

        for s, e in _sentence_spans(body):
            piece = body[s:e]
            ls = len(piece) - len(piece.lstrip())
            rs = len(piece.rstrip())
            if rs <= ls:
                continue
            out.append((line_start + body_start + s + ls, line_start + body_start + s + rs))
    return out


def split_claims(text: str) -> list[str]:
    """把答案切成主张文本（句子级）。空/纯空白单元不返回。"""
    return [text[s:e] for s, e in split_claim_spans(text)]


def claim_source_indexes(claim_text: str) -> list[int]:
    """一条主张里出现的 ``[source N]`` 序号（1-based，去重保序）。"""
    seen: list[int] = []
    for m in _RE_SOURCE_MARKER.finditer(claim_text or ""):
        n = int(m.group(1))
        if n not in seen:
            seen.append(n)
    return seen


def build_claims(answer: str, source_index: dict[int, ContextItem]) -> list[Claim]:
    """切句 + 归属：``[source N]`` → 本次请求的 ``context_id``。

    ``source_index`` 是「本次请求真的给过模型的那批证据」的序号表。序号不在表里的
    引用**不产生** ``evidence_context_ids``——这正是「引用 ID 必须属于本次请求」的
    判定依据，``postprocess`` 据此决定摘除还是拒答。

    标注文本仍保留 ``[source N]``：它是用户可见的锚点，由
    ``citations.sanitize_unbacked_source_markers`` 单独负责把越界的删掉。
    """
    claims: list[Claim] = []
    for i, (start, end) in enumerate(split_claim_spans(answer), start=1):
        sentence = answer[start:end]
        evidence: list[str] = []
        for n in claim_source_indexes(sentence):
            item = source_index.get(n)
            if item is not None and item.context_id not in evidence:
                evidence.append(item.context_id)
        claims.append(
            Claim(
                claim_id=f"claim-{i}",
                text=sentence,
                evidence_context_ids=evidence,
                span=(start, end),
            )
        )
    return claims


def cited_source_indexes(claims: Sequence[Claim]) -> list[int]:
    """当前答案里仍然站得住的 ``[source N]`` 序号（1-based，升序去重）。

    只看**未摘除**的主张，所以摘掉一条主张之后重新调用，得到的就是还站得住的引用。
    """
    out: set[int] = set()
    for claim in claims:
        if claim.dropped or claim.span is None:
            continue
        out.update(claim_source_indexes(claim.text))
    return sorted(out)


def remove_claim_spans(answer: str, claims: Sequence[Claim]) -> str:
    """按坐标切除被标记为 ``dropped`` 的主张，返回新的答案文本。

    从后往前切，前面的坐标才不会失效。**不重建 markdown**：代码块、标题、列表符号
    全都原样保留，被摘掉的只是那几句话本身——重建一遍会把用户能读的排版毁掉，而
    排版不是校验的职责。
    """
    spans = sorted(
        (c.span for c in claims if c.dropped and c.span is not None),
        key=lambda s: s[0],
        reverse=True,
    )
    out = answer or ""
    for start, end in spans:
        if 0 <= start < end <= len(out):
            out = out[:start] + out[end:]
    # 摘除会在原位留下空行/多余空白；收敛到最多一个空行，别让答案中间裂开一片。
    out = re.sub(r"[ \t]+\n", "\n", out)
    out = re.sub(r"\n{3,}", "\n\n", out)
    return out.strip()


def uncited_claim_ratio(claims: Sequence[Claim]) -> float:
    """无引用主张占比 —— 文档 RAG-12 的质量指标之一（只上报，不设门禁）。"""
    live = [c for c in claims if not c.dropped]
    if not live:
        return 0.0
    return sum(1 for c in live if not c.evidence_context_ids) / len(live)


def _iter_spans_debug(text: str) -> Iterator[tuple[int, int, str]]:  # pragma: no cover
    """调试用：把区间连同文本打出来。"""
    for s, e in split_claim_spans(text):
        yield s, e, text[s:e]
