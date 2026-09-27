"""提示词库：prompt_templates 建表 + 灌入系统预置模板.

Revision ID: 0018_prompt_templates
Revises: 0017_message_versions
Create Date: 2026-09-20

两件事：

1. 建 ``prompt_templates`` 表。个人模板与系统预置模板同住一表，用
   ``user_id IS NULL`` 区分 —— 与 ``model_configs`` 的 system-wide 行完全同一套
   约定，所以「谁能看见 / 谁能改」在两张表里是同一个问题、同一个答案。
2. 灌 8 条中文预置模板。预置内容**放在数据里而不是代码里**：运营改一句提示词
   应该是改一行数据，而不是改代码 + 发版 + 迁移链路。前端因此也没有硬编码列表
   （见 frontend/src/lib/prompt-library.ts 只做纯逻辑）。

预置用 ``op.bulk_insert`` 而不是把常量塞进 ``app/`` —— 仓库里没有别处这么种子
数据（``app/core/bootstrap.py`` 只按环境变量种 admin 与模型），而模板是要长期
被运营编辑的数据，只有表能承载。

可重复执行（upgrade / downgrade 成对跑）：

* 建表 / 建索引全部按 0015/0016 的守卫式写法来：``0000_initial`` 是用
  ``Base.metadata.create_all`` 按当前模型建表的，空库路径上这张表和它的索引
  天生就在，只有沿链升级的老生产库才需要真的建。
* 种子按「标题已存在的预置行就跳过」过滤，所以重复执行不会灌出两份；``id`` 用
  ``uuid5`` 从 slug 派生而不是随机数，drop 后重跑得到同一批主键。
* ``uq_prompt_templates_system_title`` 是 ``WHERE user_id IS NULL`` 的唯一部分
  索引：把「预置标题不重复」变成数据库约束。个人模板允许重名，所以谓词必须有，
  否则用户存两个「周报」就会被拒。

``created_at`` / ``updated_at`` 不在 bulk_insert 的列里：建表时它们带
``server_default=now()``，交给数据库填既省一次方言差异（--sql 模式下 Python 时间
要 inline_literal），也让这批预置的时间戳与迁移事务一致。
"""
from __future__ import annotations

import uuid
from typing import Any, Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0018_prompt_templates"
down_revision: Union[str, None] = "0017_message_versions"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_TABLE = "prompt_templates"
_INDEXES = (
    "ix_prompt_templates_user_id",
    "ix_prompt_templates_category",
    "uq_prompt_templates_system_title",
)

