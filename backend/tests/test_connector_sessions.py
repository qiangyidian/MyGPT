"""Per-tenant connector→session lifecycle (Task 9 follow-up).

Pins the contract that a user's ENABLED connectors contribute their MCP tools
to THAT user's run-scoped ToolRegistry (through the same ``merge_mcp_tools`` /
``McpToolWrapper`` gateway path the static ``MCP_SERVERS`` config uses), while:

  * **Tenant isolation** — user A's enabled connectors never reach user B's run.
  * **Disable → disappears** — disabling a connector drops its tools from
    subsequent runs.
  * **Graceful shutdown** — every opened session is closed at run end (no leak).
  * **Graceful degradation** — a connector whose session fails to initialize is
    isolated (log + skip); the run and the other connectors are unaffected.
  * **Credential hygiene** — credentials are decrypted in-memory only (placed in
    the session env) and NEVER logged.

No real subprocess / HTTP server: a fake session is injected via the manager's
``session_factory`` (mirrors how ``test_mcp_gateway_integration.py`` fakes the
server, but at the session seam so no process is spawned).
"""
from __future__ import annotations

import logging
import uuid

import pytest
import pytest_asyncio
from sqlalchemy import select

from app.agents.mcp_client import merge_mcp_tools
from app.connectors.catalog import get_manifest
from app.connectors.models import Connector
from app.connectors.service import ConnectorService
from app.connectors.sessions import ConnectorSessionManager
from app.tools.registry_init import get_default_registry


# --------------------------------------------------------------------------- #
# Fake session: stands in for a real McpSession so no subprocess / HTTP is
# spawned. Records initialize / call / close so the tests can assert lifecycle.
# --------------------------------------------------------------------------- #
class _FakeSession:
    """A test double for :class:`McpSession` over one connector's config."""

    def __init__(
        self,
        config,
        *,
        tools: list[dict] | None = None,
        fail_init: bool = False,
        call_result: dict | None = None,
    ) -> None:
        self._config = config
        self._tools = list(tools or [])
        self._fail_init = fail_init
        self._call_result = call_result
        # Lifecycle observability.
        self.closed = False
        self.initialize_calls = 0
        self.call_log: list[tuple[str, dict]] = []

    async def initialize(self) -> dict:
        self.initialize_calls += 1
        if self._fail_init:
            raise RuntimeError(f"fake: initialize failed for {self._config.name}")
        return {"serverInfo": {"name": self._config.name}}

    async def list_tools(self):
        from app.agents.mcp_transport import McpToolDef

        return [
            McpToolDef(
                name=t["name"],
                description=t.get("description", ""),
                input_schema=t.get("input_schema", {}),
            )
            for t in self._tools
        ]

    async def call_tool(self, name, arguments, *, timeout=None):
        self.call_log.append((name, dict(arguments)))
        return self._call_result if self._call_result is not None else {
            "ok": True,
            "name": name,
            "args": dict(arguments),
        }

    async def close(self) -> None:
        self.closed = True


def _fake_session_factory(
    tools_by_provider: dict[str, list[dict]],
    *,
    fail_providers: set[str] | None = None,
):
    """Build a session_factory keyed by provider (parsed from config.name).

    ``config.name`` is ``f"{provider}:{connector_id}"`` (see
    :meth:`ConnectorService.build_server_config`), so the leading segment is the
    provider key.
    """
    fail = set(fail_providers or [])

    def _make(config):
        provider = str(config.name).split(":", 1)[0]
        return _FakeSession(
            config,
            tools=tools_by_provider.get(provider, []),
            fail_init=(provider in fail),
        )

    return _make


# Per-provider fake tool catalogs. One tool each so presence/absence is crisp.
_GITHUB_TOOLS = [
    {
        "name": "search_repos",
        "description": "search github repos",
        "input_schema": {
            "type": "object",
            "properties": {"query": {"type": "string"}},
            "required": ["query"],
        },
    }
]
_SLACK_TOOLS = [
    {
        "name": "post_message",
        "description": "post a slack message",
        "input_schema": {
            "type": "object",
            "properties": {"channel": {"type": "string"}},
            "required": ["channel"],
        },
    }
]


