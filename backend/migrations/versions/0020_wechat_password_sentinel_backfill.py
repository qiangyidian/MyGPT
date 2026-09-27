"""把哨兵方案上线前的微信账号回填成「未设置密码」.

Revision ID: 0020_wechat_password_sentinel_backfill
Revises: 0019_ingest_claim_version
Create Date: 2026-09-20

公众号扫码自动注册要合成 ``users`` 的三个 NOT NULL 列。最初的合成法是把
``password_hash`` 写成「一串谁都不知道的随机 argon2 哈希」（设计文档
``docs/superpowers/specs/2026-09-17-wechat-mp-login-design.md`` 第 130 行），后来
换成 ``PASSWORD_NOT_SET`` 哨兵（见 app/core/security.py）—— 因为随机哈希与「本人
设过密码」在字段上完全不可区分，后果是这批人改密码时被要求填写一个永远不可能正确
的原密码，永久卡在「无法设置密码」外面（app/api/auth.py 的改密端点注释里记着这个
缺口）。

这一步把缺口关掉：只改写「确定不可能有本人密码」的行，四个条件同时成立才动。

1. ``email LIKE 'wx_%@wechat.local'`` —— 只有自动注册合成得出这个地址。刻意不用
   「有 wechat_identities 行」当条件：老账号是**主动绑定**微信的（``auth:wechat_bound``），
   它有自己的密码，那条条件会把人家的密码清掉。
2. ``password_hash LIKE '$%'`` —— 长得像一个可解析的散列。已经带哨兵的新行天然排除。
3. **有** ``wechat_identities`` 行 —— 邮箱是合成来的，身份表再确认一次来源，两者都
   被伪造才算命中（防御性冗余，不是主判据）。
4. **没有** ``auth:password_changed`` / ``auth:password_reset`` 审计事件 —— 这两个动作
   是「有人真的设过一个密码」的唯一凭据。``audit_events`` 是只追加的（没有清理任务），
   所以凭据稳定；注销账号会把 ``actor_id`` 置空，但那类行已 ``is_active=false``，改
   写与否都不影响任何人登录。

命中行的原值是一串无人所知的随机哈希：回填不需要备份旧值，因为它本来就不可还原、
也不可用来登录 —— 这正是 :func:`downgrade` 为空操作的理由（回滚等于重新制造缺陷）。
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0020_wechat_password_sentinel_backfill"
down_revision: Union[str, None] = "0019_ingest_claim_version"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# 与 app/core/security.py 的 PASSWORD_NOT_SET 同源：迁移不能 import 应用代码
# （alembic 的 env 里没有应用配置上下文），所以这里抄一份并被测试钉住。
_SENTINEL = "!password-not-set"
# 与 app/services/wechat_login_service.py 的合成邮箱同形（wx_ + openid + 保留域）。
_WX_EMAIL_PATTERN = "wx_%@wechat.local"
# 只有这两类事件代表「有人真设过一个能登录的密码」。
_ACTION_CHANGED = "auth:password_changed"
_ACTION_RESET = "auth:password_reset"

_UPDATE = sa.text(
    """
    UPDATE users
       SET password_hash = :sentinel
     WHERE email LIKE :email_pattern
       AND password_hash LIKE :hash_pattern
       AND EXISTS (
           SELECT 1 FROM wechat_identities wi WHERE wi.user_id = users.id
       )
       AND NOT EXISTS (
           SELECT 1 FROM audit_events ae
            WHERE ae.actor_id = users.id
              AND ae.action IN (:action_changed, :action_reset)
       )
    """
)


def _is_offline() -> bool:
    # 离线（--sql）模式下 get_bind() 是打印用的假连接，反射查不到表；离线脚本本来
    # 就是针对一个已知状态的库生成的，所以那条路径直接出 DDL。
    return bool(op.get_context().as_sql)


def _missing_tables() -> set[str]:
    inspector = sa.inspect(op.get_bind())
    have = set(inspector.get_table_names())
    return {t for t in ("users", "wechat_identities", "audit_events") if t not in have}


def upgrade() -> None:
    params = {
        "sentinel": _SENTINEL,
        "email_pattern": _WX_EMAIL_PATTERN,
        # 通配符作为**绑定值**送出去，SQL 文本里一个 `%` 都不写：psycopg2 在带参数
        # 时要求把字面 `%` 写成 `%%`，写死在语句里就是一颗等着炸的雷。
        "hash_pattern": "$%",
        "action_changed": _ACTION_CHANGED,
        "action_reset": _ACTION_RESET,
    }
    if _is_offline():
        op.execute(_UPDATE, params)
        return
    # 表不全 = 这不是一个跑过完整链路的库（例如只 create_all 了一半），跳过后由
    # 应用层的 password_is_set() 兜住，绝不在此处造表。
    if missing := _missing_tables():
        print(f"[0020] 跳过微信 password_hash 回填：缺表 {sorted(missing)}")
        return
    result = op.get_bind().execute(_UPDATE, params)
    # 数量要留在部署日志里：事后没人能从字段上分辨「回填过」与「本来就是哨兵」。
    print(f"[0020] 微信账号 password_hash 回填为哨兵：{result.rowcount} 行")


def downgrade() -> None:
    # 空操作，且**必须**是空操作：旧值是一串无人所知的随机哈希，既没被备份也无法
    # 还原。写回随机值只会把「改密码要填一个不可能的原密码」这个缺陷重新造出来。
    pass