# slug 只用来派生稳定主键；标题/分类给用户看；sort_order 是预置之间的全局展示
# 顺序 —— 列表接口按「预置优先 + sort_order 升序」排，所以取值必须各不相同，
# 否则同批预置的先后就会随 updated_at 漂移。取值之间留出间隔，是给运营插入新模板
# 用的（改一行数据就行，不必重排整张表）。
_PRESETS: tuple[dict[str, Any], ...] = (
    {
        "slug": "polish-zh",
        "title": "中文润色（不改原意）",
        "category": "写作",
        "description": "修语病、去冗余，但不改立场与原意，适合发稿前过一遍。",
        "tags": ["写作", "中文", "润色"],
        "sort_order": 10,
        "content": (
            "请在不改变原意与立场的前提下润色下面这段中文：修正语病、标点和搭配，"
            "删掉重复表达，长句改短，专业术语保留不动。\n\n"
            "文风：{{文风}}（例如：正式 / 口语 / 学术）\n"
            "目标读者：${读者}\n\n"
            "要求：先给出润色后的全文，再用列表说明你做了哪几处实质性改动。\n\n"
            "原文：\n"
        ),
    },
    {
        "slug": "code-review",
        "title": "代码审查（按严重程度分级）",
        "category": "编程",
        "description": "先说会不会出错，再说该不该改；每条结论都指到具体行。",
        "tags": ["编程", "审查", "质量"],
        "sort_order": 20,
        "content": (
            "请审查下面的 {{语言}} 代码，按优先级输出问题清单：\n"
            "1. 正确性（会直接导致错误结果或异常）\n"
            "2. 边界与错误处理（空值、越界、超时、并发）\n"
            "3. 安全（注入、越权、敏感信息泄漏）\n"
            "4. 性能\n"
            "5. 可读性与一致性\n\n"
            "每条给出：所在行、问题、最小修复方案。没有问题的级别请写「无」，不要"
            "为了凑数编造建议。\n\n"
            "```\n${代码}\n```\n"
        ),
    },
    {
        "slug": "unit-test",
        "title": "补单元测试",
        "category": "编程",
        "description": "正常路径 + 边界 + 异常，用项目已有的测试框架写成可直接运行的文件。",
        "tags": ["编程", "测试"],
        "sort_order": 30,
        "content": (
            "为下面的 {{语言}} 代码补单元测试，使用 ${测试框架} ：\n"
            "- 覆盖正常路径、边界值、异常输入各至少一例\n"
            "- 断言具体行为，不要只断言「不抛异常」\n"
            "- 外部依赖（网络 / 数据库 / 时间）用替身，保证测试可离线重复运行\n\n"
            "直接输出完整测试文件内容，并在末尾列出你未覆盖到的情况。\n\n"
            "```\n${代码}\n```\n"
        ),
    },
    {
        "slug": "translate-keep-terms",
        "title": "翻译（保留术语与格式）",
        "category": "翻译",
        "description": "翻得顺，同时保住代码块、占位符和专有名词不被改动。",
        "tags": ["翻译", "术语", "格式"],
        "sort_order": 40,
        "content": (
            "把下面的内容翻译成{{目标语言}}，要求：\n"
            "- 保持原有 Markdown 结构、代码块、表格、链接不动\n"
            "- 产品名、命令、API 字段、变量名一律不译，需要解释时放在括号里\n"
            "- 专业术语首次出现时附上英文原文\n"
            "- 行文按目标语言的阅读习惯重组，不要逐句对译\n\n"
            "语气：${语气}。只输出译文。\n\n"
            "原文：\n"
        ),
    },
    {
        "slug": "structured-summary",
        "title": "长文结构化摘要",
        "category": "分析",
        "description": "一句话结论 + 要点 + 证据 + 待确认，适合读长文前先拿骨架。",
        "tags": ["分析", "摘要", "阅读"],
        "sort_order": 50,
        "content": (
            "请把下面这篇内容整理成结构化摘要，读者是{{读者身份}}：\n"
            "1. 一句话结论（不超过 40 字）\n"
            "2. 关键要点：最多 ${条数} 条，每条一句话，按重要性排序\n"
            "3. 支撑证据：数据 / 事实 / 引述，并标注来自第几段\n"
            "4. 反方与局限：作者没交代的风险\n"
            "5. 待确认事项：需要追问或另找资料才能定论的点\n\n"
            "只依据原文，原文没有的信息请写「原文未提及」，不要自行补充。\n\n"
            "正文：\n"
        ),
    },
    {
        "slug": "meeting-minutes",
        "title": "会议纪要与行动项",
        "category": "办公",
        "description": "把流水记录整理成决议 + 行动项（负责人、截止时间）+ 待议。",
        "tags": ["办公", "会议", "行动项"],
        "sort_order": 60,
        "content": (
            "下面是 {{会议主题}} 的记录，请整理成会议纪要：\n"
            "- 会议结论：已达成结论的事项，逐条列出\n"
            "- 行动项表格：事项 / 负责人 / 截止时间 / 依赖，负责人或时间没提到的"
            "写「待定」，不要编造\n"
            "- 分歧与待议：谁和谁在什么点上没达成一致\n"
            "- 遗漏检查：记录里没有负责人但有明确待办的事项\n\n"
            "参会人：${参会人}。输出用简体中文，控制在 400 字以内。\n\n"
            "记录：\n"
        ),
    },
    {
        "slug": "feynman-explain",
        "title": "费曼式讲解一个概念",
        "category": "学习",
        "description": "类比 + 例子 + 自测题，检验是否真的听懂了。",
        "tags": ["学习", "讲解", "类比"],
        "sort_order": 70,
        "content": (
            "请用{{读者水平}}能听懂的方式解释「${概念}」：\n"
            "1. 一句不使用任何术语的定义\n"
            "2. 一个日常生活的类比，并说明这个类比在哪里会失效\n"
            "3. 一个具体的小例子（有真实数字或场景）\n"
            "4. 三个常见误解及纠正\n"
            "5. 三道自测题（先出题，答案单独放在最后）\n\n"
            "如果这个概念在不同语境下含义不同，请先说明你讲的是哪一种。\n"
        ),
    },
    {
        "slug": "copy-variants",
        "title": "短文案多变体",
        "category": "营销",
        "description": "同一卖点写 5 个角度，各标适用渠道，便于直接投放对比。",
        "tags": ["营销", "文案", "投放"],
        "sort_order": 80,
        "content": (
            "为「{{产品名}}」写 5 条短文案，分别从痛点、对比、使用场景、数据、情绪"
            "五个角度切入。约束：\n"
            "- 每条不超过 ${字数} 字，一条只说一件事\n"
            "- 目标人群：${人群}；不允许绝对化用语（最、第一、100%）\n"
            "- 每条后面标注最适合的投放渠道与理由（一句话）\n\n"
            "已知的产品事实（只能引用这些，缺的信息请留空而不是编造）：\n"
        ),
    },
)


def _is_postgres() -> bool:
    return op.get_bind().dialect.name == "postgresql"


def _uuid_type() -> sa.types.TypeEngine:
    """PG 上原生 UUID，其余方言（SQLite 开发/测试库）退化成 CHAR(36)。"""
    if _is_postgres():
        from sqlalchemy.dialects.postgresql import UUID

        return UUID(as_uuid=True)
    return sa.String(length=36)


