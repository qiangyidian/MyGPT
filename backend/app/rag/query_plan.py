"""RAG-06 查询理解：把用户问题变成一份可解释、可回放的 ``QueryPlan``。

设计文档对这一阶段有两条互相拉扯的要求，这个模块的形状就是它们的产物：

**要多做**——解析 ``YYYY-MM-DD`` 与相对日期、错误码、产品型号、命名实体，抽出
Schema 白名单内的过滤条件，识别语言与意图，并保证「纯编号查询必须同时走精确词项
检索」（只靠语义向量找 ``E1001`` 是不可靠的）。

**要少做**——任何字段不能从 Query 确定时**不推测**；不在白名单、值无法确定、日期
解析失败的过滤条件一律**丢弃并记录原因**。文档 1.3 把「过滤器越权尝试数」列为这一
阶段的质量指标，所以拒绝不是静默的：每一次拒绝都带着原因进 ``rejected_filters``。

本期**不启用 LLM 改写**（``query_variants`` 恒空）。文档规定改写启用前必须在
EvalSet 上证明「对应类别 Recall@30 提升 ≥5 个百分点、改写保真抽检 ≥95%」——两条
都还没有数据，所以这条路径是关着的，不是漏了。

整个模块零模型调用、零 IO、纯函数（``now`` 可注入），因此可以逐条钉死在单测里。
"""
from __future__ import annotations

import re
import uuid
from dataclasses import dataclass
from datetime import date, timedelta

from app.rag.contracts import Entity, QueryFilter, QueryPlan, RejectedFilter, new_trace_id

__all__ = ["DateRange", "build_query_plan", "resolve_relative_dates"]


# --------------------------------------------------------------------------- #
# 过滤字段白名单
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class _FieldSpec:
    """一个可出现在过滤条件里的字段。

    ``indexed`` 是**索引层能不能真的下推**，不是「该字段是否存在于 schema」。
    两者必须分开：把没索引的字段也拒成 ``field_not_in_schema`` 会让运维误以为
    schema 少了字段，而 ``field_not_indexed`` 才正确指向「该建索引了」。
    """

    ops: frozenset[str]
    indexed: bool


_ALL_CMP = frozenset({"gt", "gte", "lt", "lte", "between"})
_EQ_IN = frozenset({"eq", "in"})

#: RAG-04 的 metadata schema 白名单。今天只有 ``document_id`` / ``knowledge_base_id``
#: 真的能下推到两个检索通道（Qdrant payload 过滤 + DocumentChunk 列）；其余字段
#: 已经在契约里登记，但索引层查不了，因此会被记录成 ``field_not_indexed`` 而**不生效**。
#: 这不是省略，是文档要求的诚实状态：不应用它，并留下「为什么没应用」。
FILTER_SCHEMA: dict[str, _FieldSpec] = {
    "document_id": _FieldSpec(_EQ_IN, indexed=True),
    "knowledge_base_id": _FieldSpec(_EQ_IN, indexed=True),
    "updated_at": _FieldSpec(_ALL_CMP, indexed=False),
    "created_at": _FieldSpec(_ALL_CMP, indexed=False),
    "language": _FieldSpec(_EQ_IN, indexed=False),
    "content_type": _FieldSpec(_EQ_IN, indexed=False),
    "classification": _FieldSpec(_EQ_IN, indexed=False),
    "valid_from": _FieldSpec(frozenset({"lte"}), indexed=False),
    "valid_to": _FieldSpec(frozenset({"gte"}), indexed=False),
}

PLAN_VERSION = "query-plan-v1"


# --------------------------------------------------------------------------- #
# 日期
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class DateRange:
    """闭区间 ``[start, end]``（含两端），ISO 日期。"""

    start: date
    end: date

    def as_filter(self) -> dict[str, str]:
        return {"from": self.start.isoformat(), "to": self.end.isoformat()}


def _month_start(d: date) -> date:
    return d.replace(day=1)


