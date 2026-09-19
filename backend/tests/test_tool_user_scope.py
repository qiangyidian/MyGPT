"""工具层的租户（用户）作用域 + ``POST /api/tools/test`` 收口。

回归的是这条链路：``file_analyze`` 只按参数里的 ``document_id`` 查库，工具侧
没有任何「当前用户」概念，而 ``/api/tools/test`` 又能直接调工具 —— 于是任何登录
用户都能读到别人的文档全文。现在：

  * caller 身份由 :class:`ToolGateway` / :func:`tool_service.test_tool` 绑定为
    工具执行上下文（:mod:`app.tools.context`）；
  * 声明 ``requires_user`` 的工具在没有主体时直接被网关拒绝，拿到主体后还要
    逐行核对归属；
  * 越权返回结构化拒绝（``ok/authorized`` 皆 False），不抛未捕获异常，也不区分
    「不存在」和「不是你的」，避免被当成存在性探针；
  * ``/api/tools/test`` 只放行显式标记 ``user_testable`` 的工具（或管理员）。

测试库说明：``file_analyze`` 用模块级 ``AsyncSessionLocal``，而 conftest 的测试
引擎另有一条连接，所以要把它 patch 到 ``TestSessionLocal``；StaticPool 单连接让
用例数据互相可见，因此断言全部按自己的主键过滤。
"""
from __future__ import annotations

import uuid
from types import SimpleNamespace

import pytest

import app.tools.builtin as builtin_mod
from app.agents.gateway.tool_gateway import ToolGateway
from app.models import Conversation, Document, DocumentChunk, KnowledgeBase, User
from app.services import tool_service
from app.tools.base import BaseTool, ToolRegistry
from app.tools.builtin import DbQueryTool, FileAnalyzeTool
from app.tools.context import current_tool_context, make_tool_context, use_tool_context
from tests.conftest import TestSessionLocal, auth_headers

_SEEDED_USER = uuid.UUID("00000000-0000-0000-0000-000000000001")
_SECRET = "只有主人能看到的正文 top-secret-body"


@pytest.fixture
def builtin_uses_test_db(monkeypatch):
    """让 file_analyze 读到 conftest 那块测试库。"""
    monkeypatch.setattr(builtin_mod, "AsyncSessionLocal", TestSessionLocal)


def _principal(role: str = "user") -> SimpleNamespace:
    """A principal-shaped object: the context only ever reads ``id``/``role``."""
    return SimpleNamespace(id=uuid.uuid4(), role=role)


async def _make_user(db_session, tag: str, *, role: str = "user") -> User:
    suffix = uuid.uuid4().hex[:10]
    user = User(
        email=f"{tag}-{suffix}@example.com",
        username=f"{tag}-{suffix}",
        password_hash="not-a-real-hash",
        role=role,
        is_active=True,
    )
    db_session.add(user)
    await db_session.commit()
    await db_session.refresh(user)
    return user


async def _make_document(db_session, owner_id: uuid.UUID, text: str) -> Document:
    """A KB + one indexed document + its chunk, owned by ``owner_id``."""
    kb = KnowledgeBase(user_id=owner_id, name=f"kb-{uuid.uuid4().hex[:8]}")
    db_session.add(kb)
    await db_session.flush()
    doc = Document(
        knowledge_base_id=kb.id,
        filename="private.md",
        file_path="/nonexistent/private.md",
        file_type=".md",
        file_size=len(text),
        status="indexed",
        chunk_count=1,
    )
    db_session.add(doc)
    await db_session.flush()
    db_session.add(
        DocumentChunk(
            document_id=doc.id,
            knowledge_base_id=kb.id,
            chunk_index=0,
            content=text,
            token_count=1,
            metadata_={},
        )
    )
    await db_session.commit()
    await db_session.refresh(doc)
    return doc


# --------------------------------------------------------------------------- #
# file_analyze：归属校验
# --------------------------------------------------------------------------- #
async def test_file_analyze_denies_without_bound_user():
    """没有绑定主体 = 一个文档都不给读（fail closed，不是「内部调用」）。"""
    res = await FileAnalyzeTool().run(document_id=str(uuid.uuid4()))
    assert res["ok"] is False
    assert res["authorized"] is False
    assert "text" not in res


