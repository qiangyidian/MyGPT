"""工具目录启停：tool_toggles 建表.

Revision ID: 0021_tool_toggles
Revises: 0020_wechat_pw_sentinel
Create Date: 2026-09-20

后台的「工具」页原来只是一份**只读目录**：运营看得见某个工具有多危险（``python_exec``
``db_query``），却没有任何手段在出事之后把它停掉 —— 唯一的开关是改 ``.env`` 再发一次版，
而"要不要停"恰恰是最需要当场决定的那一刻。这张表就是那个开关的落点。

为什么是表而不是环境变量：

* 生效范围要跨进程。API 与 worker 是两个进程（k8s 上还是多个副本），env 只在启动时
  读一次；改配置 = 重启 = 正在跑的 run 被打断，而停一个工具不该有这种代价。
* 需要审计。谁在什么时候停了 ``web_search``、写没写原因，是要能被事后追问的（同文件
  的 ``updated_by`` 外键 + ``audit_events`` 里的 ``tools:toggle`` 事件）。

**行只在被运营动过的时候存在**：没有行 = 按代码默认（启用）。这让"恢复默认"是删一行
而不是记住一堆开关键，也让这张表的大小等于"被改过的工具数"而不是"工具总数"。

守卫式建表按 0015/0016/0017 的写法：``0000_initial`` 用 ``Base.metadata.create_all``
按当前模型建表，空库路径上这张表天生就在，只有沿链升级的老库才需要真的建。
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0021_tool_toggles"
down_revision: Union[str, None] = "0020_wechat_pw_sentinel"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_TABLE = "tool_toggles"


def _has_table(table: str) -> bool:
    return table in sa.inspect(op.get_bind()).get_table_names()


def upgrade() -> None:
    if _has_table(_TABLE):
        return
    op.create_table(
        _TABLE,
        # 主键就是工具名：一个工具一条状态，天然幂等（重复 upsert 不会攒出历史行）。
        # 名字来自注册表而不是 UUID，是为了让「运营关掉的是哪个工具」在 dump 里直接
        # 可读 —— 这张表迟早要被人拿肉眼看。
        sa.Column("tool_name", sa.String(length=64), primary_key=True),
        sa.Column(
            "enabled",
            sa.Boolean(),
            nullable=False,
            server_default=sa.true(),
        ),
        # 关掉一个工具的理由（"上游 API 泄露内容" / "计费异常"）。可空，但没有它的话
        # 一周后没人记得为什么，而"重新打开"就变成一次无人负责的赌博。
        sa.Column("note", sa.Text(), nullable=True),
        # ON DELETE SET NULL：注销账号不该连带抹掉这条运营记录 —— 它是对**平台**发生过
        # 什么的证据，不是那个人的资产。
        sa.Column(
            "updated_by",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
    )


def downgrade() -> None:
    # 只有表本身。删掉它等于把所有工具恢复成默认启用 —— 那是**放开**一个当初被人工
    # 关掉的执行面，方向与安全直觉相反，所以真实部署里要不要跑这一步必须是人决定的。
    if _has_table(_TABLE):
        op.drop_table(_TABLE)
