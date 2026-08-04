"""Agent addressing + inter-agent message protocol (Codex pattern).

Pure stdlib, no fixtures — exercises AgentPath parse/join/parent/root/
relative_to + invalid-segment rejection, the InterAgentMessage envelope, and
the subagent_notification ContextFragment.
"""
from __future__ import annotations

import json

import pytest

from app.agents.agent_protocol import (
    SUBAGENT_STATUSES,
    AgentPath,
    InterAgentMessage,
    subagent_notification_fragment,
)
from app.agents.context_fragments import ContextFragment


# --------------------------------------------------------------------------- #
# AgentPath — parsing & string form
# --------------------------------------------------------------------------- #
def test_parse_root_and_descendants_round_trip_through_str():
    assert str(AgentPath("/")) == "/"
    assert str(AgentPath("/root")) == "/root"
    assert str(AgentPath("/root/researcher")) == "/root/researcher"


def test_parse_classmethod_matches_constructor():
    assert AgentPath.parse("/a/b") == AgentPath("/a/b")


def test_segments_tuple_exposed_and_root_is_empty():
    assert AgentPath("/").segments == ()
    assert AgentPath("/root/researcher").segments == ("root", "researcher")


# --------------------------------------------------------------------------- #
# AgentPath — join / parent / is_root
# --------------------------------------------------------------------------- #
def test_join_appends_single_segment():
    root = AgentPath("/root")
    child = root.join("researcher")
    assert child == AgentPath("/root/researcher")
    assert str(child) == "/root/researcher"
    # join from the global root works too.
    assert AgentPath("/").join("root") == AgentPath("/root")


def test_parent_walks_up_one_level_and_root_has_none():
    assert AgentPath("/root/researcher").parent == AgentPath("/root")
    # single segment -> parent is the global root "/"
    assert AgentPath("/root").parent == AgentPath("/")
    # the global root itself has no parent
    assert AgentPath("/").parent is None


def test_is_root_only_for_global_root():
    assert AgentPath("/").is_root is True
    assert AgentPath("/root").is_root is False
    assert AgentPath("/root/researcher").is_root is False


# --------------------------------------------------------------------------- #
# AgentPath — relative_to
# --------------------------------------------------------------------------- #
def test_relative_to_returns_leading_slash_free_suffix():
    assert AgentPath("/root/researcher").relative_to("/root") == "researcher"
    assert AgentPath("/root/researcher").relative_to(AgentPath("/")) == "root/researcher"
    # relative_to self -> empty string (zero segments below).
    assert AgentPath("/root").relative_to("/root") == ""


def test_relative_to_rejects_non_ancestor():
    with pytest.raises(ValueError):
        AgentPath("/root").relative_to("/root/researcher")  # not a descendant
    with pytest.raises(ValueError):
        AgentPath("/alpha/x").relative_to("/beta")  # different branch


# --------------------------------------------------------------------------- #
# AgentPath — equality / hashability (registry-key ready)
# --------------------------------------------------------------------------- #
def test_equal_paths_hash_equal_and_compare_equal():
    a, b = AgentPath("/root/researcher"), AgentPath.parse("/root/researcher")
    assert a == b
    assert hash(a) == hash(b)
    assert {a, b} == {a}  # dedupes in a set


# --------------------------------------------------------------------------- #
# AgentPath — invalid segments / paths are rejected
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "bad",
    [
        "",           # empty string
        "root",       # not absolute (no leading /)
        "/root/",     # trailing slash -> empty trailing segment
        "/a//b",      # empty middle segment
        "/a b",       # whitespace
        "/a.b",       # dot not allowed
        "/a/b.c",     # dot in a later segment
        "/研究",       # non-ascii (not in [A-Za-z0-9_-])
    ],
)
def test_invalid_paths_are_rejected(bad):
    with pytest.raises(ValueError):
        AgentPath(bad)


@pytest.mark.parametrize(
    "bad",
    ["", "a/b", "a.b", "a b", "a:b"],
)
def test_join_rejects_invalid_child_segment(bad):
    with pytest.raises(ValueError):
        AgentPath("/root").join(bad)


# --------------------------------------------------------------------------- #
# InterAgentMessage envelope
# --------------------------------------------------------------------------- #
def test_inter_agent_message_fields_and_sender_type():
    msg = InterAgentMessage(
        msg_type="FINAL_ANSWER",
        task_name="research",
        sender=AgentPath("/root/researcher"),
        payload="found 3 sources",
    )
    assert msg.msg_type == "FINAL_ANSWER"
    assert msg.task_name == "research"
    assert isinstance(msg.sender, AgentPath)
    assert str(msg.sender) == "/root/researcher"
    assert msg.payload == "found 3 sources"


def test_inter_agent_message_is_frozen():
    msg = InterAgentMessage("MESSAGE", "t", AgentPath("/root"), "hi")
    with pytest.raises(Exception):
        msg.payload = "mutated"  # type: ignore[misc]  # frozen dataclass


# --------------------------------------------------------------------------- #
# subagent_notification_fragment — tagged JSON block the parent sees
# --------------------------------------------------------------------------- #
def test_subagent_fragment_is_tagged_context_fragment():
    frag = subagent_notification_fragment(
        AgentPath("/root/researcher"), status="Running", detail=" halfway"
    )
    assert isinstance(frag, ContextFragment)
    assert frag.name == "subagent_notification"
    assert frag.tag == "subagent_notification"
    # body is a JSON blob carrying the path + status + detail.
    payload = json.loads(frag.body)
    assert payload == {
        "agent_path": "/root/researcher",
        "status": "Running",
        "detail": " halfway",
    }


def test_subagent_fragment_renders_tagged_block_with_path_and_status():
    rendered = subagent_notification_fragment(
        AgentPath("/root/researcher"), status="Completed"
    ).render()
    assert rendered.startswith("<subagent_notification>\n")
    assert rendered.endswith("\n</subagent_notification>")
    # the rendered JSON round-trips and carries the path + status.
    body_json = rendered.split("\n", 1)[1].rsplit("\n", 1)[0]
    parsed = json.loads(body_json)
    assert parsed["agent_path"] == "/root/researcher"
    assert parsed["status"] == "Completed"
    assert parsed["detail"] == ""  # defaulted


def test_subagent_fragment_tag_is_detectable_in_history():
    # ContextFragment.contains_tag is how retention/diffing finds an injected
    # block inside retained history — the notification must be detectable.
    rendered = subagent_notification_fragment(
        AgentPath("/root/x"), "Errored", "boom"
    ).render()
    assert ContextFragment.contains_tag(rendered, "subagent_notification")


def test_subagent_fragment_accepts_string_path_and_rejects_bad_status():
    # str path is coerced to AgentPath.
    frag = subagent_notification_fragment("/root/x", status="PendingInit")
    assert json.loads(frag.body)["agent_path"] == "/root/x"

    # every documented lifecycle status is accepted
    for s in SUBAGENT_STATUSES:
        subagent_notification_fragment(AgentPath("/root/x"), s)

    # an unknown status fails loud and early.
    with pytest.raises(ValueError):
        subagent_notification_fragment(AgentPath("/root/x"), "Bogus")
