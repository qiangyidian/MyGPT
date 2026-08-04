"""Token-budgeted output spill-to-disk (Codex pattern).

A hook/skill/tool that emits a large blob (e.g. a 50KB security scan) would blow
the context window. Codex spills it: if the output exceeds a per-handler token
budget, the full text is written to a temp file and replaced in-context with a
head/tail preview + a filesystem path to the full artifact. The model keeps a
pointer; the window keeps its budget.

Pure + testable (operates on strings + a writer callable); the temp-file writer
is injected.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Callable

from app.agents.context_compaction import estimate_tokens

_HEAD = 600
_TAIL = 600


@dataclass
class SpillResult:
    in_context: str   # what the model sees (preview, or the full text if not spilled)
    spilled: bool
    path: str | None  # filesystem path to the full artifact when spilled


def maybe_spill(
    text: str,
    *,
    budget_tokens: int,
    write_fn: Callable[[str, str], str] | None = None,
    key: str = "blob",
    spill_dir: str | os.PathLike[str] | None = None,
) -> SpillResult:
    """If ``text`` exceeds ``budget_tokens`` (0 = never spill), write the full text
    via ``write_fn(name, content) -> path`` and return a head/tail preview.

    Default ``write_fn`` writes to ``spill_dir`` (or a temp dir) and returns the path.
    """
    if budget_tokens <= 0 or not text:
        return SpillResult(in_context=text, spilled=False, path=None)
    if estimate_tokens(text) <= budget_tokens:
        return SpillResult(in_context=text, spilled=False, path=None)

    writer = write_fn or _default_writer
    name = f"{key}.txt"
    try:
        path = writer(name, text)
    except Exception:  # noqa: BLE001 — spill is best-effort; never block on it
        return SpillResult(in_context=text, spilled=False, path=None)

    preview = _preview(text)
    return SpillResult(
        in_context=f"{preview}\n\n[完整内容已溢出到磁盘，见：{path}]",
        spilled=True,
        path=path,
    )


def _preview(text: str) -> str:
    if len(text) <= _HEAD + _TAIL:
        return text
    return f"{text[:_HEAD]}\n…[已省略 {len(text) - _HEAD - _TAIL} 字符]…\n{text[-_TAIL:]}"


def _default_writer(name: str, content: str) -> str:
    import tempfile

    d = tempfile.mkdtemp(prefix="hook_spill_")
    p = os.path.join(d, name)
    with open(p, "w", encoding="utf-8") as f:
        f.write(content)
    return p
