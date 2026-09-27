"""把"这一堆环境变量到底让平台怎么做"算成一份能看的结论（条目 34④）。

后台原先没有这一面：运营只能 ssh 上去读 ``.env``，而 ``.env`` 里的原文和"实际生效"
是两件事 —— 这里有三个开关是 **AND** 出来的（引擎、``python_exec``、指标暴露），一个
是"配了但值为空所以等于没配"（联网搜索），还有一个反过来（``/docs`` 在生产默认关，
与 ``DOCS_ENABLED`` 的默认值无关）。把原文抄给运营，等于让他们在服务器上现场重推一
遍判定式；推错的方向永远是"以为已经开了"。

只做只读：这里的每一项都仍然由环境变量决定，界面上不放开关。理由是这些值多数在
**进程启动时**才被读（runner 工厂、策略对象都是启动期构造），当场改出来的状态与重启
后的状态不一致，比不让人改更危险。真要按工具粒度的即时开关，那是 ``tool_toggles``
那张表负责的（条目 34③）。
"""
from __future__ import annotations

from typing import Any

from app.agents.policies.tool_policy import python_exec_enabled
from app.core.config import env_flag, get_settings
from app.schemas import FeatureFlagOut


def _flag(
    key: str,
    label: str,
    group: str,
    *,
    enabled: bool,
    source: str,
    note: str,
    value: str | None = None,
) -> FeatureFlagOut:
    return FeatureFlagOut(
        key=key,
        label=label,
        group=group,
        enabled=enabled,
        value=value if value is not None else ("true" if enabled else "false"),
        source=source,
        note=note,
    )


