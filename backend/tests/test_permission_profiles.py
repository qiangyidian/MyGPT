"""permission_profiles + environments (Codex leaf modules, reduced)."""
from __future__ import annotations

import pytest

from app.agents.permission_profiles import (
    PermissionProfileError,
    ProfileDecl,
    catalog,
    resolve_profile,
)
from app.agents.environments import (
    Environment,
    EnvironmentSet,
    environment_fragment,
    environments_instructions_fragment,
)


# ---- permission_profiles ----
def test_builtin_resolves_directly():
    p = resolve_profile(":read-only", {})
    assert p.fs_read is True and p.fs_write is False and p.network is False


def test_custom_extends_builtin_with_overrides():
    decls = {"strict": ProfileDecl(name="strict", extends=":workspace-write",
                                   overrides={"network": True})}
    p = resolve_profile("strict", decls)
    assert p.fs_write is True and p.shell is True  # inherited from workspace-write
    assert p.network is True  # child override


def test_extends_chain_custom_over_custom():
    decls = {
        "base": ProfileDecl(name="base", extends=":read-only", overrides={"fs_write": True}),
        "locked": ProfileDecl(name="locked", extends="base", overrides={"fs_write": False}),
    }
    p = resolve_profile("locked", decls)
    assert p.fs_read is True and p.fs_write is False  # leaf wins


def test_cycle_detected():
    decls = {
        "a": ProfileDecl(name="a", extends="b"),
        "b": ProfileDecl(name="b", extends="a"),
    }
    with pytest.raises(PermissionProfileError):
        resolve_profile("a", decls)


def test_unknown_profile_raises():
    with pytest.raises(PermissionProfileError):
        resolve_profile("nope", {})


def test_catalog_allowlist_gate():
    decls = {"strict": ProfileDecl(name="strict", extends=":read-only")}
    entries = catalog(decls, allowed=[":read-only", "strict"])  # danger-full NOT allowed
    by_name = {e.name: e for e in entries}
    assert by_name[":read-only"].allowed is True
    assert by_name["strict"].allowed is True
    assert by_name[":danger-full-access"].allowed is False


# ---- environments ----
def test_environment_fragment_labeled_per_env():
    f = environment_fragment(Environment(env_id="dev", cwd="/repo", status="starting", shell="bash"))
    body = f.render()
    assert "<environment_context_dev>" in body
    assert "id: dev" in body and "cwd: /repo" in body and "status: starting" in body


def test_environments_instructions_fragment():
    body = environments_instructions_fragment().render()
    assert "starting" in body and "继续推进" in body


def test_envset_ready_and_starting_split():
    es = EnvironmentSet()
    es.add(Environment(env_id="a", status="ready"))
    es.add(Environment(env_id="b", status="starting"))
    assert {e.env_id for e in es.ready()} == {"a"}
    assert {e.env_id for e in es.starting()} == {"b"}


async def test_envset_wait_until_ready_returns_when_marked():
    es = EnvironmentSet()
    es.add(Environment(env_id="c", status="starting"))
    es.mark("c", "ready")
    e = await es.wait_until_ready("c", timeout_s=0.5)
    assert e is not None and e.env_id == "c"


async def test_envset_wait_until_ready_missing_returns_none():
    es = EnvironmentSet()
    assert await es.wait_until_ready("ghost", timeout_s=0.2) is None
