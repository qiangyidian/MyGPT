"""会话导出（条目 26）：把一整段对话交给用户自己保存。

只做导出，不做公开分享链接——分享意味着要处理匿名访问、撤销、爬虫和残留快照，
风险远大于「用户想备份自己的对话」这个需求本身。

两种格式：
  * ``markdown`` —— 人在编辑器/Notion 里继续用。排版尽量与界面一致：无支撑的
    ``[source N]`` 标记同样被剥掉（复用 :func:`sanitize_unbacked_source_markers`），
    置信度分数不外泄（那是调试字段），附件/步骤/来源各自成块。
  * ``json`` —— 程序再处理的无损格式：会话字段 + 全部消息（含 metadata）。

导出不受详情接口的 200 条窗口限制：数据所有权的意思就是「全部」，分页留给界面。
"""
from __future__ import annotations

import json
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from urllib.parse import quote

from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Conversation, Message
from app.rag.citations import sanitize_unbacked_source_markers
from app.schemas import ConversationOut, MessageOut

__all__ = [
    "EXPORT_FORMATS",
    "ExportedConversation",
    "export_conversation",
    "render_markdown",
    "render_json",
]

EXPORT_FORMATS = ("markdown", "json")
#: 导出格式 → 文件名后缀。`markdown` 的后缀是 `.md`，不是 `.markdown`。
_FORMAT_SUFFIXES = {"markdown": "md", "json": "json"}
#: 导出文件里每条步骤结果/片段最多带多少字符：来源摘要是给人看上下文的，不是把
#: 整库复制一遍。
_SNIPPET_CHARS = 300
_STEP_RESULT_CHARS = 200
_ROLE_LABELS = {
    "user": "用户",
    "assistant": "助手",
    "system": "系统",
    "tool": "工具",
}
#: metadata.status 里表示「这一轮没正常完成」的取值。
_BROKEN_STATUSES = {"error", "aborted", "cancelled", "truncated", "budget"}


@dataclass(frozen=True)
class ExportedConversation:
    """One rendered export plus the headers the route needs."""

    filename: str
    media_type: str
    body: str

    def content_disposition(self) -> str:
        """RFC 5987 pair: an ASCII fallback plus the UTF-8 original.

        Conversation titles are normally Chinese and HTTP headers are latin-1,
        so a bare ``filename="中文.md"`` aborts the response with
        UnicodeEncodeError.
        """
        safe = self.filename.replace('"', "").replace("\r", "").replace("\n", "")
        ascii_name = safe.encode("latin-1", "replace").decode("latin-1")
        return f'attachment; filename="{ascii_name}"; filename*=UTF-8\'\'{quote(safe)}'


def _now() -> datetime:
    return datetime.now(UTC)


def _stamp(value: datetime | None) -> str:
    if value is None:
        return ""
    if value.tzinfo is None:  # SQLite hands back naive datetimes.
        value = value.replace(tzinfo=UTC)
    return value.astimezone(UTC).strftime("%Y-%m-%d %H:%M UTC")


def _human_size(raw: Any) -> str:
    try:
        size = float(raw)
    except (TypeError, ValueError):
        return ""
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return f"{size:.0f}{unit}" if unit == "B" else f"{size:.1f}{unit}"
        size /= 1024
    return ""


def _clip(text: str, limit: int) -> str:
    collapsed = " ".join((text or "").split())
    return collapsed if len(collapsed) <= limit else collapsed[: limit - 1] + "…"


def _as_list(raw: Any) -> list[dict[str, Any]]:
    return [item for item in raw if isinstance(item, dict)] if isinstance(raw, list) else []


def _slug(title: str) -> str:
    cleaned = "".join(ch if ch.isalnum() or ch in "-_ " else "" for ch in title).strip()
    cleaned = cleaned.replace(" ", "-")
    return cleaned[:60] or "conversation"


# --------------------------------------------------------------------------- #
# Markdown rendering.
# --------------------------------------------------------------------------- #
def render_markdown(
    conv: Conversation, messages: Sequence[Message], *, exported_at: datetime | None = None
) -> str:
    """Render one conversation as a standalone Markdown document."""
    when = exported_at or _now()
    rows = list(messages)
    lines: list[str] = [f"# {conv.title or '新对话'}", ""]
    lines.append(f"- 导出时间：{_stamp(when)}")
    lines.append(f"- 消息数：{len(rows)}")
    models = sorted({m.model_name for m in rows if m.model_name})
    if models:
        lines.append(f"- 模型：{'、'.join(models)}")
    if conv.parent_conversation_id:
        lines.append("- 来源：分支对话")
    lines.append("")

    system_prompt = (conv.system_prompt or "").strip()
    if system_prompt:
        lines += ["## 会话设定", "", "```text", system_prompt, "```", ""]

    lines += ["---", ""]
    for message in rows:
        lines += _render_message(message)
    return "\n".join(lines).rstrip() + "\n"


