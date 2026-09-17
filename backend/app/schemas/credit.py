"""积分与兑换码的请求 / 响应 DTO。"""
from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel, Field

from app.schemas.common import ORMModel


class CreditAccountOut(BaseModel):
    balance: int
    lifetime_granted: int
    lifetime_consumed: int
    # 观察模式开关，供前端展示"当前不拦截"的提示。
    enforced: bool


class RedeemRequest(BaseModel):
    code: str = Field(min_length=1, max_length=64)


class RedeemResultOut(BaseModel):
    credits_added: int
    balance: int
    batch_name: str


class LedgerEntryOut(ORMModel):
    id: uuid.UUID
    delta: int
    balance_after: int
    reason: str
    ref_type: str | None = None
    note: str | None = None
    created_at: datetime


class LedgerPageOut(BaseModel):
    entries: list[LedgerEntryOut]
    next_cursor: str | None = None


# ---- 管理端 --------------------------------------------------------------- #

class RedeemBatchCreate(BaseModel):
    name: str = Field(min_length=1, max_length=128)
    # 正数校验放在服务层（redeem_batch_invalid_credits → 400），Pydantic 的
    # gt=0 会把这类业务错误变成 422 validation，前端无法识别。
    credits_per_code: int
    count: int
    expires_at: datetime | None = None
    note: str | None = None


class RedeemBatchOut(ORMModel):
    id: uuid.UUID
    name: str
    credits_per_code: int
    expires_at: datetime | None = None
    note: str | None = None
    created_at: datetime


class RedeemBatchCreateOut(BaseModel):
    batch: RedeemBatchOut
    # 明文码，仅此一次。
    codes: list[str]


class RedeemBatchProgressOut(BaseModel):
    batch: RedeemBatchOut
    total: int
    redeemed: int
    void: int
    active: int


class RedeemCodeOut(ORMModel):
    id: uuid.UUID
    code_prefix: str
    status: str
    redeemed_by: uuid.UUID | None = None
    redeemed_at: datetime | None = None
    created_at: datetime


class VoidBatchOut(BaseModel):
    voided: int


class CreditAccountRowOut(BaseModel):
    user_id: uuid.UUID
    email: str
    username: str
    balance: int
    lifetime_granted: int
    lifetime_consumed: int


class CreditAdjustRequest(BaseModel):
    user_id: uuid.UUID
    # 非零；上限由 CREDITS_MAX_ADJUST 在服务层再校验一次。
    delta: int
    note: str | None = Field(default=None, max_length=500)