async def test_file_analyze_denies_foreign_document(db_session, builtin_uses_test_db):
    owner = await _make_user(db_session, "owner")
    intruder = await _make_user(db_session, "intruder")
    doc = await _make_document(db_session, owner.id, _SECRET)

    with use_tool_context(make_tool_context(intruder)):
        res = await FileAnalyzeTool().run(document_id=str(doc.id))

    assert res["ok"] is False and res["authorized"] is False
    assert _SECRET not in str(res)
    assert res.get("filename") is None and res.get("text") is None

    # A foreign id must be indistinguishable from a missing one, or the tool
    # becomes a document-existence oracle.
    with use_tool_context(make_tool_context(intruder)):
        missing = await FileAnalyzeTool().run(document_id=str(uuid.uuid4()))
    assert missing["error"] == res["error"]


async def test_file_analyze_returns_text_for_owner(db_session, builtin_uses_test_db):
    owner = await _make_user(db_session, "owner2")
    doc = await _make_document(db_session, owner.id, _SECRET)

    with use_tool_context(make_tool_context(owner)):
        res = await FileAnalyzeTool().run(document_id=str(doc.id))

    assert res["ok"] is True
    assert _SECRET in res["text"]
    assert res["document_id"] == str(doc.id)


async def test_file_analyze_allows_admin(db_session, builtin_uses_test_db):
    owner = await _make_user(db_session, "owner3")
    doc = await _make_document(db_session, owner.id, _SECRET)

    with use_tool_context(make_tool_context(_principal("admin"))):
        res = await FileAnalyzeTool().run(document_id=str(doc.id))

    assert res["ok"] is True and _SECRET in res["text"]


async def test_file_analyze_malformed_id_is_a_clean_denial():
    """非法 id 不能穿透到驱动层报错（那会是个未捕获异常 / 500）。"""
    with use_tool_context(make_tool_context(_principal())):
        res = await FileAnalyzeTool().run(document_id="1' OR '1'='1")
    assert res["ok"] is False and res["authorized"] is False


# --------------------------------------------------------------------------- #
# gateway：没有主体就不跑需要主体的工具
# --------------------------------------------------------------------------- #
async def test_gateway_blocks_user_scoped_tool_without_principal(db_session):
    conv = Conversation(user_id=_SEEDED_USER, title="scope")
    db_session.add(conv)
    await db_session.flush()

    gw = ToolGateway(
        db_session,
        conversation_id=conv.id,
        assistant_message_id=None,
        run_id=None,
        user=None,
    )
    exec_ = await gw.execute(
        tool_call_id="call-1",
        tool_name="file_analyze",
        arguments={"document_id": str(uuid.uuid4())},
    )
    assert exec_.ok is False
    assert exec_.status == "blocked"
    assert "user scope" in (exec_.error or "")


async def test_gateway_passes_its_user_into_the_tool(db_session):
    """The gateway is the one holding the run's user — it must reach ``run()``."""
    conv = Conversation(user_id=_SEEDED_USER, title="scope2")
    db_session.add(conv)
    await db_session.flush()

    seen: dict[str, object] = {}

    class _Spy(BaseTool):
        name = "ctx_probe"
        description = "records the bound context"
        user_testable = True

        async def run(self, **kwargs):
            ctx = current_tool_context()
            seen["ctx"] = ctx
            return {"ok": True}

    registry = ToolRegistry()
    registry.register(_Spy())
    gw = ToolGateway(
        db_session,
        conversation_id=conv.id,
        assistant_message_id=None,
        run_id=None,
        user=SimpleNamespace(id=_SEEDED_USER, role="user"),
        registry=registry,
    )
    exec_ = await gw.execute(tool_call_id="c", tool_name="ctx_probe", arguments={})
    assert exec_.ok is True
    ctx = seen["ctx"]
    assert ctx is not None and ctx.user_id == _SEEDED_USER and ctx.is_admin is False
    # Binding is per-call: gone again once the gateway returned.
    assert current_tool_context() is None


