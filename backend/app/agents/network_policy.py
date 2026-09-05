"""Per-host network egress policy (Codex pattern).

Codex treats network egress as a first-class, individually-addressable policy
target: ``network_rule(host, protocol, decision=allow|forbidden)`` with
safe-by-default parsing (hosts normalized, wildcards rejected so you can't
accidentally allowlist ``*.com``). This is the data-exfiltration control surface
for tools that make outbound calls.

Stdlib-only; mirrors :mod:`app.agents.exec_policy` (allow/prompt/forbidden prefix
rules for commands).
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

Decision = Literal["allow", "forbidden"]

_WILDCARD_CHARS = ("*", "?")
_SCHEME_SEP = "://"


def normalize_host(host: str) -> str:
    """Lowercase, drop user@, port, and trailing dot; keep IPv6 brackets stripped."""
    h = (host or "").strip().lower()
    if "@" in h:
        h = h.rsplit("@", 1)[1]
    if _SCHEME_SEP in h:
        h = h.split(_SCHEME_SEP, 1)[1]
    # IPv6 literal like [::1]:443 or [::1]
    if h.startswith("["):
        end = h.find("]")
        h = h[1:end] if end != -1 else h
    # Strip a trailing :port (naive but fine for host strings; IPv6 already handled).
    if h.count(":") == 1:
        h = h.split(":", 1)[0]
    h = h.rstrip(".")
    return h.strip()


def validate_host(host: str) -> None:
    """Reject empty hosts, wildcards, and globs — a rule must name a concrete host."""
    h = (host or "").strip()
    if not h:
        raise ValueError("network rule host must not be empty")
    if any(c in h for c in _WILDCARD_CHARS):
        raise ValueError(f"network rule host must be concrete (no wildcards): {host!r}")


@dataclass(frozen=True)
class NetworkRule:
    host: str          # stored normalized
    decision: Decision
    protocol: str | None = None  # "https" / "http" / None = any


class NetworkPolicy:
    """First-match-wins host policy. Default decision when no rule matches."""

    def __init__(self, rules: list[NetworkRule] | None = None, *, default: Decision = "allow") -> None:
        self.rules = list(rules or [])
        self.default = default

    def decide(self, host: str, protocol: str | None = None) -> Decision:
        h = normalize_host(host)
        proto = (protocol or "").strip().lower() or None
        for r in self.rules:
            if r.host == h and (r.protocol is None or r.protocol == proto):
                return r.decision
        return self.default


class NetworkRuleStore:
    """JSON-backed persistence for network rules (atomic write, dedup)."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)

    def load(self) -> NetworkPolicy:
        if not self.path.exists():
            return NetworkPolicy(default="allow")
        data = json.loads(self.path.read_text(encoding="utf-8"))
        rules = [
            NetworkRule(host=r["host"], decision=r["decision"], protocol=r.get("protocol"))
            for r in data.get("rules", [])
        ]
        return NetworkPolicy(rules, default=data.get("default", "allow"))

    def add_rule(self, host: str, decision: Decision, *, protocol: str | None = None) -> NetworkPolicy:
        validate_host(host)
        h = normalize_host(host)
        policy = self.load()
        # Dedup identical (host, decision, protocol).
        exists = any(
            r.host == h and r.decision == decision and (r.protocol or None) == (protocol or None)
            for r in policy.rules
        )
        if not exists:
            policy.rules.append(NetworkRule(host=h, decision=decision, protocol=protocol or None))
        self._write(policy)
        return self.load()

    def _write(self, policy: NetworkPolicy) -> None:
        payload = {
            "default": policy.default,
            "rules": [
                {"host": r.host, "decision": r.decision, "protocol": r.protocol}
                for r in policy.rules
            ],
        }
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, self.path)


def load_active_policy() -> NetworkPolicy:
    """Load the operator-configured egress policy, or default-allow when unset.

    Reads ``NETWORK_POLICY_FILE`` (a JSON file managed by :class:`NetworkRuleStore`).
    Empty/unset path => allow-all (the historic behaviour), so this is opt-in.
    A missing/corrupt file degrades to allow-all rather than breaking tool calls.
    """
    from app.core.config import get_settings

    path = getattr(get_settings(), "NETWORK_POLICY_FILE", "") or ""
    if not path:
        return NetworkPolicy(default="allow")
    try:
        return NetworkRuleStore(Path(path)).load()
    except Exception:
        return NetworkPolicy(default="allow")
