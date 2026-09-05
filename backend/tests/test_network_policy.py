"""Network egress policy + agent spawn-graph store (Codex leaf modules)."""
from __future__ import annotations

import pytest

from app.agents.agent_graph_store import JsonAgentGraphStore, new_node_id
from app.agents.network_policy import (
    NetworkPolicy,
    NetworkRule,
    NetworkRuleStore,
    normalize_host,
    validate_host,
)


# ---- network_policy ----
def test_normalize_host_strips_port_user_scheme_dot():
    assert normalize_host("API.Example.COM:443") == "api.example.com"
    assert normalize_host("user@host.example.com") == "host.example.com"
    assert normalize_host("https://foo.bar/") == "foo.bar/"
    assert normalize_host("Example.COM.") == "example.com"


def test_validate_host_rejects_wildcards_and_empty():
    with pytest.raises(ValueError):
        validate_host("")
    with pytest.raises(ValueError):
        validate_host("*.example.com")
    with pytest.raises(ValueError):
        validate_host("foo?bar")
    validate_host("api.github.com")  # ok


def test_policy_first_match_wins_and_default():
    p = NetworkPolicy(
        [NetworkRule(host="evil.example.com", decision="forbidden")],
        default="allow",
    )
    assert p.decide("evil.example.com") == "forbidden"
    assert p.decide("ok.example.com") == "allow"


def test_policy_protocol_scoped():
    p = NetworkPolicy([NetworkRule(host="mixed.example.com", decision="forbidden", protocol="http")])
    assert p.decide("mixed.example.com", protocol="http") == "forbidden"
    assert p.decide("mixed.example.com", protocol="https") == "allow"  # rule is http-only


def test_rulestore_persist_dedup_roundtrip(tmp_path):
    store = NetworkRuleStore(tmp_path / "net.json")
    store.add_rule("api.github.com", "allow")
    store.add_rule("api.github.com", "allow")  # dup -> ignored
    store.add_rule("evil.example.com", "forbidden")
    policy = NetworkRuleStore(tmp_path / "net.json").load()
    assert len(policy.rules) == 2
    assert policy.decide("api.github.com") == "allow"
    assert policy.decide("evil.example.com") == "forbidden"


# ---- agent_graph_store ----
def test_graph_children_and_descendants_bfs(tmp_path):
    store = JsonAgentGraphStore(tmp_path / "graph.json")
    root = new_node_id()
    a = new_node_id()
    b = new_node_id()
    c = new_node_id()  # child of a (grandchild of root)
    store.add_edge(parent_id=root, child_id=a)
    store.add_edge(parent_id=root, child_id=b)
    store.add_edge(parent_id=a, child_id=c)

    kids = store.children(root)
    assert {e.child_id for e in kids} == {a, b}

    desc = store.descendants(root)
    assert {e.child_id for e in desc} == {a, b, c}  # BFS reaches the grandchild


def test_graph_status_filter_and_close(tmp_path):
    store = JsonAgentGraphStore(tmp_path / "graph.json")
    root = new_node_id()
    a = new_node_id()
    b = new_node_id()
    store.add_edge(parent_id=root, child_id=a)
    store.add_edge(parent_id=root, child_id=b)
    store.set_status(a, "closed")

    open_only = store.descendants(root, include_closed=False)
    assert {e.child_id for e in open_only} == {b}

    # reload persists status
    store2 = JsonAgentGraphStore(tmp_path / "graph.json")
    assert all(e.status == "closed" for e in store2.children(root) if e.child_id == a)


def test_graph_set_status_rejects_bad_value(tmp_path):
    store = JsonAgentGraphStore(tmp_path / "g.json")
    store.add_edge(parent_id="p", child_id="c")
    with pytest.raises(ValueError):
        store.set_status("c", "bogus")