def _add_months(d: date, months: int) -> date:
    """按月平移，日期溢出时夹到当月最后一天（1/31 + 1 月 = 2/28）。"""
    total = d.year * 12 + (d.month - 1) + months
    year, month = divmod(total, 12)
    month += 1
    day = d.day
    while day > 0:
        try:
            return date(year, month, day)
        except ValueError:
            day -= 1
    return date(year, month, 1)  # pragma: no cover - day=1 永远合法


_ISO_DATE = r"\d{4}-\d{2}-\d{2}"
_ISO_MONTH = r"\d{4}-\d{2}"
_RANGE_SEP = r"\s*(?:~|～|—|–|-|至|到|到)\s*"

_RE_ISO_RANGE = re.compile(rf"({_ISO_DATE}){_RANGE_SEP}({_ISO_DATE})")
_RE_ISO_MONTH_RANGE = re.compile(rf"({_ISO_MONTH}){_RANGE_SEP}({_ISO_MONTH})")
_RE_ISO_DATE = re.compile(_ISO_DATE)
_RE_ISO_MONTH = re.compile(_ISO_MONTH)

#: (正则, 解析器)。**顺序即优先级**：先长后短、先具体后笼统，否则「上个月」会被
#: 「本月」之类的短模式或「月」类兜底先吃掉。
_RELATIVE: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"前\s*(\d+)\s*天"), "days_ago"),
    (re.compile(r"前\s*(\d+)\s*个?月"), "months_ago"),
    (re.compile(r"前\s*(\d+)\s*年"), "years_ago"),
    (re.compile(r"(?:过去|最近|近)\s*(\d+)\s*天"), "last_days"),
    (re.compile(r"(?:过去|最近|近)\s*(\d+)\s*个?月"), "last_months"),
    (re.compile(r"(?:过去|最近|近)\s*(\d+)\s*年"), "last_years"),
    (re.compile(r"今天|今日"), "today"),
    (re.compile(r"昨天|昨日"), "yesterday"),
    (re.compile(r"前天"), "day_before_yesterday"),
    (re.compile(r"明天|明日"), "tomorrow"),
    (re.compile(r"本周|这周|这一周|这个星期|本星期"), "this_week"),
    (re.compile(r"上周|上星期|上个星期"), "last_week"),
    (re.compile(r"本季度|这个季度|这季度"), "this_quarter"),
    (re.compile(r"上季度|上个季度|上一季度"), "last_quarter"),
    (re.compile(r"本月|这个月|这月"), "this_month"),
    (re.compile(r"上个月|上月|上一个月"), "last_month"),
    (re.compile(r"下个月|下月"), "next_month"),
    (re.compile(r"今年|本年|这年"), "this_year"),
    (re.compile(r"去年|上年|上一年"), "last_year"),
    (re.compile(r"前年"), "year_before_last"),
)


