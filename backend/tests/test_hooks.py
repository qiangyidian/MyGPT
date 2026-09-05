"""Lifecycle-hooks engine tests.

Drives the engine primarily through the in-process callable path (fast, no
platform flakiness) and additionally exercises the real subprocess path with a
tiny hook script written to ``tmp_path`` — including the Codex exit-code-2 block
and a sleep-past-timeout case.
"""
from __future__ import annotations

import sys

from app.agents.hooks import (
    HookEngine,
    HookHandler,
    HookInput,
    HookResult,
    HookTrustStatus,
    PreToolUseResult,
    trust_hash,
    trust_status,
)


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def _dec(**decision):
    """Build a callable hook that always returns ``decision`` (as a dict)."""

    def _fn(_payload: dict) -> dict:
        return dict(decision)

    return _fn


def _capture(capture: dict, key: str = "seen"):
    """Build a callable hook that records the input payload into ``capture[key]``
    and returns an allow passthrough."""

    def _fn(payload: dict) -> dict:
        capture[key] = payload
        return {"permission_decision": "allow"}

    return _fn


def _write_hook(tmp_path, name: str, body: str) -> str:
    """Write a small python hook script to tmp_path and return its path."""
    p = tmp_path / name
    p.write_text(body, encoding="utf-8")
    return str(p)


# --------------------------------------------------------------------------- #
# Callable path — allow / deny / rewrite / context
# --------------------------------------------------------------------------- #
def test_callable_allow_passthrough():
    engine = HookEngine()
    r = engine.run_pre_tool_use("Bash", {"cmd": "ls"}, [HookHandler(_dec(permission_decision="allow"))])
    assert r.permission_decision == "allow"
    assert r.continue_ is True
    assert r.updated_input is None
    assert r.additional_context is None


def test_callable_deny_blocks_with_reason():
    engine = HookEngine()
    r = engine.run_pre_tool_use(
        "Bash",
        {"cmd": "rm -rf /"},
        [HookHandler(_dec(permission_decision="deny", stop_reason="destructive"))],
    )
    assert r.permission_decision == "deny"
    assert r.continue_ is False
    assert "destructive" in r.stop_reason


def test_callable_continue_false_also_blocks():
    # A bare {"continue": false} (no permission_decision) must still fold to deny.
    engine = HookEngine()
    r = engine.run_pre_tool_use("Bash", {}, [HookHandler(_dec(**{"continue": False}))])
    assert r.permission_decision == "deny"
    assert r.continue_ is False


def test_updated_input_rewrites_args():
    engine = HookEngine()
    r = engine.run_pre_tool_use(
        "Bash",
        {"cmd": "echo"},
        [HookHandler(_dec(updated_input={"cmd": "echo patched", "safe": True}))],
    )
    assert r.updated_input == {"cmd": "echo patched", "safe": True}
    assert r.permission_decision is None  # no explicit allow/deny -> passthrough


def test_updated_input_last_non_none_wins():
    engine = HookEngine()
    handlers = [
        HookHandler(_dec(updated_input={"v": 1})),
        HookHandler(_dec(updated_input={"v": 2})),
        HookHandler(_dec()),  # no updated_input -> does NOT clobber
    ]
    r = engine.run_pre_tool_use("Bash", {}, handlers)
    assert r.updated_input == {"v": 2}


def test_additional_context_injected_and_concatenated():
    engine = HookEngine()
    handlers = [
        HookHandler(_dec(additional_context="ctx-A")),
        HookHandler(_dec(additional_context="ctx-B")),
    ]
    r = engine.run_pre_tool_use("Bash", {}, handlers)
    assert r.additional_context is not None
    assert "ctx-A" in r.additional_context
    assert "ctx-B" in r.additional_context
    # blank-line separator between the two contexts
    assert r.additional_context == "ctx-A\n\nctx-B"


def test_any_deny_wins_over_allow_and_context():
    engine = HookEngine()
    handlers = [
        HookHandler(_dec(permission_decision="allow", additional_context="keep")),
        HookHandler(_dec(permission_decision="deny", stop_reason="nope")),
        HookHandler(_dec(updated_input={"rewrite": True})),
    ]
    r = engine.run_pre_tool_use("Bash", {}, handlers)
    assert r.permission_decision == "deny"
    assert r.continue_ is False
    assert "nope" in r.stop_reason
    # A deny makes updated_input / additional_context moot — they are not exposed.
    assert r.updated_input is None
    assert r.additional_context is None


def test_input_envelope_carries_event_tool_fields():
    capture: dict = {}
    engine = HookEngine()
    engine.run_pre_tool_use(
        "Read",
        {"path": "/a/b"},
        [HookHandler(_capture(capture))],
        session_id="s1",
        turn_id="t1",
        cwd="/repo",
    )
    payload = capture["seen"]
    assert payload["event"] == "pre_tool_use"
    assert payload["tool_name"] == "Read"
    assert payload["tool_input"] == {"path": "/a/b"}
    assert payload["session_id"] == "s1"
    assert payload["turn_id"] == "t1"
    assert payload["cwd"] == "/repo"


