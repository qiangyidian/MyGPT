"""Permission profiles -> compiled capability policy (Codex pattern, reduced).

Codex lets users declare ``[permissions.strict] extends = ":workspace-write"``
with filesystem/network overrides, resolves the inheritance chain (cycle-safe),
and compiles to a runtime sandbox policy. A catalog gates which profiles are
even selectable (allowlist × sandbox-mode × deny-read).

This is the portable, reduced core: ``extends`` inheritance + the reserved
``:`` built-in prefix (``:read-only`` / ``:workspace-write`` / ``:danger-full-access``)
+ compile to a :class:`CapabilityPolicy` + an allowlist-gated catalog. Globs,
MITM, and per-path deny maps are deferred (the hooks/exec-policy/network-policy
modules cover the fine-grained cases).
"""
from __future__ import annotations

from dataclasses import dataclass, field, replace

_BUILTIN_PREFIX = ":"


@dataclass(frozen=True)
class CapabilityPolicy:
    """The compiled, boolean-capability view of a permission profile."""

    fs_read: bool = True
    fs_write: bool = False
    network: bool = False
    shell: bool = False
    danger_full_access: bool = False

    def merge_override(self, **overrides: bool) -> "CapabilityPolicy":
        """Child overrides win (True/False explicitly set on the child)."""
        return replace(self, **{k: v for k, v in overrides.items()})


_BUILTINS: dict[str, CapabilityPolicy] = {
    ":read-only": CapabilityPolicy(fs_read=True, fs_write=False, network=False, shell=False),
    ":workspace-write": CapabilityPolicy(fs_read=True, fs_write=True, network=False, shell=True),
    ":danger-full-access": CapabilityPolicy(
        fs_read=True, fs_write=True, network=True, shell=True, danger_full_access=True
    ),
}


@dataclass
class ProfileDecl:
    name: str
    extends: str | None = None       # a built-in (":...") or another custom name
    overrides: dict = field(default_factory=dict)  # capability bools the child sets


class PermissionProfileError(ValueError):
    pass


def resolve_profile(name: str, decls: dict[str, ProfileDecl]) -> CapabilityPolicy:
    """Resolve a profile's full capability policy by walking its ``extends`` chain.

    Built-in parents (``:``-prefixed) are the roots; custom profiles may extend a
    built-in or another custom profile. Cycle-safe.
    """
    chain: list[str] = []
    cur: str | None = name
    while cur is not None:
        if cur in chain:
            raise PermissionProfileError(f"permission profile cycle: {' -> '.join(chain + [cur])}")
        chain.append(cur)
        if cur.startswith(_BUILTIN_PREFIX):
            if cur not in _BUILTINS:
                raise PermissionProfileError(f"unknown built-in profile: {cur}")
            break  # built-ins are roots
        decl = decls.get(cur)
        if decl is None:
            raise PermissionProfileError(f"unknown profile: {cur}")
        cur = decl.extends

    # Walk root -> leaf applying overrides (leaf wins).
    policy = CapabilityPolicy(fs_read=False, fs_write=False, network=False, shell=False)
    for n in reversed(chain):
        if n.startswith(_BUILTIN_PREFIX):
            policy = _BUILTINS[n]
        else:
            policy = policy.merge_override(**decls[n].overrides)
    return policy


@dataclass
class ProfileCatalogEntry:
    name: str
    policy: CapabilityPolicy
    allowed: bool


def catalog(
    decls: dict[str, ProfileDecl],
    *,
    allowed: list[str] | None = None,
) -> list[ProfileCatalogEntry]:
    """List all selectable profiles with an ``allowed`` flag (allowlist gate).

    ``allowed=None`` means everything is selectable; otherwise only named
    profiles (plus all built-ins) are ``allowed=True``. A managed policy can use
    this to forbid ``:danger-full-access`` by simply not listing it.
    """
    allow = set(allowed) if allowed is not None else None
    out: list[ProfileCatalogEntry] = []

    def _gate(n: str) -> bool:
        if allow is None:
            return True
        return n in allow

    for name, pol in _BUILTINS.items():
        out.append(ProfileCatalogEntry(name, pol, _gate(name)))
    for name, decl in decls.items():
        out.append(ProfileCatalogEntry(name, resolve_profile(name, decls), _gate(name)))
    return out