def _resolve(match: re.Match[str], kind: str, today: date) -> DateRange | None:
    """把一个相对日期匹配解析成闭区间；无法解析返回 None（调用方据此拒掉该过滤）。"""
    n = int(match.group(1)) if match.groups() and match.group(1) else 0
    if kind == "days_ago":
        d = today - timedelta(days=n)
        return DateRange(d, d)
    if kind == "months_ago":
        return DateRange(_add_months(today, -n), today)
    if kind == "years_ago":
        return DateRange(date(today.year - n, 1, 1), today)
    if kind == "last_days":
        return DateRange(today - timedelta(days=max(0, n - 1)), today)
    if kind == "last_months":
        return DateRange(_add_months(today, -n), today)
    if kind == "last_years":
        return DateRange(date(today.year - n, 1, 1), today)
    if kind == "today":
        return DateRange(today, today)
    if kind == "yesterday":
        d = today - timedelta(days=1)
        return DateRange(d, d)
    if kind == "day_before_yesterday":
        d = today - timedelta(days=2)
        return DateRange(d, d)
    if kind == "tomorrow":
        d = today + timedelta(days=1)
        return DateRange(d, d)
    if kind in ("this_week", "last_week"):
        # 周一为一周之始（zh-CN 的「本周」就是自然周，不从周日起算）。
        monday = today - timedelta(days=today.weekday())
        if kind == "last_week":
            monday -= timedelta(days=7)
        return DateRange(monday, monday + timedelta(days=6))
    if kind in ("this_quarter", "last_quarter"):
        q_start_month = 3 * ((today.month - 1) // 3) + 1
        start = _month_start(today).replace(month=q_start_month)
        if kind == "last_quarter":
            start = _add_months(start, -3)
        return DateRange(start, _add_months(start, 3) - timedelta(days=1))
    if kind == "this_month":
        start = _month_start(today)
        return DateRange(start, _add_months(start, 1) - timedelta(days=1))
    if kind == "last_month":
        start = _add_months(_month_start(today), -1)
        return DateRange(start, _month_start(today) - timedelta(days=1))
    if kind == "next_month":
        start = _add_months(_month_start(today), 1)
        return DateRange(start, _add_months(start, 1) - timedelta(days=1))
    if kind == "this_year":
        return DateRange(date(today.year, 1, 1), date(today.year, 12, 31))
    if kind == "last_year":
        return DateRange(date(today.year - 1, 1, 1), date(today.year - 1, 12, 31))
    if kind == "year_before_last":
        return DateRange(date(today.year - 2, 1, 1), date(today.year - 2, 12, 31))
    return None


def resolve_relative_dates(
    text: str, *, today: date
) -> tuple[list[tuple[int, int, DateRange]], str]:
    """抽出绝对/相对日期区间。

    返回 ``(命中列表, 归一化后的文本)``；命中列表是 ``(start, end, range)``，按位置
    升序且**互不重叠**——一个更短的模式若落在已被吃掉的区段内就被丢弃，否则
    「2025-03-01」会同时被 ISO 日期和「月」类模式命中两次，归一化文本会被写坏。
    """
    taken: list[tuple[int, int]] = []
    hits: list[tuple[int, int, DateRange]] = []

    def claim(start: int, end: int, rng: DateRange | None) -> None:
        if rng is None:
            return
        if any(s < end and start < e for s, e in taken):  # 与已占区段重叠
            return
        taken.append((start, end))
        hits.append((start, end, rng))

    for m in _RE_ISO_RANGE.finditer(text):
        try:
            claim(m.start(), m.end(), DateRange(date.fromisoformat(m.group(1)), date.fromisoformat(m.group(2))))
        except ValueError:
            continue
    for m in _RE_ISO_MONTH_RANGE.finditer(text):
        y1, m1 = (int(x) for x in m.group(1).split("-"))
        y2, m2 = (int(x) for x in m.group(2).split("-"))
        start, end = date(y1, m1, 1), _add_months(date(y2, m2, 1), 1) - timedelta(days=1)
        claim(m.start(), m.end(), DateRange(start, end) if start <= end else None)
    for m in _RE_ISO_DATE.finditer(text):
        try:
            d = date.fromisoformat(m.group(0))
        except ValueError:
            continue
        claim(m.start(), m.end(), DateRange(d, d))
    for m in _RE_ISO_MONTH.finditer(text):
        y, mo = (int(x) for x in m.group(0).split("-"))
        if not 1 <= mo <= 12:
            continue
        start = date(y, mo, 1)
        claim(m.start(), m.end(), DateRange(start, _add_months(start, 1) - timedelta(days=1)))
    for pattern, kind in _RELATIVE:
        for m in pattern.finditer(text):
            claim(m.start(), m.end(), _resolve(m, kind, today))

    hits.sort(key=lambda h: h[0])
    out = text
    for start, end, rng in reversed(hits):  # 从后往前替换，位置才不会失效
        out = out[:start] + _canonical_range(rng) + out[end:]
    return hits, out


def _canonical_range(rng: DateRange) -> str:
    if rng.start == rng.end:
        return rng.start.isoformat()
    return f"{rng.start.isoformat()}_{rng.end.isoformat()}"


# --------------------------------------------------------------------------- #
# 错误码与产品型号
# --------------------------------------------------------------------------- #
#: 带前缀的编号：``E1001`` / ``ERR-123`` / ``HTTP_404``。无前缀的纯数字**不算**——
#: 它会和「3 个步骤」「第 2 章」这类普通数字混在一起，误判成本远高于收益。
_RE_CODED_TOKEN = re.compile(r"\b([A-Za-z]{1,6})[-_]?(\d{2,8})\b")
#: 判断「这段上下文在问编号」的提示词，用来给同一串字符定性。
_CODE_CONTEXT = re.compile(
    r"错误码|错误代码|报错码|错误编号|故障码|返回码|状态码|error\s*code|status\s*code|\bcode\b",
    re.IGNORECASE,
)

#: 已登记的型号/别名词典。文档要求「未登记的新别名不能命中，需通过 RAG-15 反馈登记」
#: —— 所以这里默认是空的，由调用方注入并随反馈增长，而不是我在这里编一份词表。
DEFAULT_MODEL_LEXICON: frozenset[str] = frozenset()


# --------------------------------------------------------------------------- #
# 引号短语
# --------------------------------------------------------------------------- #
_RE_QUOTED = re.compile(r"[「『\"“”']([^「」『』\"“”']{1,60})[」』\"“”']")


# --------------------------------------------------------------------------- #
# 意图
# --------------------------------------------------------------------------- #
_INTENT_RULES: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"对比|比较|区别|差异|不同|相比|\bvs\b|\bversus\b"), "compare"),
    (re.compile(r"为什么|为何|原因|导致|怎么会|缘何|\bwhy\b|\bcause\b|\breason\b"), "cause_analysis"),
    (re.compile(r"你觉得|你认为|预测|会不会|将来|未来|建议我|我应该"), "no_answer_candidate"),
    (
        re.compile(
            r"是什么|什么意思|代表什么|定义|指的是|含义|表示什么|怎么读|"
            r"\bwhat\s+is\b|\bdefine\b|\bmeaning\b",
            re.IGNORECASE,
        ),
        "lookup",
    ),
)
_DEFAULT_INTENT = "explain"