# --------------------------------------------------------------------------- #
# Matcher + selection
# --------------------------------------------------------------------------- #
def test_matcher_filters_by_tool_name():
    saw: dict = {}

    def bash_fn(payload):
        saw["bash"] = True
        return {"additional_context": "from-bash"}

    def read_fn(payload):
        saw["read"] = True
        return {"additional_context": "from-read"}

    handlers = [
        HookHandler(bash_fn, matcher="Bash"),
        HookHandler(read_fn, matcher="Read"),
    ]
    engine = HookEngine()
    r = engine.run_pre_tool_use("Bash", {}, handlers)
    assert saw.get("bash") is True and "read" not in saw
    assert r.additional_context == "from-bash"


def test_matcher_regex_star_and_fnmatch_fallback():
    from app.agents.hooks.engine import _matcher_hits

    engine = HookEngine()
    # "*" is the reserved match-all sentinel.
    r = engine.run_pre_tool_use("Anything", {}, [HookHandler(_dec(permission_decision="allow"), matcher="*")])
    assert r.permission_decision == "allow"
    # valid regex fullmatch: "bash_.*" matches "bash_exec".
    r = engine.run_pre_tool_use("bash_exec", {}, [HookHandler(_dec(permission_decision="allow"), matcher="bash_.*")])
    assert r.permission_decision == "allow"
    # the same regex does not match a different tool -> handler skipped -> passthrough.
    r = engine.run_pre_tool_use("Read", {}, [HookHandler(_dec(permission_decision="allow"), matcher="bash_.*")])
    assert r.permission_decision is None

    # Invalid regex (leading "*" -> re.error) falls back to fnmatch glob semantics.
    assert _matcher_hits("*bash", "foobash") is True
    assert _matcher_hits("*bash", "bash") is True
    assert _matcher_hits("*bash", "read") is False


def test_select_filters_registry_by_event_and_tool():
    engine = HookEngine(
        [
            HookHandler(_dec(), matcher="Bash", event="pre_tool_use"),
            HookHandler(_dec(), matcher="Bash", event="post_tool_use"),
            HookHandler(_dec(), matcher="Read", event="pre_tool_use"),
            HookHandler(_dec(), matcher="*", event=None),  # fires for every event
        ]
    )
    pre_bash = engine.select("pre_tool_use", "Bash")
    # Bash(pre) + the universal handler (event=None) match; Read and post are out.
    assert len(pre_bash) == 2
    post_bash = engine.select("post_tool_use", "Bash")
    assert len(post_bash) == 2  # Bash(post) + universal


# --------------------------------------------------------------------------- #
# Failure modes -> safe default
# --------------------------------------------------------------------------- #
def test_callable_returning_none_is_safe_default():
    engine = HookEngine()
    r = engine.run_pre_tool_use("Bash", {}, [HookHandler(lambda _p: None)])
    assert r.permission_decision is None
    assert r.continue_ is True
    assert r.updated_input is None


def test_callable_raising_is_safe_default():
    def boom(_p):
        raise RuntimeError("hook crashed")

    engine = HookEngine()
    r = engine.run_pre_tool_use("Bash", {}, [HookHandler(boom)])
    assert r.permission_decision is None
    assert r.continue_ is True


def test_callable_unparseable_dict_is_safe_default():
    # invalid permission_decision literal -> whole result drops to safe default.
    engine = HookEngine()
    r = engine.run_pre_tool_use("Bash", {}, [HookHandler(_dec(permission_decision="maybe"))])
    assert r.permission_decision is None
    assert r.continue_ is True


# --------------------------------------------------------------------------- #
# Subprocess path (real tiny hook scripts)
# --------------------------------------------------------------------------- #
def test_subprocess_allow_passthrough(tmp_path):
    script = _write_hook(
        tmp_path,
        "allow.py",
        "import json, sys\n"
        "json.load(sys.stdin)\n"
        'print(json.dumps({"permission_decision": "allow", "continue": True}))\n',
    )
    engine = HookEngine()
    r = engine.run_pre_tool_use("Bash", {"cmd": "ls"}, [HookHandler([sys.executable, script])])
    assert r.permission_decision == "allow"
    assert r.continue_ is True


def test_subprocess_exit2_blocks_with_stderr_reason(tmp_path):
    script = _write_hook(
        tmp_path,
        "block.py",
        "import sys\n"
        'sys.stderr.write("blocked: too dangerous")\n'
        "sys.exit(2)\n",
    )
    engine = HookEngine()
    r = engine.run_pre_tool_use("Bash", {"cmd": "rm -rf /"}, [HookHandler([sys.executable, script])])
    assert r.permission_decision == "deny"
    assert r.continue_ is False
    assert "too dangerous" in r.stop_reason


