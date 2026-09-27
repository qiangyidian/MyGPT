"""工具启停（条目 34③）：库里的一行怎么就变成"模型看不见、网关不放行"。

三层各测一次，因为它们的失效方式完全不同：

* **执行侧**（``is_tool_allowed``）：这是唯一的安全保证。它坏了 = 关掉的工具照样能
  被调用，而界面显示"已停用"，比根本没有这个开关更糟。
* **装配侧**（``ToolRegistry.list`` / ``openai_schemas``）：它坏了 = 工具还摆在模型
  面前，模型去调用才在网关撞墙 —— 对用户是一次失败的回答。
* **快照侧**（TTL、读失败沿用旧值、跨 session 重读）：它坏了 = 运营按了按钮但某个
  进程永远不认账，或者数据库抖一下把刚关掉的工具重新放开。

快照是**模块级可变状态**（同步读的那一面拿不到 session，见模块 docstring），所以每个
用例前后必须手动清空 —— 不清会串味：上一个用例关掉的工具在下一个用例里仍然看不见，
而那种偶发红根本查不出来。
"""
from __future__ import annotations

import uuid
import time

import pytest

from app.agents.policies.tool_policy import is_tool_allowed
from app.models import ToolToggle
from app.services import tool_toggles
from app.tools.registry_init import get_default_registry
from tests.conftest import TestSessionLocal, auth_headers


@pytest.fixture(autouse=True)
async def _isolate_snapshot(seeded_db):
    """用例之间不共享任何启停状态：进程内快照 + 库里的行都清一遍。

    ``seeded_db`` 是必需的依赖，不是顺手写的：建表发生在那里面，而这个 autouse 用例
    夹具要先于任何查询跑起来，否则第一次 ``delete`` 会打在还不存在的表上。

    快照那半边直接改私有变量 —— 公开 API 里没有"清空"这个动作（生产上不需要），而
    测试要的正是这个不经过数据库的起点。库里那半边更要清：测试库是跨用例共享的一条
    连接，留一行"已停用"会让后面某个用到同一个工具的用例看不见它，那种偶发红几乎
    查不出来。
    """

    def _reset() -> None:
        tool_toggles._disabled = frozenset()
        tool_toggles._states = {}
        tool_toggles._loaded_once = False
        tool_toggles._monotonic_at = 0.0

    async def _wipe_rows() -> None:
        from sqlalchemy import delete

        async with TestSessionLocal() as db:
            await db.execute(delete(ToolToggle))
            await db.commit()

    _reset()
    await _wipe_rows()
    yield
    _reset()
    await _wipe_rows()


#: 优先拿一个"就是它自己"的普通工具来测：``python_exec`` / ``db_query`` 在
#: ``is_tool_allowed`` 里另有环境规则，用它们做断言会把两件事混在一起测。
_PREFERRED = ("datetime_now", "web_search", "http_get", "file_analyze")


def _a_registered_tool() -> str:
    """注册表里挑一个真存在的工具名（写死名字会在加工具时静默失真）。"""
    names = [tool.name for tool in get_default_registry().list(include_disabled=True)]
    assert names, "默认注册表是空的，这个文件测不出任何东西"
    for wanted in _PREFERRED:
        if wanted in names:
            return wanted
    return names[0]


async def test_disable_blocks_execution_advertisement_and_catalog(db_session):
    name = _a_registered_tool()
    registry = get_default_registry()
    assert is_tool_allowed(name, None) is True

    await tool_toggles.set_enabled(
        db_session, tool_name=name, enabled=False, note="上游越权"
    )

    # 执行侧
    assert tool_toggles.is_disabled(name) is True
    assert is_tool_allowed(name, None) is False
    # 装配侧：默认看不见，后台那一份仍然看得见（否则没有第二个地方能把它打开）
    assert name not in [t.name for t in registry.list()]
    assert name in [t.name for t in registry.list(include_disabled=True)]
    assert name not in {s["function"]["name"] for s in registry.openai_schemas()}
    # 理由要留在行上：一周后唯一还能回答"当初为什么关"的地方就是它
    row = await tool_toggles.get_state(db_session, name)
    assert row is not None and row.note == "上游越权" and row.enabled is False

    await tool_toggles.set_enabled(db_session, tool_name=name, enabled=True)
    assert tool_toggles.is_disabled(name) is False
    assert is_tool_allowed(name, None) is True


async def test_re_enable_keeps_the_row_as_evidence(db_session):
    """重新启用不删行：那一行记的是"谁在什么时候把它打开的"。"""
    name = _a_registered_tool()
    admin_id = uuid.uuid4()
    await tool_toggles.set_enabled(
        db_session, tool_name=name, enabled=False, admin_id=admin_id
    )
    await tool_toggles.set_enabled(
        db_session, tool_name=name, enabled=True, admin_id=admin_id
    )

    row = await tool_toggles.get_state(db_session, name)
    assert row is not None
    assert row.enabled is True
    assert row.updated_by == admin_id


async def test_clear_toggle_restores_code_default(db_session):
    """删行 = 回到"没人动过"，与"有一行写着 enabled=true"在语义上并不等价。"""
    name = _a_registered_tool()
    await tool_toggles.set_enabled(db_session, tool_name=name, enabled=False, note="x")
    assert await tool_toggles.clear_toggle(db_session, name) is True
    assert await tool_toggles.get_state(db_session, name) is None
    assert tool_toggles.disabled_tools() == frozenset()
    # 再删一次是幂等的（运营连点两下不该看到 500）
    assert await tool_toggles.clear_toggle(db_session, name) is False


