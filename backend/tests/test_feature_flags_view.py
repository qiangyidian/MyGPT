"""``effective_flags`` 只许报「算过的结论」——本文件钉住那几处 AND。

这一面的全部价值在于它和判定式同源：面板上写"引擎已开"而请求其实走老运行时，比没有
这个面板更糟（运营会拿着这张截图去争论配置对不对）。所以每个 AND 都要有一条用例把它
按倒在地：

* 引擎 = 总开关 **AND** profile 在灰度名单里；
* ``python_exec``（生产）= 显式放行 **AND** 真有隔离后端 —— 这里历史上写过 ``or``，
  于是随手填一个 ``PYTHON_SANDBOX`` 就能在生产放行任意代码执行；
* ``/metrics`` = 启用 **AND** 有 Bearer token；
* ``/docs`` = 生产默认关，与 ``DOCS_ENABLED`` 的默认值无关。

另有一条结构性的：每一项都必须自带 ``source``（改哪儿）。少这一行的开关会让运营只改
一个变量就以为改完了，而那几个开关恰恰是跨两个变量的。
"""
from __future__ import annotations

from types import SimpleNamespace

from app.services.feature_flags import effective_flags


def _settings(**over: object) -> SimpleNamespace:
    """一套"生产、什么都没开"的桩配置，按用例覆盖单个字段。

    用 SimpleNamespace 而不是真 ``Settings()``：这里测的是**呈现层有没有把判定式算
    对**，不是配置校验。真 Settings 会把每组用例都拖进启动守卫里。
    """
    base: dict[str, object] = {
        "ENV": "prod",
        "is_dev": False,
        "AGENT_WORKFLOW_ENGINE": "true",
        "AGENT_WORKFLOW_ENGINE_PROFILES": "deep_research",
        "CREWAI_ENABLED": True,
        "CREDITS_ENFORCED": False,
        "QUOTAS_ENABLED": False,
        "ALLOW_PYTHON_EXEC": True,
        "SANDBOX_MODE": "local",
        "SANDBOX_DOCKER_IMAGE": "python:3.12-slim",
        "PYTHON_SANDBOX": "",
        "WORKSPACE_TOOLS_ENABLED": False,
        "WEB_SEARCH_ENDPOINT": "",
        "RAG_HYBRID": True,
        "SPEECH_ENABLED": False,
        "WECHAT_AUTH_ENABLED": False,
        "WECHAT_AUTH_APP_SECRET": "",
        "DOCS_ENABLED": None,
        "PROMETHEUS_ENABLED": False,
        "METRICS_TOKEN": "",
        "docs_enabled": False,
    }
    base.update(over)
    return SimpleNamespace(**base)


def _by_key(flags: list) -> dict[str, object]:
    return {f.key: f for f in flags}


def test_engine_requires_master_and_membership():
    flags = _by_key(effective_flags(_settings()))
    assert flags["engine_master"].enabled is True
    assert flags["engine_profiles"].enabled is True

    # 名单为空 = 一个都不走，而不是"全都走"——这是最容易读反的一条。
    blank = _by_key(
        effective_flags(_settings(AGENT_WORKFLOW_ENGINE_PROFILES="  ,  "))
    )
    assert blank["engine_master"].enabled is True
    assert blank["engine_profiles"].enabled is False
    assert "空" in blank["engine_profiles"].value

    # 总开关关掉时，名单再宽也不走引擎。
    off = _by_key(
        effective_flags(
            _settings(AGENT_WORKFLOW_ENGINE="false", AGENT_WORKFLOW_ENGINE_PROFILES="deep_research")
        )
    )
    assert off["engine_master"].enabled is False
    assert off["engine_profiles"].enabled is False


def test_python_exec_needs_opt_in_and_a_real_backend():
    """生产里两个条件缺一个都不算"可执行"。

    只放行（``ALLOW_PYTHON_EXEC=true``）而没有隔离后端时报"可执行"，就是当年那个
    ``or`` 的形态 —— 面板会说"已经开了"，而代码那边其实根本不该放行。
    """
    no_backend = _by_key(effective_flags(_settings(ALLOW_PYTHON_EXEC=True)))
    assert no_backend["python_exec"].enabled is False

    not_opted_in = _by_key(
        effective_flags(
            _settings(
                ALLOW_PYTHON_EXEC=False,
                PYTHON_SANDBOX="docker",
                SANDBOX_MODE="docker",
            )
        )
    )
    assert not_opted_in["python_exec"].enabled is False

    both = _by_key(
        effective_flags(
            _settings(
                ALLOW_PYTHON_EXEC=True,
                PYTHON_SANDBOX="docker",
                SANDBOX_MODE="docker",
            )
        )
    )
    assert both["python_exec"].enabled is True


def test_metrics_needs_both_the_switch_and_a_token():
    enabled_only = _by_key(effective_flags(_settings(PROMETHEUS_ENABLED=True)))
    assert enabled_only["metrics"].enabled is False
    assert "token" in enabled_only["metrics"].value.lower() or "缺" in enabled_only["metrics"].value

    both = _by_key(
        effective_flags(_settings(PROMETHEUS_ENABLED=True, METRICS_TOKEN="x-abc"))
    )
    assert both["metrics"].enabled is True


def test_docs_reflects_the_effective_value_not_the_raw_default():
    """``docs`` 那一项读的是 ``docs_enabled`` 这个属性（按 ENV 收敛过的结论）。

    桩上直接给属性：真实 Settings 里它是 ``DOCS_ENABLED is None → ENV in (dev,test)``，
    呈现层**不许**自己再看一遍 DOCS_ENABLED 原文，否则会推出一个与代码不同的答案。
    """
    flags = _by_key(effective_flags(_settings(docs_enabled=True)))
    assert flags["docs"].enabled is True
    assert flags["docs"].value == "暴露"


def test_search_endpoint_reports_configured_vs_fallback():
    flags = _by_key(effective_flags(_settings()))
    assert flags["web_search"].enabled is False
    assert "默认" in flags["web_search"].value

    with_url = _by_key(effective_flags(_settings(WEB_SEARCH_ENDPOINT="http://searx.local/search")))
    assert with_url["web_search"].enabled is True
    assert with_url["web_search"].value == "http://searx.local/search"


def test_every_flag_names_where_to_change_it():
    """结构性的一条：新加一项却不写 ``source``/``note`` 直接红。

    这一面的意义就是"照着这行去改"，缺了来源等于把运营送回 `.env` 里自己找。
    """
    flags = effective_flags(_settings())
    assert flags, "一个开关都没报出来，这一面就是空面板"
    keys = [f.key for f in flags]
    assert len(set(keys)) == len(keys), "key 重复会让 React 的列表渲染撞 key"
    for item in flags:
        assert item.source.strip(), f"{item.key} 没说改哪个环境变量"
        assert item.note.strip(), f"{item.key} 没解释它实际管到什么"
        assert item.label.strip(), f"{item.key} 没有中文标题"
        assert item.group.strip(), f"{item.key} 没分组，界面上会落进未知组"