# --------------------------------------------------------------------------- #
# db_query：全库读，只能管理员
# --------------------------------------------------------------------------- #
async def test_db_query_denied_for_non_admin_caller():
    with use_tool_context(make_tool_context(_principal("user"))):
        res = await DbQueryTool().run(sql="SELECT 1")
    assert res["ok"] is False
    assert res["authorized"] is False
    assert res["rows"] == []
    assert "admin" in res["error"]


async def test_db_query_still_runs_for_admin():
    with use_tool_context(make_tool_context(_principal("admin"))):
        res = await DbQueryTool().run(sql="SELECT 1")
    assert res["ok"] is True


async def test_db_query_without_context_is_unchanged():
    """Unbound = non-request caller (unit test / ops script): only the SQL guard
    applies, so the pre-existing direct-call behaviour keeps working."""
    res = await DbQueryTool().run(sql="DROP TABLE users")
    assert res["ok"] is False and "only read-only" in res["error"]


# --------------------------------------------------------------------------- #
# /api/tools/test：显式白名单 + 主体绑定
# --------------------------------------------------------------------------- #
class _NotTestable(BaseTool):
    name = "internal_only_tool"
    description = "a tool nobody opted in for ad-hoc testing"

    async def run(self, **kwargs):
        return {"ok": True, "ran": True}


@pytest.fixture
def probe_registry(monkeypatch):
    registry = ToolRegistry()
    registry.register(_NotTestable())
    monkeypatch.setattr(tool_service, "get_default_registry", lambda: registry)
    return registry


async def test_test_tool_refuses_tool_not_opted_in(probe_registry):
    res = await tool_service.test_tool(
        "internal_only_tool", {}, user=_principal("user")
    )
    assert res.ok is False
    assert "未开放给用户直接测试" in (res.error or "")
    assert res.result is None


async def test_test_tool_allows_admin_to_test_any_gated_tool(probe_registry):
    res = await tool_service.test_tool(
        "internal_only_tool", {}, user=_principal("admin")
    )
    assert res.ok is True
    assert res.result == {"ok": True, "ran": True}


async def test_test_tool_denies_user_scoped_tool_without_principal():
    res = await tool_service.test_tool(
        "file_analyze", {"document_id": str(uuid.uuid4())}, user=None
    )
    assert res.ok is False
    assert "user scope" in (res.error or "")


async def test_test_tool_runs_file_analyze_as_the_caller(db_session, builtin_uses_test_db):
    """The endpoint's principal is the tenant scope: my own document reads, the
    next user's does not."""
    owner = await _make_user(db_session, "owner4")
    doc = await _make_document(db_session, owner.id, _SECRET)

    ok = await tool_service.test_tool(
        "file_analyze", {"document_id": str(doc.id)}, user=owner
    )
    assert ok.ok is True and _SECRET in ok.result["text"]

    stranger = await _make_user(db_session, "stranger")
    denied = await tool_service.test_tool(
        "file_analyze", {"document_id": str(doc.id)}, user=stranger
    )
    assert denied.ok is True  # the tool answered, it answered "no"
    assert denied.result["authorized"] is False
    assert _SECRET not in str(denied.result)


async def test_test_tool_unknown_tool_still_reports_error():
    res = await tool_service.test_tool("definitely_not_a_tool", {}, user=None)
    assert res.ok is False and "Unknown tool" in (res.error or "")


# --------------------------------------------------------------------------- #
# 路由层：登录 + 限流
# --------------------------------------------------------------------------- #
async def test_tools_test_route_requires_login(client):
    r = await client.post("/api/tools/test", json={"name": "datetime_now", "arguments": {}})
    assert r.status_code == 401


async def test_tools_test_route_allows_safe_tool_for_logged_in_user(client):
    r = await client.post(
        "/api/tools/test",
        json={"name": "datetime_now", "arguments": {}},
        headers=auth_headers(),
    )
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True and body["result"]["unix_ts"]


async def test_tools_test_route_is_rate_limited():
    """ENV=test disables the limiter itself, so assert the wiring is present:
    a route without the dependency is a regression of this fix."""
    from app.api.tools import router

    route = next(r for r in router.routes if getattr(r, "path", "") == "/api/tools/test")
    assert route.dependencies, "/api/tools/test 必须带 per-user 限流依赖"
