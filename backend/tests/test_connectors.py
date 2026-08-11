"""Encrypted tenant-scoped connector definitions + provider catalog.

Pins the Task 9 contract:
  * credentials are encrypted at rest (ciphertext stored, plaintext never);
    the plaintext is only ever materialized in memory via
    ``decrypted_credentials``.
  * credential rotation replaces the ciphertext.
  * enable/disable toggles, and enabling enforces the catalog's minimum OAuth
    scopes.
  * connectors are tenant-scoped: user A cannot see or fetch user B's connector.
  * the provider catalog carries the named providers, each with a non-empty
    minimum-OAuth-scope set and a transport + command/URL.
"""
from __future__ import annotations

import uuid

import pytest
import pytest_asyncio
from sqlalchemy import select

from app.connectors.catalog import PROVIDER_CATALOG, ProviderManifest, get_manifest
from app.connectors.models import Connector
from app.connectors.service import ConnectorService, InsufficientScopesError
from app.core.security import decrypt_secret


def test_catalog_includes_named_providers():
    expected = {
        "github", "gmail", "outlook_mail", "google_calendar", "outlook_calendar",
        "slack", "teams", "notion", "drive", "sharepoint", "box", "atlassian",
        "figma",
    }
    assert expected.issubset(set(PROVIDER_CATALOG.keys()))


def test_catalog_manifests_carry_minimum_scopes():
    for key, manifest in PROVIDER_CATALOG.items():
        assert isinstance(manifest, ProviderManifest)
        assert manifest.name
        assert manifest.transport in {"stdio", "http"}
        assert manifest.command_or_url
        # Every provider must declare at least one required OAuth scope so the
        # minimum-scope gate is meaningful.
        assert manifest.required_scopes, f"provider {key!r} has no required scopes"


def test_get_manifest_returns_typed_entry():
    m = get_manifest("github")
    assert m is not None
    assert m.kind == "github"
    assert any("repo" in s or "gist" in s for s in m.required_scopes)