@pytest_asyncio.fixture
async def two_users(db_session):
    """Two distinct users for tenant-isolation checks (get-or-create, idempotent)."""
    from app.core.security import hash_password
    from app.models import User

    a_id = uuid.UUID("00000000-0000-0000-0000-0000000000b1")
    b_id = uuid.UUID("00000000-0000-0000-0000-0000000000b2")

    for uid, email, username in [
        (a_id, "sess-a@example.com", "sess-a"),
        (b_id, "sess-b@example.com", "sess-b"),
    ]:
        if await db_session.get(User, uid) is None:
            db_session.add(
                User(
                    id=uid,
                    email=email,
                    username=username,
                    password_hash=hash_password("Aa1234567"),
                    role="user",
                    is_active=True,
                )
            )
    # Wipe leftover connectors so each test starts clean.
    res = await db_session.execute(
        select(Connector).where(Connector.user_id.in_([a_id, b_id]))
    )
    for row in res.scalars().all():
        await db_session.delete(row)
    await db_session.commit()
    a = await db_session.get(User, a_id)
    b = await db_session.get(User, b_id)
    return a, b


async def _create_enabled(db_session, user_id, *, provider, name, credentials):
    """Helper: create + commit an enabled connector for ``user_id``."""
    svc = ConnectorService(db_session)
    conn = await svc.create(
        user_id=user_id,
        name=name,
        provider=provider,
        credentials=credentials,
        oauth_scopes=list(get_manifest(provider).required_scopes),
        enabled=True,
    )
    await db_session.commit()
    return conn


# --------------------------------------------------------------------------- #
# 1. Tenant isolation: a user's enabled connector contributes its tool to THAT
#    user's run registry only; another user's connector never appears.
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_enabled_connector_tool_merges_into_run_registry(db_session, two_users):
    a, _ = two_users
    conn = await _create_enabled(
        db_session,
        a.id,
        provider="github",
        name="a-gh",
        credentials={"access_token": "a-gh-token"},
    )
    factory = _fake_session_factory({"github": _GITHUB_TOOLS})
    mgr = ConnectorSessionManager(db_session, session_factory=factory)

    user_registry = await mgr.open_for_user(a.id)
    try:
        # The user's connector exposed exactly one tool, namespaced under the
        # build_server_config server name ``f"{provider}:{id}"``.
        expected_server = f"github:{conn.id}"
        tool_names = {t.namespaced_name for t in user_registry.catalog.all()}
        assert tool_names == {f"mcp__{expected_server}__search_repos"}, tool_names

        # Merging into a fresh run registry surfaces the namespaced tool so the
        # model is offered it (same merge seam as the static MCP path).
        run_registry = get_default_registry()
        merged = merge_mcp_tools(run_registry, mcp_registry=user_registry)
        assert merged == 1
        names = {t.name for t in run_registry.list()}
        assert f"mcp__{expected_server}__search_repos" in names
    finally:
        await mgr.close_all(user_registry)


@pytest.mark.asyncio
async def test_tenant_isolation_other_user_connector_absent(db_session, two_users):
    a, b = two_users
    # A has github; B has slack. They must NOT cross-contaminate.
    conn_a = await _create_enabled(
        db_session,
        a.id,
        provider="github",
        name="a-gh",
        credentials={"access_token": "a-gh-token"},
    )
    conn_b = await _create_enabled(
        db_session,
        b.id,
        provider="slack",
        name="b-slack",
        credentials={"access_token": "b-slack-token"},
    )

    factory = _fake_session_factory(
        {"github": _GITHUB_TOOLS, "slack": _SLACK_TOOLS}
    )
    mgr = ConnectorSessionManager(db_session, session_factory=factory)

    # A's registry has ONLY github tools.
    reg_a = await mgr.open_for_user(a.id)
    try:
        a_names = {t.namespaced_name for t in reg_a.catalog.all()}
        assert any(n.startswith("mcp__github:") for n in a_names), a_names
        assert not any("slack" in n for n in a_names), f"B's slack leaked into A: {a_names}"
    finally:
        await mgr.close_all(reg_a)

    # B's registry has ONLY slack tools.
    reg_b = await mgr.open_for_user(b.id)
    try:
        b_names = {t.namespaced_name for t in reg_b.catalog.all()}
        assert any(n.startswith("mcp__slack:") for n in b_names), b_names
        assert not any("github" in n for n in b_names), f"A's github leaked into B: {b_names}"
    finally:
        await mgr.close_all(reg_b)


