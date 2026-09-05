"""Tests for the exec-policy engine (allow / prompt / forbidden prefix rules).

Plain sync pytest; the module under test is stdlib-only so no event loop, DB,
or fixtures from conftest are needed.
"""
from __future__ import annotations

import json

import pytest

from app.agents.exec_policy import (
    DEFAULT_DECISION,
    ExecPolicy,
    PrefixRule,
    RuleStore,
    validate_pattern,
)


# --------------------------------------------------------------------------- #
# PrefixRule / ExecPolicy matching semantics
# --------------------------------------------------------------------------- #
def test_prefix_match_exact_and_with_extra_args():
    """rule (["git","status"],"allow") matches the exact argv and any argv that
    extends it, but NOT a sibling command."""
    policy = ExecPolicy(
        rules=[PrefixRule(pattern=["git", "status"], decision="allow")],
        default="prompt",
    )
    assert policy.decide(["git", "status"]) == "allow"
    assert policy.decide(["git", "status", "--short"]) == "allow"
    # Different command entirely -> falls through to default.
    assert policy.decide(["git", "push"]) == "prompt"
    # Pattern shorter than argv prefix but diverging -> no match.
    assert policy.decide(["git", "stash"]) == "prompt"


def test_pattern_longer_than_argv_never_matches():
    """A pattern cannot match an argv that is shorter than itself."""
    policy = ExecPolicy(rules=[PrefixRule(pattern=["git", "status"], decision="allow")])
    assert policy.decide(["git"]) == DEFAULT_DECISION
    assert policy.decide([]) == DEFAULT_DECISION


def test_case_sensitive_on_command_name():
    """Matching is case-sensitive: ``Git`` != ``git``."""
    policy = ExecPolicy(rules=[PrefixRule(pattern=["git"], decision="allow")])
    assert policy.decide(["git"]) == "allow"
    assert policy.decide(["Git"]) == DEFAULT_DECISION
    assert policy.decide(["GIT"]) == DEFAULT_DECISION


def test_first_match_wins_precedence():
    """When multiple rules match, the FIRST in list order decides."""
    policy = ExecPolicy(
        rules=[
            PrefixRule(pattern=["git"], decision="forbidden"),
            PrefixRule(pattern=["git", "status"], decision="allow"),
        ],
    )
    # The broader ["git"] rule comes first -> forbidden wins even though the
    # more specific ["git","status"] allow rule would also match.
    assert policy.decide(["git", "status"]) == "forbidden"

    # Reverse the order: now the specific allow rule is first.
    policy_specific_first = ExecPolicy(
        rules=[
            PrefixRule(pattern=["git", "status"], decision="allow"),
            PrefixRule(pattern=["git"], decision="forbidden"),
        ],
    )
    assert policy_specific_first.decide(["git", "status"]) == "allow"
    # "git push" only matches the broad forbidden rule.
    assert policy_specific_first.decide(["git", "push"]) == "forbidden"


def test_default_returned_when_no_rule_matches():
    """No matching rule -> the policy default is returned."""
    policy = ExecPolicy(
        rules=[PrefixRule(pattern=["ls"], decision="allow")],
        default="prompt",
    )
    assert policy.decide(["rm", "-rf", "/"]) == "prompt"

    # A non-default default also flows through.
    strict = ExecPolicy(
        rules=[PrefixRule(pattern=["ls"], decision="allow")],
        default="forbidden",
    )
    assert strict.decide(["rm", "-rf", "/"]) == "forbidden"


def test_default_policy_is_prompt_with_no_rules():
    """An empty ExecPolicy defaults to prompt."""
    policy = ExecPolicy()
    assert policy.default == DEFAULT_DECISION == "prompt"
    assert policy.decide(["anything"]) == "prompt"


# --------------------------------------------------------------------------- #
# validate_pattern
# --------------------------------------------------------------------------- #
def test_validate_pattern_rejects_empty():
    with pytest.raises(ValueError):
        validate_pattern([])


@pytest.mark.parametrize("bad_token", ["*", "?", "://", "status?", "a*b", "http://x"])
def test_validate_pattern_rejects_wildcards_and_globs(bad_token):
    with pytest.raises(ValueError):
        validate_pattern(["git", bad_token])


def test_validate_pattern_accepts_concrete_tokens():
    """Concrete commands and flags validate cleanly (no exception)."""
    validate_pattern(["git", "status"])
    validate_pattern(["ls"])
    validate_pattern(["docker", "logs", "--tail", "10"])


# --------------------------------------------------------------------------- #
# RuleStore: JSON-backed persistence
# --------------------------------------------------------------------------- #
def test_rulestore_load_returns_empty_default_when_file_missing(tmp_path):
    store = RuleStore(tmp_path / "does-not-exist.json")
    policy = store.load()
    assert policy.rules == []
    assert policy.default == DEFAULT_DECISION
    assert policy.decide(["git", "status"]) == "prompt"