def _render_message(message: Message) -> list[str]:
    meta = message.metadata_ or {}
    label = _ROLE_LABELS.get(message.role, message.role)
    header = [f"## {label} · {_stamp(message.created_at)}"]
    if message.model_name:
        header[0] += f" · {message.model_name}"
    status_value = str(meta.get("status") or "")
    if status_value in _BROKEN_STATUSES:
        header[0] += f" ·（本轮未完成：{status_value}）"

    citations = _as_list(meta.get("citations"))
    # Same rule the chat UI applies: a marker with no backing citation is a
    # fabrication and must not survive into the exported text.
    body, _changed = sanitize_unbacked_source_markers(message.content or "", len(citations))

    lines = [header[0], ""]
    attachments = _as_list(meta.get("attachments")) if message.role == "user" else []
    if attachments:
        lines += [f"- 附件：{_describe_attachments(attachments)}", ""]
    lines += [(body.rstrip() or "（空）"), ""]

    steps = _as_list(meta.get("steps"))
    if steps:
        lines += ["**执行步骤**", ""]
        lines += [f"{i}. {_describe_step(step)}" for i, step in enumerate(steps, start=1)]
        lines.append("")
    artifacts = _as_list(meta.get("artifacts"))
    if artifacts:
        names = [str(a.get("filename") or a.get("id") or "") for a in artifacts]
        lines += [f"**产物**：{'、'.join(n for n in names if n)}", ""]
    if citations:
        lines += ["**来源**", ""]
        lines += [f"{i}. {_describe_citation(c)}" for i, c in enumerate(citations, start=1)]
        lines.append("")
    return lines


def _describe_attachments(items: list[dict[str, Any]]) -> str:
    parts = []
    for item in items:
        name = str(item.get("filename") or "未命名附件")
        size = _human_size(item.get("size_bytes"))
        note = f"（{size}）" if size else ""
        failed = str(item.get("status") or "") in {"failed", "error"}
        parts.append(f"{name}{note}{'［解析失败］' if failed else ''}")
    return "、".join(parts)


def _describe_step(step: dict[str, Any]) -> str:
    title = str(step.get("title") or step.get("name") or "步骤")
    status = str(step.get("status") or "done")
    mark = {"done": "完成", "running": "进行中", "failed": "失败", "skipped": "跳过"}.get(
        status, status
    )
    tool = step.get("tool") if isinstance(step.get("tool"), dict) else {}
    result = _clip(str(tool.get("resultPreview") or step.get("result") or ""), _STEP_RESULT_CHARS)
    line = f"[{mark}] {title}"
    return f"{line} —— {result}" if result else line


def _describe_citation(citation: dict[str, Any]) -> str:
    name = str(citation.get("document_name") or "未知来源")
    kind = str(citation.get("source_type") or "document")
    bits = [f"{name}（{'网页' if kind == 'web' else '附件' if kind == 'attachment' else '文档'}）"]
    page = citation.get("page_number")
    if isinstance(page, int) and page > 0:
        bits.append(f"第 {page} 页")
    url = str(citation.get("url") or "")
    if url:
        bits.append(url)
    snippet = _clip(str(citation.get("snippet") or ""), _SNIPPET_CHARS)
    # The score stays out on purpose: it is a debug field, and a raw 0.63 read
    # back as "63% 可信" would be a claim this product never made.
    line = " · ".join(bits)
    return f"{line}：{snippet}" if snippet else line


# --------------------------------------------------------------------------- #
# JSON rendering (lossless).
# --------------------------------------------------------------------------- #
def render_json(
    conv: Conversation, messages: Sequence[Message], *, exported_at: datetime | None = None
) -> str:
    """Lossless export: the API's own shapes, so a consumer can re-import them."""
    when = exported_at or _now()
    payload: dict[str, Any] = {
        "format_version": 1,
        "kind": "mygpt.conversation.export",
        "exported_at": when.isoformat(),
        "conversation": ConversationOut.model_validate(conv).model_dump(mode="json"),
        "messages": [_message_payload(m) for m in messages],
    }
    return json.dumps(payload, ensure_ascii=False, indent=2)


def _message_payload(message: Message) -> dict[str, Any]:
    try:
        return MessageOut.model_validate(message).model_dump(mode="json")
    except ValidationError:  # a legacy row missing a newer column must not block export
        return {
            "id": str(message.id),
            "conversation_id": str(message.conversation_id),
            "role": message.role,
            "content": message.content,
            "metadata": message.metadata_ or {},
            "model_name": message.model_name,
            "created_at": _stamp(message.created_at),
        }


# --------------------------------------------------------------------------- #
# Entry point used by the router.
# --------------------------------------------------------------------------- #
async def export_conversation(
    db: AsyncSession, conv: Conversation, *, fmt: str = "markdown"
) -> ExportedConversation:
    """Render one owned conversation for download. Unknown ``fmt`` → ValueError."""
    normalized = (fmt or "markdown").strip().lower()
    if normalized not in EXPORT_FORMATS:
        raise ValueError(f"不支持的导出格式：{fmt}")
    rows = list(
        (
            await db.execute(
                select(Message)
                .where(Message.conversation_id == conv.id)
                .order_by(Message.created_at.asc(), Message.id.asc())
            )
        )
        .scalars()
        .all()
    )
    stem = _slug(conv.title or "") or "conversation"
    # The id suffix keeps two exports of same-titled conversations distinct and
    # stops a crafted title from producing a path-looking filename.
    suffix = _FORMAT_SUFFIXES[normalized]
    filename = f"{stem}-{str(uuid.uuid4())[:8]}.{suffix}"
    if normalized == "json":
        return ExportedConversation(
            filename=filename,
            media_type="application/json; charset=utf-8",
            body=render_json(conv, rows),
        )
    return ExportedConversation(
        filename=filename,
        media_type="text/markdown; charset=utf-8",
        body=render_markdown(conv, rows),
    )
