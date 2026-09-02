"""permission_profiles + environments (Codex leaf modules, reduced)."""
from __future__ import annotations

import pytest

from app.agents.permission_profiles import (
    PermissionProfileError,
    ProfileDecl,
    catalog,
    recommended_profile_for,
    resolve_profile,
    workspace_requires_approval,
    workspace_requires_write_capability,
    workspace_risk_level,
)
from app.agents.environments import (
    Environment,
    EnvironmentSet,
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


# ---- workspace operation -> risk/approval mapping (Task 8) ----
@pytest.mark.parametrize(
    "op",
    ["workspace_read", "workspace_list", "workspace_search", "workspace_git_status", "workspace_git_diff"],
)
def test_workspace_reads_are_low_risk_and_unapproved(op):
    assert workspace_risk_level(op) == "low"
    assert workspace_requires_approval(op) is False
    assert workspace_requires_write_capability(op) is False
    assert recommended_profile_for(op) == ":read-only"


@pytest.mark.parametrize(
    "op",
    ["workspace_write", "workspace_apply_patch", "workspace_shell", "git_commit", "git_push"],
)
def test_workspace_writes_are_high_risk_and_require_approval(op):
    assert workspace_risk_level(op) == "high"
    assert workspace_requires_approval(op) is True
    assert workspace_requires_write_capability(op) is True
    # Writes/patch/shell reuse the existing :workspace-write built-in (no network).
    assert recommended_profile_for(op) == ":workspace-write"


def test_workspace_write_profile_grants_fs_write_and_shell_but_not_network():
    """The recommended write profile must NOT grant network (only danger-full does)."""
    from app.agents.permission_profiles import _BUILTINS

    pol = _BUILTINS[":workspace-write"]
    assert pol.fs_read and pol.fs_write and pol.shell
    assert pol.network is False
    assert pol.danger_full_access is False


def test_unknown_workspace_operation_fails_closed_high_risk():
    """An unmapped operation is treated as high-risk so it must be audited."""
    assert workspace_risk_level("workspace_nope") == "high"
    assert workspace_requires_approval("workspace_nope") is True


# ---- environments ----


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
