"""slash_commands: single-registry parse/help/dispatch + aliases."""
from app.agents import slash_commands as sc
from app.agents.slash_commands import CommandSpec, dispatch, help_text, parse, register, reset_registry


def setup_function(_fn):
    reset_registry()
    for c in [
        CommandSpec(name="mode", description="切换模式", supports_inline_args=True, aliases=("m",)),
        CommandSpec(name="help", description="帮助", aliases=("?",)),
    ]:
        register(c)


def test_parse_name_and_inline_args():
    spec, inline = parse("/mode analyst")
    assert spec is not None and spec.name == "mode"
    assert inline == "analyst"


def test_parse_alias():
    spec, _ = parse("/m fast")
    assert spec is not None and spec.name == "mode"


def test_parse_non_command():
    spec, inline = parse("just chatting")
    assert spec is None
    assert inline == "just chatting"


def test_dispatch_runs_handler():
    calls = []
    register(CommandSpec(name="echo", description="x", supports_inline_args=True,
                         handler=lambda a: calls.append(a)))
    spec, res = dispatch("/echo hi")
    assert spec.name == "echo"
    assert calls == ["hi"]


def test_help_text_lists_registered():
    h = help_text()
    assert "/mode" in h and "/help" in h


def test_unknown_command_returns_help():
    spec, res = dispatch("/nope")
    assert spec is None
    assert "可用命令" in res
