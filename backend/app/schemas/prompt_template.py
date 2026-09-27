"""提示词模板的请求 / 响应模型（app/api/prompts.py）。"""
from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel, Field, field_validator, model_validator

from app.schemas.common import ORMModel

# 与前端 frontend/src/lib/prompt-library.ts 的 PROMPT_LIMITS 同源：改一边必须改
# 另一边，否则表单填得进去、保存却 422。
DEFAULT_CATEGORY = "通用"  # 与 app/models/prompt_template.py 的列默认值一致
TITLE_MAX = 128
CONTENT_MAX = 20000
DESCRIPTION_MAX = 500
CATEGORY_MAX = 32
TAG_MAX_COUNT = 10
TAG_MAX_LEN = 32


class PromptTemplateFields(BaseModel):
    """Create / Update 共享的字段定义与清洗规则。"""

    title: str = Field(min_length=1, max_length=TITLE_MAX)
    content: str = Field(min_length=1, max_length=CONTENT_MAX)
    category: str = Field(default=DEFAULT_CATEGORY, min_length=1, max_length=CATEGORY_MAX)
    tags: list[str] = Field(default_factory=list, max_length=TAG_MAX_COUNT)
    description: str | None = Field(default=None, max_length=DESCRIPTION_MAX)

    @field_validator("title", "category")
    @classmethod
    def _reject_blank_and_trim(cls, v: str | None) -> str | None:
        # Update 子类把这些字段放开成 ``str | None``，所以 None 也要接得住
        # （None = 本次 PATCH 没提这个字段，由 model_fields_set 判定）。
        if v is None:
            return None
        stripped = v.strip()
        if not stripped:
            raise ValueError("不能为空")
        return stripped

    @field_validator("content")
    @classmethod
    def _content_must_not_be_blank(cls, v: str | None) -> str | None:
        """正文只判空、不 trim：模板首尾的空白是内容的一部分。

        预置模板几乎都以「原文：\\n」这类换行收尾，trim 掉就等于悄悄改了用户存
        下来的提示词。
        """
        if v is None:
            return None
        if not v.strip():
            raise ValueError("不能为空")
        return v

    @field_validator("description")
    @classmethod
    def _blank_description_clears(cls, v: str | None) -> str | None:
        """描述留空白 = 清空，而不是存一串空格进去。"""
        return v.strip() if v is not None and v.strip() else None

    @field_validator("tags")
    @classmethod
    def _clean_tags(cls, v: list[str] | None) -> list[str] | None:
        """去空白、去重；数量由字段上的 ``max_length`` 把关（这里只管单个标签）。"""
        if v is None:
            return None
        cleaned: list[str] = []
        for tag in v:
            item = (tag or "").strip()
            if not item:
                continue
            if len(item) > TAG_MAX_LEN:
                raise ValueError(f"单个标签最长 {TAG_MAX_LEN} 个字符")
            if item not in cleaned:
                cleaned.append(item)
        return cleaned


class PromptTemplateCreate(PromptTemplateFields):
    """POST body —— 永远落在当前用户名下（user_id 不接受客户端传入）。"""


class PromptTemplateUpdate(PromptTemplateFields):
    """PATCH body：省略的字段不动，显式 null 只对可空字段有意义。

    服务端按 ``model_fields_set`` 应用（与 knowledge_bases 的 PATCH 同一套语
    义），所以「客户端到底发了什么」本身就是接口的一部分。
    """

    title: str | None = Field(default=None, min_length=1, max_length=TITLE_MAX)
    content: str | None = Field(default=None, min_length=1, max_length=CONTENT_MAX)
    category: str | None = Field(default=None, min_length=1, max_length=CATEGORY_MAX)
    tags: list[str] | None = Field(default=None, max_length=TAG_MAX_COUNT)
    description: str | None = Field(default=None, max_length=DESCRIPTION_MAX)

    @model_validator(mode="after")
    def _reject_null_for_required_fields(self) -> PromptTemplateUpdate:
        """title/content/category 在列上是 NOT NULL。

        不收口就会把 ``{"title": null}`` 变成一次 setattr(row, "title", None)，
        提交时炸成 500；这里给一句能看懂的中文 422。
        """
        not_nullable = {"title", "content", "category"}
        explicit_nulls = sorted(
            field
            for field in self.model_fields_set
            if field in not_nullable and getattr(self, field) is None
        )
        if explicit_nulls:
            raise ValueError("这些字段不能为 null：" + "、".join(explicit_nulls))
        return self


class PromptTemplateOut(ORMModel):
    id: uuid.UUID
    # null = 系统预置模板（前端据此显示「预置」标记并隐藏编辑入口）。
    user_id: uuid.UUID | None
    title: str
    content: str
    category: str
    tags: list[str] = Field(default_factory=list)
    description: str | None
    sort_order: int = 0
    created_at: datetime
    updated_at: datetime

    @field_validator("tags", mode="before")
    @classmethod
    def _null_tags_as_list(cls, v: list[str] | None) -> list[str]:
        # 列可空（历史行 / 直接 SQL 写入），但对外一律给数组，省掉前端的 ``?? []``。
        return v or []