async def test_blank_tool_name_is_refused(db_session):
    """空白名字不进库：主键是名字，收下一个空串就是一行谁也点不到的开关。"""
    with pytest.raises(ValueError):
        await tool_toggles.set_enabled(db_session, tool_name="   ", enabled=False)


async def test_refresh_reads_rows_written_elsewhere():
    """别的进程写的行：靠重读库进快照。

    绕过 :func:`set_enabled` 直接写，是为了模拟"另一个 API 副本按下了按钮"—— 用
    set_enabled 的话本进程的 ``reload_from`` 已经把它塞进快照，测不出重读这条路。
    """
    name = _a_registered_tool()
    async with TestSessionLocal() as db:
        db.add(ToolToggle(tool_name=name, enabled=False, note="另一个进程关的"))
        await db.commit()

    disabled = await tool_toggles.refresh(TestSessionLocal)
    assert name in disabled

    # 收尾删行：测试库是跨用例共享的一条连接，留着一行"已停用"会让后面某个用到这个
    # 工具的用例莫名其妙地看不见它 —— 那种红几乎查不出来。
    async with TestSessionLocal() as db:
        await tool_toggles.clear_toggle(db, name)


async def test_refresh_within_ttl_does_not_read_again(monkeypatch):
    """TTL 内不再问库：刷新点是"每个 run 开始"，没这层闸门就是每条消息一条 SELECT。"""
    calls: list[int] = []
    original = tool_toggles.reload_from

    async def _spy(db):
        calls.append(1)
        return await original(db)

    monkeypatch.setattr(tool_toggles, "reload_from", _spy)
    async with TestSessionLocal() as db:
        await tool_toggles.refresh_with(db)
        await tool_toggles.refresh_with(db)
    assert len(calls) == 1


async def test_stale_snapshot_expires_after_the_ttl():
    """时钟走过 TTL 之后必须重新变"该读一次"。

    只测 ``>=`` 这一侧：另一侧由上一例覆盖。这一条管的是"运营改了、进程却永远不再
    看库"—— 那比多读几次严重得多。
    """
    tool_toggles._store([])
    assert tool_toggles.snapshot_is_stale() is False
    ahead = time.monotonic() + tool_toggles.SNAPSHOT_TTL_SECONDS + 1
    assert tool_toggles.snapshot_is_stale(ahead) is True


async def test_read_failure_keeps_the_previous_snapshot():
    """数据库抖一下不许把刚关掉的工具重新放开，也不许把全部工具当成关掉。"""
    tool_toggles._store([ToolToggle(tool_name="web_search", enabled=False)])
    # 手动把快照标成过期，逼这次真的去读（否则 TTL 会先短路，测的就是另一件事了）。
    tool_toggles._monotonic_at = 0.0

    class _Boom:
        def __call__(self):
            raise RuntimeError("db down")

    disabled = await tool_toggles.refresh(_Boom())
    assert "web_search" in disabled, "读失败退化成全部可用，等于按钮被凭空按第二次"
    assert tool_toggles.is_disabled("web_search") is True


# --------------------------------------------------------------------------- #
# 接口面
# --------------------------------------------------------------------------- #
async def test_admin_catalog_and_toggle_require_admin(client):
    """两个后台端点都不许普通用户碰：前者泄露目录，后者能关掉执行面。"""
    catalog = await client.get("/api/admin/tools", headers=auth_headers())
    assert catalog.status_code == 403

    toggle = await client.post(
        "/api/admin/tools/datetime_now/toggle",
        json={"enabled": False, "note": "不该成功"},
        headers=auth_headers(),
    )
    assert toggle.status_code == 403
    assert tool_toggles.is_disabled("datetime_now") is False


async def test_toggle_rejects_unknown_tool_name(client, admin_token):
    """未知名字 404：主键收下任意字符串 = 允许攒出没人在用的幽灵开关。"""
    resp = await client.post(
        "/api/admin/tools/no_such_tool_xyz/toggle",
        json={"enabled": False, "note": "手滑"},
        headers=auth_headers(admin_token),
    )
    assert resp.status_code == 404
    assert "no_such_tool_xyz" in resp.json()["message"]
    assert "no_such_tool_xyz" not in tool_toggles.disabled_tools()


async def test_toggle_round_trip_through_the_api(client, admin_token):
    name = _a_registered_tool()
    off = await client.post(
        f"/api/admin/tools/{name}/toggle",
        json={"enabled": False, "note": "上游返回越权内容"},
        headers=auth_headers(admin_token),
    )
    assert off.status_code == 200, off.text
    body = off.json()
    assert body["enabled"] is False and body["toggle_note"] == "上游返回越权内容"

    # 后台目录里有它（要能重新打开），用户侧目录里没有（看不见也就点不到）。
    admin_view = await client.get("/api/admin/tools", headers=auth_headers(admin_token))
    assert name in {item["name"] for item in admin_view.json()}
    user_view = await client.get("/api/tools", headers=auth_headers())
    assert name not in {item["name"] for item in user_view.json()}

    on = await client.post(
        f"/api/admin/tools/{name}/toggle",
        json={"enabled": True},
        headers=auth_headers(admin_token),
    )
    assert on.status_code == 200 and on.json()["enabled"] is True