def test_rulestore_add_persist_reload_roundtrip(tmp_path):
    """add_allow_prefix writes to disk; a fresh RuleStore reloads it."""
    path = tmp_path / "policy.json"
    store = RuleStore(path)
    returned = store.add_allow_prefix(["git", "status"])
    # The returned policy reflects the new rule immediately.
    assert returned.decide(["git", "status"]) == "allow"

    # The file exists on disk with the expected shape.
    assert path.exists()
    on_disk = json.loads(path.read_text(encoding="utf-8"))
    assert on_disk["default"] == "prompt"
    assert on_disk["rules"] == [{"pattern": ["git", "status"], "decision": "allow"}]

    # A brand-new store over the same path reloads the remembered approval.
    reloaded = RuleStore(path).load()
    assert reloaded.decide(["git", "status"]) == "allow"
    assert reloaded.decide(["git", "status", "--short"]) == "allow"
    # Unrelated command still falls through to the default.
    assert reloaded.decide(["git", "push"]) == "prompt"
    assert reloaded.decide(["rm", "-rf"]) == "prompt"


def test_rulestore_dedup_identical_rule(tmp_path):
    """Adding the exact same (pattern, allow) twice does not duplicate."""
    store = RuleStore(tmp_path / "policy.json")
    store.add_allow_prefix(["git", "status"])
    store.add_allow_prefix(["git", "status"])  # identical -> deduped

    policy = store.load()
    matching = [
        r for r in policy.rules
        if r.pattern == ["git", "status"] and r.decision == "allow"
    ]
    assert len(matching) == 1
    # And the on-disk file only contains the one rule.
    on_disk = json.loads(store.path.read_text(encoding="utf-8"))
    assert len(on_disk["rules"]) == 1


def test_rulestore_distinct_rules_coexist(tmp_path):
    """Different patterns are kept as separate remembered approvals."""
    store = RuleStore(tmp_path / "policy.json")
    store.add_allow_prefix(["git", "status"])
    store.add_allow_prefix(["git", "pull"])
    store.add_allow_prefix(["ls"])

    policy = store.load()
    assert policy.decide(["git", "status"]) == "allow"
    assert policy.decide(["git", "pull"]) == "allow"
    assert policy.decide(["ls", "-la"]) == "allow"
    assert policy.decide(["git", "push"]) == "prompt"
    assert len(policy.rules) == 3


def test_rulestore_add_validates_pattern(tmp_path):
    """The persistence layer enforces pattern safety on user input."""
    store = RuleStore(tmp_path / "policy.json")
    with pytest.raises(ValueError):
        store.add_allow_prefix(["git", "*"])
    with pytest.raises(ValueError):
        store.add_allow_prefix([])
    # Nothing was persisted by the rejected calls.
    assert not store.path.exists()


def test_rulestore_write_is_atomic_via_tmp_replace(tmp_path):
    """No stray .tmp file is left behind after a successful write."""
    path = tmp_path / "policy.json"
    RuleStore(path).add_allow_prefix(["git", "status"])
    assert path.exists()
    assert not (tmp_path / "policy.json.tmp").exists()


def test_rulestore_preserves_existing_default_and_rules(tmp_path):
    """add_allow_prefix must not clobber rules/default already on disk."""
    path = tmp_path / "policy.json"
    # Seed the file with a non-default default and a forbidden rule.
    path.write_text(
        json.dumps(
            {
                "default": "forbidden",
                "rules": [{"pattern": ["ls"], "decision": "forbidden"}],
            }
        ),
        encoding="utf-8",
    )
    RuleStore(path).add_allow_prefix(["git", "status"])
    policy = RuleStore(path).load()
    # Default preserved.
    assert policy.default == "forbidden"
    # Existing forbidden rule preserved.
    assert policy.decide(["ls"]) == "forbidden"
    # New allow rule added.
    assert policy.decide(["git", "status"]) == "allow"
    # Unrelated command falls through to the preserved forbidden default.
    assert policy.decide(["rm", "-rf"]) == "forbidden"
    assert len(policy.rules) == 2


def test_decision_literal_values_covered():
    """All three Decision values are usable as rule decisions."""
    policy = ExecPolicy(
        rules=[
            PrefixRule(pattern=["ok"], decision="allow"),
            PrefixRule(pattern=["maybe"], decision="prompt"),
            PrefixRule(pattern=["nope"], decision="forbidden"),
        ],
        default="prompt",
    )
    assert policy.decide(["ok"]) == "allow"
    assert policy.decide(["maybe"]) == "prompt"
    assert policy.decide(["nope"]) == "forbidden"
