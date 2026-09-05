"""Rule-based candidate-memory extraction (B7).

After a chat turn completes, cheaply extract 0–3 *candidate* user memories from
the user's message. No model calls — patterns only — so auto-proposal adds
negligible latency. Everything is stored INACTIVE via the existing
``MemoryService.propose`` (exact-content dedup, opt-in activation); nothing
enters the prompt until the user enables it in settings.

Pattern philosophy: precision over recall. A wrongly-proposed memory costs the
user a review action; a missed one costs nothing (the user can add it in
settings). So the patterns match explicit self-statements only.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

# ---- patterns (ordered; first match wins per message) ---------------------- #

_PREFERENCE_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"(?:我)?(?:喜欢|偏好|更喜欢|习惯)(?:用|使用|以)?(?:的是)?(?P<body>[^。！？,，;；]{2,60})"), "preference"),
    (re.compile(r"(?:请|以后|下次)(?:总是|一律|记得|帮)?我?(?:用|使用|以)(?P<body>[^。！？,，;；]{2,60})"), "preference"),
    (re.compile(r"(?:不要|别)(?:给)?我?(?P<body>[^。！？,，;；]{2,60})"), "preference"),
]

_FACT_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"(?:我是|我叫|我在|我的)(?P<body>[^。！？,，;；]{2,60})"), "fact"),
    (re.compile(r"(?:我们|我们的)(?:公司|团队|项目|产品)(?:是|叫|叫做)?(?P<body>[^。！？,，;；]{2,60})"), "fact"),
]

_MIN_CHARS = 4
_MAX_CANDIDATES = 3


@dataclass
class MemoryCandidate:
    content: str
    memory_type: str
    confidence: float


def extract_memory_candidates(user_message: str) -> list[MemoryCandidate]:
    """Extract up to 3 candidate memories from one user message.

    Returns normalized, declarative restatements ("喜欢 X" → "用户喜欢 X") with
    a conservative confidence; the caller persists them as INACTIVE rows.
    """
    text = (user_message or "").strip()
    if len(text) < _MIN_CHARS:
        return []

    out: list[MemoryCandidate] = []
    seen: set[str] = set()

    def _add(body: str, mtype: str, confidence: float) -> None:
        body = body.strip().rstrip("。，！？.;")
        if len(body) < _MIN_CHARS:
            return
        content = f"用户{body}" if not body.startswith("用户") else body
        if content in seen:
            return
        seen.add(content)
        out.append(MemoryCandidate(content=content, memory_type=mtype, confidence=confidence))

    for pattern, mtype in _PREFERENCE_PATTERNS:
        m = pattern.search(text)
        if m:
            body = m.group("body")
            if mtype == "preference":
                verb = "喜欢"
                for kw, v in (("更喜欢", "更偏好"), ("习惯", "习惯使用"), ("喜欢", "喜欢")):
                    if kw in m.group(0):
                        verb = v
                        break
                _add(f"{verb}{body}", "preference", 0.6)
            break  # one preference per message

    for pattern, _mtype in _FACT_PATTERNS:
        m = pattern.search(text)
        if m:
            prefix = m.group(0)[: m.start("body") - m.start()] if "body" in m.groupdict() else ""
            # Rebuild "我是X" → "是X" style fact with the original lead.
            lead = m.group(0)
            body = m.group("body")
            _add(f"{lead[:2]}{body}" if lead[:2] in ("我是", "我叫", "我在") else body, "fact", 0.5)
            break  # one fact per message

    return out[:_MAX_CANDIDATES]
