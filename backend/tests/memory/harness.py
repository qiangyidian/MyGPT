"""记忆评估集跑分器（离线，不依赖模型）。

S0 用它量出**当前**抽取器的基线；S2 换成 LLM 抽取后，同一个跑分器给出可比的第二组
数字。``run()`` 同时接受同步与异步抽取器，所以两种实现都能直接接进来。
"""
from __future__ import annotations

import inspect
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

CASES_PATH = Path(__file__).with_name("eval_cases.yaml")

CATEGORIES = ("should_remember", "conflict", "decoy", "attack")

Extractor = Callable[[str], Any | Awaitable[Any]]


@dataclass
class Case:
    id: str
    category: str
    messages: list[dict[str, str]]
    expect_any: list[str] = field(default_factory=list)
    expect_none: bool = False
    attachments: list[dict[str, str]] = field(default_factory=list)

    @property
    def extractor_input(self) -> str:
        """抽取器看到的文本：最后一条 user 消息（attack 类再拼附件正文，见 yaml 头注）。"""
        parts = [self.messages[-1]["content"]]
        parts.extend(a["content"] for a in self.attachments)
        return "\n".join(parts)


@dataclass
class Report:
    total: int
    per_category: dict[str, int]
    recall: float
    false_positive: float
    conflict_supersede: float
    attack_activated: int
    candidates_per_turn: float
    per_case: list[dict[str, Any]] = field(default_factory=list)


def load_cases(path: Path = CASES_PATH) -> list[Case]:
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    out: list[Case] = []
    for item in raw["cases"]:
        out.append(
            Case(
                id=item["id"],
                category=item["category"],
                messages=[dict(m) for m in item["messages"]],
                expect_any=list(item.get("expect_any") or []),
                expect_none=bool(item.get("expect_none", False)),
                attachments=[dict(a) for a in (item.get("attachments") or [])],
            )
        )
    return out


def _content_of(candidate: Any) -> str:
    return str(getattr(candidate, "content", candidate) or "")


async def run(cases: list[Case], extract: Extractor) -> Report:
    """跑一遍语料。``extract(text) -> list[候选]``（可同步可异步）。"""
    per_category: dict[str, int] = dict.fromkeys(CATEGORIES, 0)
    per_case: list[dict[str, Any]] = []
    hits = should_total = decoy_fp = decoy_total = 0
    conflict_hits = conflict_total = attack_activated = total_candidates = 0

    for case in cases:
        per_category[case.category] = per_category.get(case.category, 0) + 1
        produced = extract(case.extractor_input)
        if inspect.isawaitable(produced):
            produced = await produced
        candidates = list(produced or [])
        contents = [_content_of(c) for c in candidates]
        total_candidates += len(candidates)

        if case.category == "should_remember":
            should_total += 1
            hits += int(
                bool(case.expect_any)
                and any(any(kw in c for kw in case.expect_any) for c in contents)
            )
        elif case.category == "decoy":
            decoy_total += 1
            decoy_fp += int(bool(candidates))
        elif case.category == "conflict":
            conflict_total += 1
            # S3 之前没有取代机制，这一项的口径是"新值出现在候选里"；
            # 到 S3 改成"旧条被 invalid_at 失效"（届时同步改 harness 注释与断言）。
            conflict_hits += int(
                bool(case.expect_any)
                and any(any(kw in c for kw in case.expect_any) for c in contents)
            )
        elif case.category == "attack":
            attack_activated += len(candidates)

        per_case.append({"id": case.id, "category": case.category, "candidates": contents})

    return Report(
        total=len(cases),
        per_category=per_category,
        recall=(hits / should_total) if should_total else 0.0,
        false_positive=(decoy_fp / decoy_total) if decoy_total else 0.0,
        conflict_supersede=(conflict_hits / conflict_total) if conflict_total else 0.0,
        attack_activated=attack_activated,
        candidates_per_turn=(total_candidates / len(cases)) if cases else 0.0,
        per_case=per_case,
    )


def report_text(report: Report) -> str:
    lines = [
        "",
        "=== 记忆基线 ===",
        f"用例总数        {report.total}  {report.per_category}",
        f"记住率          {report.recall:.3f}   (should_remember 命中 / 总数)",
        f"误记率          {report.false_positive:.3f}   (decoy 产生候选 / 总数，越低越好)",
        f"冲突新值召回    {report.conflict_supersede:.3f}   (S3 前口径，见 harness 注释)",
        f"攻击产记忆数    {report.attack_activated}   (越低越好，目标 0)",
        f"每轮候选数      {report.candidates_per_turn:.2f}",
        "",
    ]
    for row in report.per_case:
        if row["candidates"]:
            lines.append(f"  {row['id']:<28} -> {row['candidates']}")
    return "\n".join(lines)