def _detect_intent(text: str, entities: list[Entity]) -> str:
    for pattern, intent in _INTENT_RULES:
        if pattern.search(text):
            return intent
    # 一串错误码本身就是在「查这个词是什么意思」，规则词没命中时按 lookup。
    if any(e.type == "error_code" for e in entities):
        return "lookup"
    return _DEFAULT_INTENT


_RE_CJK = re.compile(r"[㐀-䶿一-鿿぀-ヿ가-힯]")
_RE_LATIN = re.compile(r"[A-Za-z]")


def detect_language(text: str) -> str:
    """中文／英文／混排。按字数占比判定，不调模型。"""
    cjk = len(_RE_CJK.findall(text))
    latin = len(_RE_LATIN.findall(text))
    if cjk == 0 and latin == 0:
        return "unknown"
    if cjk and latin and min(cjk, latin) / max(cjk, latin) >= 0.2:
        return "mixed"
    return "zh-CN" if cjk >= latin else "en"


# --------------------------------------------------------------------------- #
# 主入口
# --------------------------------------------------------------------------- #
def build_query_plan(
    question: str,
    *,
    today: date | None = None,
    document_ids: list[str] | None = None,
    model_lexicon: frozenset[str] = DEFAULT_MODEL_LEXICON,
    request_id: str | None = None,
    trace_id: str | None = None,
) -> QueryPlan:
    """把问题解析成 ``QueryPlan``。纯函数；``today`` 可注入以便钉死相对日期。

    ``document_ids`` 是调用方（``@文件`` 引用）已经确定的文档范围，它走的是同一套
    白名单校验——它今天能生效正是因为 ``document_id`` 是索引层真的支持下推的字段。
    """
    raw = question or ""
    today = today or date.today()
    request_id = request_id or "req_" + uuid.uuid4().hex
    trace_id = trace_id or new_trace_id()

    rejected: list[RejectedFilter] = []
    entities: list[Entity] = []
    filters: list[QueryFilter] = []

    # ---- 日期 ---------------------------------------------------------------
    date_hits, normalized = resolve_relative_dates(raw, today=today)
    for start, end, rng in date_hits:
        entities.append(
            Entity(type="date_range", value=_canonical_range(rng), span=(start, end))
        )
        # 想问的是「那段时间的资料」，落到文档的有效期/更新时间上。
        # ``updated_at`` 在 schema 里但索引层查不了 —— 记下来，不假装它生效了。
        for field in ("updated_at",):
            spec = FILTER_SCHEMA[field]
            if not spec.indexed:
                rejected.append(
                    RejectedFilter(
                        field=field,
                        op="between",
                        value=rng.as_filter(),
                        raw_text=raw[start:end],
                        reason="field_not_indexed",
                    )
                )
            elif "between" in spec.ops:
                filters.append(QueryFilter(field=field, op="between", value=rng.as_filter()))

    # ---- 错误码 / 型号 ------------------------------------------------------
    in_code_context = bool(_CODE_CONTEXT.search(raw))
    for m in _RE_CODED_TOKEN.finditer(normalized):
        token = m.group(0)
        if token in model_lexicon:
            entities.append(Entity(type="product_model", value=token, span=(m.start(), m.end())))
            continue
        # 有「错误码」这类上下文词，或前缀本身就是 E/ERR/HTTP 这类通用错误前缀时判为
        # 错误码；其余按型号。定性只影响意图与人工归因——两种类型都会打开精确词项检索。
        is_code = in_code_context or m.group(1).upper() in {
            "E", "ERR", "ERROR", "HTTP", "CODE", "STATUS", "SQL", "ORA", "WARN", "FATAL",
        }
        entities.append(
            Entity(
                type="error_code" if is_code else "product_model",
                value=token,
                span=(m.start(), m.end()),
            )
        )

    # ---- 引号短语 -----------------------------------------------------------
    for m in _RE_QUOTED.finditer(normalized):
        phrase = m.group(1).strip()
        if phrase:
            entities.append(
                Entity(type="quoted_phrase", value=phrase, span=(m.start(1), m.end(1)))
            )

    # ---- 调用方给定的文档范围 ----------------------------------------------
    if document_ids:
        spec = FILTER_SCHEMA["document_id"]
        if spec.indexed and "in" in spec.ops:
            filters.append(QueryFilter(field="document_id", op="in", value=list(document_ids)))
        else:  # pragma: no cover - document_id 是白名单里最稳的一列
            rejected.append(
                RejectedFilter(
                    field="document_id",
                    op="in",
                    value=list(document_ids),
                    raw_text="@文件引用",
                    reason="field_not_indexed",
                )
            )

    normalized = _normalize_text(normalized)
    requires_exact = any(e.type in ("error_code", "product_model") for e in entities)

    return QueryPlan(
        request_id=request_id,
        trace_id=trace_id,
        original_query=raw,
        normalized_query=normalized,
        query_variants=[],
        intent=_detect_intent(raw, entities),  # type: ignore[arg-type]
        entities=entities,
        filters=filters,
        rejected_filters=rejected,
        language=detect_language(raw),  # type: ignore[arg-type]
        requires_exact_match=requires_exact,
        plan_version=PLAN_VERSION,
    )


_RE_SPACE = re.compile(r"[ \t　]{2,}")
_RE_TRAILING_PUNCT = re.compile(r"[?？。！!，,、；;：:\s]+$")


def _normalize_text(text: str) -> str:
    """折叠空白、去掉句末标点；**不做同义词替换**（那属于改写，本期关闭）。"""
    out = _RE_SPACE.sub(" ", text or "").strip()
    # 日期归一化会产出 ``2025-01-01_2025-12-31`` 这种区间记号，那个下划线要留住，
    # 所以只剥句末的标点与空白。
    out = _RE_TRAILING_PUNCT.sub("", out)
    return out