def effective_flags(settings: Any | None = None) -> list[FeatureFlagOut]:
    """当前进程读到的一套配置 → 每个开关的生效结论。

    ``settings`` 只为测试留的注入点：默认取真实配置，判定式一律复用生产路径上那同一
    批函数（``python_exec_enabled`` 等），这里**不重写一遍** —— 重写出来的第二套判定
    迟早会和真的那个分叉，而分叉的表现为"面板说开着，代码说没开"。
    """
    s = settings if settings is not None else get_settings()
    flags: list[FeatureFlagOut] = []

    # ---- 多 Agent 引擎 -----------------------------------------------------
    # 与执行侧同一个解析口径：`env_flag` 是本仓库唯一的开关词表（"1"/"true"/"yes"/
    # "on" 为真，认不出来 = 关）。在这里另写一份 in ("1","true",...) 迟早会和
    # `orchestrator._truthy` 分叉，而分叉的表现是面板与路由决策各说一套。
    engine_on = env_flag(getattr(s, "AGENT_WORKFLOW_ENGINE", ""), default=False)
    profiles = [
        p.strip() for p in str(getattr(s, "AGENT_WORKFLOW_ENGINE_PROFILES", "") or "").split(",")
        if p.strip()
    ]
    flags.append(
        _flag(
            "engine_master",
            "多 Agent 引擎总开关",
            "engine",
            enabled=engine_on,
            source="AGENT_WORKFLOW_ENGINE",
            note="关掉时下面这份灰度名单一律不生效：所有请求都走老运行时。",
            value="true" if engine_on else "false",
        )
    )
    flags.append(
        _flag(
            "engine_profiles",
            "引擎灰度名单（已生效的 profile）",
            "engine",
            enabled=engine_on and bool(profiles),
            source="AGENT_WORKFLOW_ENGINE_PROFILES",
            note=(
                "只有「总开关为真」且「profile 在名单里」才走引擎，两个条件是 AND。"
                "名单为空 = 一个都不走，而不是全都走。"
            ),
            value=",".join(profiles) if profiles else "（空）",
        )
    )
    crewai_on = env_flag(getattr(s, "CREWAI_ENABLED", False))
    flags.append(
        _flag(
            "crewai",
            "CrewAI 多 Agent 运行时",
            "engine",
            enabled=crewai_on,
            source="CREWAI_ENABLED",
            note=(
                "这里报的是配置意图。包在本机能不能真 import、请求最后为什么回退到单"
                "模型，看「运行时观测」页的 /api/admin/agent-runtime —— 两者不一致就"
                "是最常见的「以为已经开了」。"
            ),
        )
    )

    # ---- 计费与配额 --------------------------------------------------------
    flags.append(
        _flag(
            "credits_enforced",
            "积分拦截",
            "billing",
            enabled=env_flag(getattr(s, "CREDITS_ENFORCED", False)),
            source="CREDITS_ENFORCED",
            note=(
                "false 是观察模式：照常扣分、照常出流水，但余额不足**不会**挡住对话。"
                "上线初期这是有意的，直接开会把所有余额为 0 的老用户挡在门外。"
            ),
        )
    )
    flags.append(
        _flag(
            "quotas_enabled",
            "配额上限",
            "billing",
            enabled=env_flag(getattr(s, "QUOTAS_ENABLED", False)),
            source="QUOTAS_ENABLED",
            note=(
                "与积分是两套独立叠加的限制：任意一个开着都会挡（错误码分别是 "
                "quota_exceeded 与 insufficient_credits）。配额是管理员配的上限，不随"
                "充值增长、用户看不到。"
            ),
        )
    )

    # ---- 工具与执行面 ------------------------------------------------------
    exec_effective = python_exec_enabled(s)
    flags.append(
        _flag(
            "python_exec",
            "代码执行工具（python_exec）",
            "tools",
            enabled=exec_effective,
            source="ALLOW_PYTHON_EXEC + SANDBOX_MODE + PYTHON_SANDBOX",
            note=(
                "生产是 **AND**：显式放行 **且** 真的配好了隔离后端。历史上这里写过 "
                "or，于是随手填一个 PYTHON_SANDBOX 就能在生产放行任意代码执行。"
                "dev 环境恒为真（本地开发路径）。单个工具要临时停，用后台「工具」页的"
                "启停开关，不要改这里。"
            ),
            value="可执行" if exec_effective else "不可执行",
        )
    )
    workspace_on = env_flag(getattr(s, "WORKSPACE_TOOLS_ENABLED", False))
    flags.append(
        _flag(
            "workspace_tools",
            "工作区文件/命令工具",
            "tools",
            enabled=workspace_on,
            source="WORKSPACE_TOOLS_ENABLED",
            note="关掉时工作区那一组工具根本不会注册进 registry，模型看不见。",
        )
    )
    search_configured = bool(str(getattr(s, "WEB_SEARCH_ENDPOINT", "") or "").strip())
    flags.append(
        _flag(
            "web_search",
            "联网搜索后端",
            "tools",
            enabled=search_configured,
            source="WEB_SEARCH_ENDPOINT",
            note=(
                "留空时回落到 DuckDuckGo 抓取（无凭证可用，但部分网络被墙）。"
                "「工具已启用」与「搜索真的打得通」不是一回事，失败会在来源面板里看见。"
            ),
            value=str(getattr(s, "WEB_SEARCH_ENDPOINT", "") or "").strip() or "（未配置，走默认抓取）",
        )
    )

    # ---- 检索与内容 --------------------------------------------------------
    flags.append(
        _flag(
            "rag_hybrid",
            "混合检索（向量 + 关键词）",
            "rag",
            enabled=env_flag(getattr(s, "RAG_HYBRID", False)),
            source="RAG_HYBRID",
            note=(
                "关掉后只走向量那一路。每个知识库还能在自身设置里单独覆盖 top_k /"
                " 阈值 / 重排，这里的值只是平台默认。"
            ),
        )
    )
    flags.append(
        _flag(
            "speech",
            "语音输入 / 播报",
            "access",
            enabled=env_flag(getattr(s, "SPEECH_ENABLED", False)),
            source="SPEECH_ENABLED",
            note=(
                "关掉时转写与合成接口在任何出站调用之前返回中文 503，"
                "前端按钮据 capabilities 置灰 —— 不是坏了，是没开。"
            ),
        )
    )
    wechat_on = env_flag(getattr(s, "WECHAT_AUTH_ENABLED", False))
    flags.append(
        _flag(
            "wechat_login",
            "微信扫码登录",
            "access",
            enabled=wechat_on,
            source="WECHAT_AUTH_ENABLED + WECHAT_AUTH_APP_SECRET",
            note=(
                "生产里开了却没配 APP_SECRET 的话，后端会**直接拒绝启动**（不是一堆"
                "莫名 503）。老环境里自动注册的微信账号没有可校验的原密码，见迁移 0020。"
            ),
        )
    )

    # ---- 暴露面 -----------------------------------------------------------
    docs_on = bool(s.docs_enabled)
    flags.append(
        _flag(
            "docs",
            "接口文档 /docs 与 /openapi.json",
            "exposure",
            enabled=docs_on,
            source="DOCS_ENABLED",
            note=(
                "生产默认关，与 DOCS_ENABLED 的默认值无关 —— 这是一条按 ENV 收敛的"
                "判定，不是「配了什么就是什么」。"
            ),
            value="暴露" if docs_on else "关闭",
        )
    )
    metrics_on = env_flag(getattr(s, "PROMETHEUS_ENABLED", False))
    metrics_token = bool(str(getattr(s, "METRICS_TOKEN", "") or "").strip())
    flags.append(
        _flag(
            "metrics",
            "Prometheus 指标暴露（/metrics）",
            "exposure",
            enabled=metrics_on and metrics_token,
            source="PROMETHEUS_ENABLED + METRICS_TOKEN",
            note=(
                "两个条件都要成立：开着总开关却没设 token，在生产同样拒绝启动；"
                "抓取的 Bearer 就是 METRICS_TOKEN。这里报的是「两者都在」的结论。"
            ),
            value=(
                "需要 Bearer"
                if metrics_on and metrics_token
                else ("未启用" if not metrics_on else "缺 token")
            ),
        )
    )
    return flags