@pytest_asyncio.fixture
async def two_users(db_session):
    """Two distinct users for tenant-isolation checks.

    Idempotent: the in-memory DB is session-shared (StaticPool), so fixed-UUID
    users would collide on a second test. We get-or-create them and also wipe
    any leftover connectors so list assertions stay stable across tests.
    """
    from app.core.security import hash_password
    from app.models import User

    a_id = uuid.UUID("00000000-0000-0000-0000-0000000000a1")
    b_id = uuid.UUID("00000000-0000-0000-0000-0000000000a2")

    for uid, email, username in [
        (a_id, "conn-a@example.com", "conn-a"),
        (b_id, "conn-b@example.com", "conn-b"),
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
    # Wipe leftover connectors from prior tests so each test starts clean.
    res = await db_session.execute(
        select(Connector).where(Connector.user_id.in_([a_id, b_id]))
    )
    for row in res.scalars().all():
        await db_session.delete(row)
    await db_session.commit()
    a = await db_session.get(User, a_id)
    b = await db_session.get(User, b_id)
    return a, b


@pytest.mark.asyncio
async def test_credentials_encrypted_at_rest(db_session, two_users):
    a, _ = two_users
    svc = ConnectorService(db_session)
    conn = await svc.create(
        user_id=a.id,
        name="my github",
        provider="github",
        credentials={"access_token": "ghp_supersecret_value"},
        oauth_scopes=list(get_manifest("github").required_scopes),
    )
    await db_session.commit()

    # The stored column is ciphertext, never the plaintext token.
    assert "ghp_supersecret_value" not in conn.credentials_enc
    assert conn.credentials_enc  # non-empty

    # And decrypting the stored column round-trips to the original payload.
    assert decrypt_secret(conn.credentials_enc) == '{"access_token": "ghp_supersecret_value"}'

    # The in-memory accessor returns the original dict.
    assert svc.decrypted_credentials(conn) == {"access_token": "ghp_supersecret_value"}


@pytest.mark.asyncio
async def test_rotate_credentials_replaces_ciphertext(db_session, two_users):
    a, _ = two_users
    svc = ConnectorService(db_session)
    conn = await svc.create(
        user_id=a.id,
        name="gh",
        provider="github",
        credentials={"access_token": "old-token"},
        oauth_scopes=list(get_manifest("github").required_scopes),
    )
    await db_session.commit()
    old_enc = conn.credentials_enc

    conn = await svc.rotate_credentials(a.id, conn.id, {"access_token": "new-token"})
    await db_session.commit()

    assert conn.credentials_enc != old_enc
    assert "old-token" not in decrypt_secret(conn.credentials_enc)
    assert svc.decrypted_credentials(conn) == {"access_token": "new-token"}


@pytest.mark.asyncio
async def test_enable_requires_minimum_scopes(db_session, two_users):
    a, _ = two_users
    svc = ConnectorService(db_session)
    conn = await svc.create(
        user_id=a.id,
        name="gh",
        provider="github",
        credentials={"access_token": "tok"},
        oauth_scopes=[],  # missing all required scopes
    )
    await db_session.commit()

    with pytest.raises(InsufficientScopesError):
        await svc.enable(a.id, conn.id)

    # Provide the required scopes; enabling now succeeds.
    conn.oauth_scopes = list(get_manifest("github").required_scopes)
    await db_session.commit()
    conn = await svc.enable(a.id, conn.id)
    await db_session.commit()
    assert conn.enabled is True


@pytest.mark.asyncio
async def test_disable_then_enable_toggles(db_session, two_users):
    a, _ = two_users
    svc = ConnectorService(db_session)
    conn = await svc.create(
        user_id=a.id,
        name="gh",
        provider="github",
        credentials={"access_token": "tok"},
        oauth_scopes=list(get_manifest("github").required_scopes),
        enabled=True,
    )
    await db_session.commit()

    conn = await svc.disable(a.id, conn.id)
    await db_session.commit()
    assert conn.enabled is False

    conn = await svc.enable(a.id, conn.id)
    await db_session.commit()
    assert conn.enabled is True


@pytest.mark.asyncio
async def test_tenant_isolation_user_cannot_see_others(db_session, two_users):
    a, b = two_users
    svc = ConnectorService(db_session)
    conn = await svc.create(
        user_id=a.id,
        name="a-only",
        provider="github",
        credentials={"access_token": "a-token"},
        oauth_scopes=list(get_manifest("github").required_scopes),
    )
    await db_session.commit()

    # User B's list is empty.
    seen_b = await svc.list_for_user(b.id)
    assert seen_b == []

    # User B cannot fetch A's connector by id.
    assert await svc.get_for_user(b.id, conn.id) is None

    # User A sees it.
    seen_a = await svc.list_for_user(a.id)
    assert len(seen_a) == 1
    assert seen_a[0].id == conn.id


@pytest.mark.asyncio
async def test_tenant_isolation_enable_rotate_delete_scoped(db_session, two_users):
    a, b = two_users
    svc = ConnectorService(db_session)
    conn = await svc.create(
        user_id=a.id,
        name="a-only",
        provider="github",
        credentials={"access_token": "a-token"},
        oauth_scopes=list(get_manifest("github").required_scopes),
    )
    await db_session.commit()

    # B cannot enable / rotate / delete A's connector (all no-op-on-miss).
    with pytest.raises(Exception):
        await svc.enable(b.id, conn.id)
    with pytest.raises(Exception):
        await svc.rotate_credentials(b.id, conn.id, {"access_token": "x"})
    await svc.delete(b.id, conn.id)  # delete is idempotent; row stays for A
    await db_session.commit()

    # Row is unchanged and still owned by A.
    res = await db_session.execute(select(Connector).where(Connector.id == conn.id))
    row = res.scalar_one()
    assert row.user_id == a.id
    assert svc.decrypted_credentials(row) == {"access_token": "a-token"}


@pytest.mark.asyncio
async def test_create_snapshots_manifest(db_session, two_users):
    a, _ = two_users
    svc = ConnectorService(db_session)
    conn = await svc.create(
        user_id=a.id,
        name="gh",
        provider="github",
        credentials={"access_token": "tok"},
        oauth_scopes=list(get_manifest("github").required_scopes),
    )
    await db_session.commit()
    # The manifest snapshot records how to reach this provider.
    assert conn.manifest["kind"] == "github"
    assert conn.manifest["transport"] in {"stdio", "http"}
    assert conn.command_or_url