# --------------------------------------------------------------------------- #
# 2. Disable -> tool disappears from subsequent runs.
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_disable_connector_drops_tool_from_next_run(db_session, two_users):
    a, _ = two_users
    conn = await _create_enabled(
        db_session,
        a.id,
        provider="github",
        name="a-gh",
        credentials={"access_token": "tok"},
    )
    factory = _fake_session_factory({"github": _GITHUB_TOOLS})
    mgr = ConnectorSessionManager(db_session, session_factory=factory)

    # Enabled -> tool present.
    reg = await mgr.open_for_user(a.id)
    await mgr.close_all(reg)
    assert any(
        n.endswith("__search_repos") for n in (
            t.namespaced_name for t in reg.catalog.all()
        )
    )

    # Disable the connector.
    svc = ConnectorService(db_session)
    await svc.disable(a.id, conn.id)
    await db_session.commit()

    # Next run -> no connector tools.
    reg2 = await mgr.open_for_user(a.id)
    try:
        assert reg2.catalog.count() == 0, "disabled connector still contributed tools"
        run_registry = get_default_registry()
        merged = merge_mcp_tools(run_registry, mcp_registry=reg2)
        assert merged == 0
    finally:
        await mgr.close_all(reg2)


# --------------------------------------------------------------------------- #
# 3. Graceful shutdown: close_all closes every opened session (no leak).
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_close_all_closes_every_session(db_session, two_users):
    a, b = two_users
    await _create_enabled(
        db_session, a.id, provider="github", name="a-gh", credentials={"access_token": "a"}
    )
    await _create_enabled(
        db_session, a.id, provider="slack", name="a-slack", credentials={"access_token": "a2"}
    )

    opened_sessions: list[_FakeSession] = []

    def _tracking_factory(config):
        provider = str(config.name).split(":", 1)[0]
        tools = {"github": _GITHUB_TOOLS, "slack": _SLACK_TOOLS}.get(provider, [])
        sess = _FakeSession(config, tools=tools)
        opened_sessions.append(sess)
        return sess

    mgr = ConnectorSessionManager(db_session, session_factory=_tracking_factory)
    reg = await mgr.open_for_user(a.id)
    assert len(opened_sessions) == 2, "expected one session per enabled connector"

    await mgr.close_all(reg)
    assert all(s.closed for s in opened_sessions), "a session was not closed (leak)"


# --------------------------------------------------------------------------- #
# 4. Graceful degradation: a connector whose session fails to initialize is
#    isolated (skipped); the others still contribute their tools and the run is
#    unaffected.
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_failing_connector_isolated_others_succeed(db_session, two_users, caplog):
    a, _ = two_users
    await _create_enabled(
        db_session, a.id, provider="github", name="a-gh", credentials={"access_token": "ok"}
    )
    await _create_enabled(
        db_session, a.id, provider="slack", name="a-slack", credentials={"access_token": "ok2"}
    )

    # github initializes fine; slack's session blows up at initialize().
    factory = _fake_session_factory(
        {"github": _GITHUB_TOOLS, "slack": _SLACK_TOOLS},
        fail_providers={"slack"},
    )
    mgr = ConnectorSessionManager(db_session, session_factory=factory)

    with caplog.at_level(logging.WARNING, logger="app.connectors.sessions"):
        reg = await mgr.open_for_user(a.id)
    try:
        names = {t.namespaced_name for t in reg.catalog.all()}
        # github tool present (the healthy connector).
        assert any(n.startswith("mcp__github:") for n in names), names
        # slack tool absent (the failing connector was skipped, not crashed).
        assert not any("slack" in n for n in names), names
    finally:
        await mgr.close_all(reg)

    # The failure was logged (isolated, not silent).
    assert any("slack" in rec.getMessage() or "failed" in rec.getMessage().lower()
               for rec in caplog.records), "failing connector was not logged"


# --------------------------------------------------------------------------- #
# 5. Credential hygiene: decrypted creds reach the session env (in-memory) but
#    are NEVER logged.
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_credentials_decrypted_in_memory_only_never_logged(
    db_session, two_users, caplog
):
    a, _ = two_users
    plaintext = "super-secret-ghp-token-XYZ"
    await _create_enabled(
        db_session,
        a.id,
        provider="github",
        name="a-gh",
        credentials={"access_token": plaintext},
    )

    captured_envs: list[dict] = {}

    def _capturing_factory(config):
        provider = str(config.name).split(":", 1)[0]
        captured_envs[provider] = dict(config.env)
        return _FakeSession(config, tools=_GITHUB_TOOLS)

    mgr = ConnectorSessionManager(db_session, session_factory=_capturing_factory)

    with caplog.at_level(logging.DEBUG, logger="app.connectors.sessions"):
        reg = await mgr.open_for_user(a.id)
    try:
        # The decrypted plaintext reached the session env (in-memory).
        assert captured_envs.get("github", {}).get("ACCESS_TOKEN") == plaintext
        # The plaintext never appears in any log record.
        for rec in caplog.records:
            assert plaintext not in rec.getMessage(), (
                f"plaintext credential leaked into log: {rec.getMessage()!r}"
            )
    finally:
        await mgr.close_all(reg)