def test_subprocess_updated_input_rewrites_args(tmp_path):
    script = _write_hook(
        tmp_path,
        "rewrite.py",
        "import json, sys\n"
        "data = json.load(sys.stdin)\n"
        'print(json.dumps({"updated_input": {**data.get("tool_input", {}), "injected": True}}))\n',
    )
    engine = HookEngine()
    r = engine.run_pre_tool_use("Bash", {"cmd": "echo"}, [HookHandler([sys.executable, script])])
    assert r.updated_input == {"cmd": "echo", "injected": True}


def test_subprocess_unparseable_stdout_is_safe_default(tmp_path):
    script = _write_hook(tmp_path, "junk.py", 'print("this is not json")\n')
    engine = HookEngine()
    r = engine.run_pre_tool_use("Bash", {}, [HookHandler([sys.executable, script])])
    assert r.permission_decision is None
    assert r.continue_ is True


def test_subprocess_timeout_is_safe_default(tmp_path):
    # Sleeps well past the timeout; subprocess.run kills the child on expiry.
    script = _write_hook(tmp_path, "sleep.py", "import time; time.sleep(30)\n")
    engine = HookEngine()
    r = engine.run_pre_tool_use("Bash", {}, [HookHandler([sys.executable, script], timeout_s=0.5)])
    assert r.permission_decision is None
    assert r.continue_ is True
    assert r.updated_input is None


def test_subprocess_missing_executable_is_safe_default():
    engine = HookEngine()
    r = engine.run_pre_tool_use("Bash", {}, [HookHandler(["__definitely_not_a_real_binary__"])])
    assert r.permission_decision is None
    assert r.continue_ is True


# --------------------------------------------------------------------------- #
# Trust
# --------------------------------------------------------------------------- #
def test_trust_hash_stable_and_sensitive():
    h = HookHandler(["python", "hook.py"], matcher="Bash", timeout_s=5.0)
    same = HookHandler(["python", "hook.py"], matcher="Bash", timeout_s=5.0)
    assert trust_hash(h) == trust_hash(same)
    # changing any identity field changes the hash
    assert trust_hash(HookHandler(["python", "hook.py"], matcher="Read")) != trust_hash(h)
    assert trust_hash(HookHandler(["python", "hook.py"], matcher="Bash", timeout_s=9.0)) != trust_hash(h)
    assert trust_hash(HookHandler(["python", "other.py"], matcher="Bash")) != trust_hash(h)


def test_trust_status_classification():
    cmd = HookHandler(["python", "hook.py"])
    assert trust_status(cmd, None) == HookTrustStatus.untrusted
    h = trust_hash(cmd)
    assert trust_status(cmd, h) == HookTrustStatus.trusted
    assert trust_status(HookHandler(["python", "other.py"]), h) == HookTrustStatus.modified
    # callables are always managed regardless of any recorded hash
    assert trust_status(HookHandler(lambda _p: None), trust_hash(cmd)) == HookTrustStatus.managed


# --------------------------------------------------------------------------- #
# Schema sanity
# --------------------------------------------------------------------------- #
def test_hook_result_alias_round_trip():
    # The JSON wire key is "continue"; the Python field is continue_.
    r = HookResult.model_validate({"continue": False, "stop_reason": "x"})
    assert r.continue_ is False
    assert r.stop_reason == "x"
    dumped = r.model_dump(by_alias=True)
    assert dumped["continue"] is False


def test_pre_tool_use_result_defaults():
    r = PreToolUseResult()
    assert r.continue_ is True
    assert r.permission_decision is None
    assert r.updated_input is None
    assert r.additional_context is None


def test_event_subclasses_carry_discriminator():
    from app.agents.hooks import (
        PostToolUseInput,
        PreToolUseInput,
        SessionStartInput,
        StopInput,
        UserPromptSubmitInput,
    )

    assert PreToolUseInput().event == "pre_tool_use"
    assert PostToolUseInput().event == "post_tool_use"
    assert UserPromptSubmitInput(prompt="hi").event == "user_prompt_submit"
    assert SessionStartInput().event == "session_start"
    assert StopInput().event == "stop"


def test_run_one_returns_typed_result_for_other_events():
    # Non-fold events just need run_one to parse a decision into the right model.
    engine = HookEngine()
    inp = HookInput(event="session_start", tool_name="")
    r = engine.run_one(HookHandler(_dec(**{"continue": True, "system_message": "hi"})), inp, HookResult)
    assert isinstance(r, HookResult)
    assert r.continue_ is True
    assert r.system_message == "hi"
