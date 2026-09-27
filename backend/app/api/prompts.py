"""提示词库路由：list（搜索 + 分类 + 分页）/ get / create / patch / delete。

归属规则与 ``app/api/models.py`` 一致：用户看得到自己的模板 + 系统预置模板
（``user_id IS NULL``），看不到别人的。**别人的 id 一律 404，不返回 403** ——
403 等于承认「这个 id 存在，只是你不该碰」。预置模板本身对所有人可读（它就在列表
里），所以非管理员改/删它返回 403 不泄露任何信息，这一点也照搬 models.py。
"""
from __future__ import annotations

import logging
import uuid
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import get_current_user
from app.core.like import LIKE_ESCAPE, like_pattern
from app.db import get_db
from app.models import PromptTemplate, User
from app.schemas import (
    PromptTemplateCreate,
    PromptTemplateOut,
    PromptTemplateUpdate,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/prompts", tags=["prompts"])

NOT_FOUND = status.HTTP_404_NOT_FOUND
FORBID = status.HTTP_403_FORBIDDEN

# 分页上限与 knowledge_bases 同一思路：默认值宽松（个人模板量级有限），但绝不
# 允许无上限拉表。搜索走 ILIKE，一页的行数就是序列化成本的天花板。
DEFAULT_PAGE_SIZE = 50
MAX_PAGE_SIZE = 1000

# PATCH 可写的字段。user_id / sort_order 不在其中：归属和预置排序是服务端（以及
# 迁移）的事，客户端能写就等于任何人都能把自己的模板伪装成系统预置。
_PATCHABLE = ("title", "content", "category", "tags", "description")
# 显式 null 有明确含义的字段：清空。其余字段 null 已被 schema 挡掉。
_NULL_TO_EMPTY = ("tags",)


def _to_out(row: PromptTemplate) -> PromptTemplateOut:
    return PromptTemplateOut(
        id=row.id,
        user_id=row.user_id,
        title=row.title,
        content=row.content,
        category=row.category,
        tags=row.tags or [],
        description=row.description,
        sort_order=row.sort_order,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


async def _load_visible(db: AsyncSession, prompt_id: uuid.UUID, user: User) -> PromptTemplate:
    """预置模板人人可读；个人模板只有主人（与管理员）读得到。"""
    row = await db.get(PromptTemplate, prompt_id)
    if row is None:
        raise HTTPException(NOT_FOUND, "Prompt template not found")
    if row.user_id is None:
        return row  # 系统预置
    if row.user_id != user.id and user.role != "admin":
        raise HTTPException(NOT_FOUND, "Prompt template not found")  # 404 而非 403
    return row


def _guard_writable(row: PromptTemplate, user: User) -> None:
    """系统预置模板只有管理员能改/删（它本来就对所有人可见，403 不泄露存在性）。"""
    if row.user_id is None and user.role != "admin":
        raise HTTPException(FORBID, "系统预置模板只能由管理员修改")


def _owned_or_shared(stmt, user: User, scope: str):
    """把列表/分类查询限制在调用者看得见的行上。"""
    if user.role == "admin" and scope == "all":
        return stmt  # 管理员在 all 下看全表
    if scope == "mine":
        return stmt.where(PromptTemplate.user_id == user.id)
    if scope == "preset":
        return stmt.where(PromptTemplate.user_id.is_(None))
    return stmt.where(
        or_(PromptTemplate.user_id == user.id, PromptTemplate.user_id.is_(None))
    )


@router.get("", response_model=list[PromptTemplateOut])
async def list_prompts(
    q: str | None = Query(default=None, max_length=200),
    category: str | None = Query(default=None, max_length=32),
    scope: Literal["all", "mine", "preset"] = Query(default="all"),
    limit: int = Query(default=DEFAULT_PAGE_SIZE, ge=1, le=MAX_PAGE_SIZE),
    offset: int = Query(default=0, ge=0),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> list[PromptTemplateOut]:
    """一页模板列表：预置在前（按 ``sort_order``），其余按最近更新。

    ``q`` 命中标题 / 描述 / 正文，``category`` 精确筛分类，``scope`` 决定看预置、
    自己的还是两者都要。
    """
    stmt = _owned_or_shared(select(PromptTemplate), user, scope)
    if category and category.strip():
        stmt = stmt.where(PromptTemplate.category == category.strip())
    needle = (q or "").strip()
    if needle:
        pattern = like_pattern(needle)
        stmt = stmt.where(
            PromptTemplate.title.ilike(pattern, escape=LIKE_ESCAPE)
            | PromptTemplate.description.ilike(pattern, escape=LIKE_ESCAPE)
            | PromptTemplate.content.ilike(pattern, escape=LIKE_ESCAPE)
        )
    stmt = stmt.order_by(
        # 预置模板置顶，保证「打开就有一套能用的」这个体验不随用户自己存了多少
        # 模板而消失；``id`` 兜底，让同一时间戳的行有确定顺序（分页才不会重复/漏行）。
        PromptTemplate.user_id.is_(None).desc(),
        PromptTemplate.sort_order.asc(),
        PromptTemplate.updated_at.desc(),
        PromptTemplate.id.desc(),
    )
    rows = (await db.execute(stmt.limit(limit).offset(offset))).scalars().all()
    return [_to_out(row) for row in rows]


@router.get("/categories", response_model=list[str])
async def list_prompt_categories(
    scope: Literal["all", "mine", "preset"] = Query(default="all"),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> list[str]:
    """已有分类名（按用量从多到少）—— 筛选标签页的数据源。

    从库里聚合而不是给前端一份常量表：用户自建分类必须能立刻出现在筛选里，运营
    改预置分类也不必发版。
    """
    stmt = _owned_or_shared(
        select(PromptTemplate.category, func.count().label("n")), user, scope
    ).group_by(PromptTemplate.category)
    rows = (await db.execute(stmt)).all()
    # 排序在 Python 侧做：``ORDER BY count(*) DESC`` 在两个方言里都一样，但分类名
    # 的次级排序依赖数据库排序规则（PG 的 collation vs SQLite 的 BINARY），在测试
    # 与生产可能给出不同顺序。这里要的是稳定输出。
    return [name for name, _n in sorted(rows, key=lambda r: (-r[1], r[0]))]


@router.post("", response_model=PromptTemplateOut, status_code=status.HTTP_201_CREATED)
async def create_prompt(
    payload: PromptTemplateCreate,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> PromptTemplateOut:
    row = PromptTemplate(
        user_id=user.id,
        title=payload.title,
        content=payload.content,
        category=payload.category,
        tags=payload.tags,
        description=payload.description,
    )
    db.add(row)
    await db.commit()
    await db.refresh(row)
    return _to_out(row)


@router.get("/{prompt_id}", response_model=PromptTemplateOut)
async def get_prompt(
    prompt_id: uuid.UUID,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> PromptTemplateOut:
    return _to_out(await _load_visible(db, prompt_id, user))


@router.patch("/{prompt_id}", response_model=PromptTemplateOut)
async def update_prompt(
    prompt_id: uuid.UUID,
    payload: PromptTemplateUpdate,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> PromptTemplateOut:
    """改一个模板的若干字段。

    ``model_fields_set`` 让 PATCH 名副其实：没提的字段保持原样，提了且为 null 的
    可空字段被清空。直接读 ``model_dump()`` 会把调用方没提到的字段一起抹掉。
    """
    row = await _load_visible(db, prompt_id, user)
    _guard_writable(row, user)
    provided = payload.model_fields_set & set(_PATCHABLE)
    if not provided:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "没有需要更新的字段")
    for field in provided:
        value = getattr(payload, field)
        if field in _NULL_TO_EMPTY and value is None:
            value = []
        setattr(row, field, value)
    await db.commit()
    await db.refresh(row)
    logger.info(
        "prompt template %s updated: %s", row.id, ", ".join(sorted(provided))
    )
    return _to_out(row)


@router.delete("/{prompt_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_prompt(
    prompt_id: uuid.UUID,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> None:
    row = await _load_visible(db, prompt_id, user)
    _guard_writable(row, user)
    await db.delete(row)
    await db.commit()