def _json_type() -> sa.types.TypeEngine:
    if _is_postgres():
        from sqlalchemy.dialects.postgresql import JSONB

        return JSONB()
    return sa.JSON()


def _tables() -> set[str]:
    return set(sa.inspect(op.get_bind()).get_table_names())


def _existing_indexes(table: str, tables: set[str]) -> set[str]:
    if table not in tables:
        return set()
    return {ix["name"] for ix in sa.inspect(op.get_bind()).get_indexes(table)}


def _preset_id(slug: str) -> Any:
    """从 slug 派生的稳定主键（重跑 upgrade / downgrade 对都落在同一批 id 上）。"""
    return uuid.uuid5(uuid.NAMESPACE_URL, f"mygpt:prompt-preset:{slug}")


def _seed_table() -> sa.Table:
    """bulk_insert 用的临时表：只声明要写的列。

    类型必须与建表语句一致，否则 PG 上 JSONB 列收到的是普通文本参数、UUID 列收到
    的是字符串，插入会失败或被隐式转成意外的形态。
    """
    return sa.table(
        _TABLE,
        sa.column("id", _uuid_type()),
        sa.column("user_id", _uuid_type()),
        sa.column("title", sa.String(length=128)),
        sa.column("content", sa.Text()),
        sa.column("category", sa.String(length=32)),
        sa.column("tags", _json_type()),
        sa.column("description", sa.Text()),
        sa.column("sort_order", sa.Integer()),
    )


def _seed_presets(tables: set[str]) -> None:
    if _TABLE not in tables:
        return
    preset = _seed_table()
    bind = op.get_bind()
    # 「已存在同名预置就跳过」：新库上这条守卫意味着本函数只灌一次；老库里管理员
    # 改过或删过的预置也不会被一次 upgrade 复原（那等于悄悄覆盖运营的决定）。
    have = {
        row[0]
        for row in bind.execute(
            sa.text("SELECT title FROM prompt_templates WHERE user_id IS NULL")
        )
    }
    rows: list[dict[str, Any]] = []
    for item in _PRESETS:
        if item["title"] in have:
            continue
        rows.append(
            {
                "id": _preset_id(str(item["slug"])),
                "user_id": None,  # NULL = 系统预置
                "title": item["title"],
                "content": item["content"],
                "category": item["category"],
                "tags": item["tags"],
                "description": item["description"],
                "sort_order": item["sort_order"],
            }
        )
    if rows:
        op.bulk_insert(preset, rows)


def upgrade() -> None:
    tables = _tables()

    if _TABLE not in tables:
        op.create_table(
            _TABLE,
            sa.Column("id", _uuid_type(), primary_key=True),
            # NULL = 系统预置模板（人人可读，仅管理员可写）。
            sa.Column("user_id", _uuid_type(), nullable=True),
            sa.Column("title", sa.String(length=128), nullable=False),
            sa.Column("content", sa.Text(), nullable=False),
            sa.Column(
                "category", sa.String(length=32), nullable=False, server_default="通用"
            ),
            sa.Column("tags", _json_type(), nullable=True),
            sa.Column("description", sa.Text(), nullable=True),
            sa.Column(
                "sort_order", sa.Integer(), nullable=False, server_default="0"
            ),
            sa.Column(
                "created_at",
                sa.DateTime(timezone=True),
                server_default=sa.func.now(),
                nullable=False,
            ),
            sa.Column(
                "updated_at",
                sa.DateTime(timezone=True),
                server_default=sa.func.now(),
                nullable=False,
            ),
            sa.ForeignKeyConstraint(
                ["user_id"], ["users.id"], ondelete="CASCADE"
            ),
        )

    tables = _tables()
    have = _existing_indexes(_TABLE, tables)
    # 名字与模型里的 index=True / __table_args__ 完全一致：两条建库路径
    # （create_all 与沿链迁移）必须落到同一套 schema 上。
    if "ix_prompt_templates_user_id" not in have:
        op.create_index("ix_prompt_templates_user_id", _TABLE, ["user_id"])
    if "ix_prompt_templates_category" not in have:
        op.create_index("ix_prompt_templates_category", _TABLE, ["category"])
    if "uq_prompt_templates_system_title" not in have:
        op.create_index(
            "uq_prompt_templates_system_title",
            _TABLE,
            ["title"],
            unique=True,
            postgresql_where=sa.text("user_id IS NULL"),
            sqlite_where=sa.text("user_id IS NULL"),
        )

    _seed_presets(tables)


def downgrade() -> None:
    tables = _tables()
    if _TABLE not in tables:
        return
    have = _existing_indexes(_TABLE, tables)
    for name in reversed(_INDEXES):
        if name in have:
            op.drop_index(name, table_name=_TABLE)
    # 表连带其中的预置行一起消失：个人模板是用户数据，只回滚这一步就是把「删掉
    # 用户存的模板」藏进一次静默的 downgrade —— 需要回滚请显式导出该表。
    op.drop_table(_TABLE)
