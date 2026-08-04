"""Tests for hierarchical AGENTS.md loading (Codex pattern).

These exercise the pure, sync, filesystem-only loader — no DB, no async, no
network. Each test builds a fake project tree under pytest's ``tmp_path``.
"""
from __future__ import annotations

from pathlib import Path

from app.agents.context_fragments import ContextFragment
from app.agents.project_instructions import (
    find_project_root,
    load_project_instructions,
    project_instructions_fragment,
)


# --------------------------------------------------------------------------- #
# find_project_root
# --------------------------------------------------------------------------- #
def test_find_project_root_locates_git_marker(tmp_path: Path):
    root = tmp_path / "proj"
    sub = root / "a" / "b"
    sub.mkdir(parents=True)
    (root / ".git").mkdir()

    found = find_project_root(sub)
    assert found == root.resolve()


def test_find_project_root_accepts_file_start_path(tmp_path: Path):
    root = tmp_path / "proj"
    deep = root / "src"
    deep.mkdir(parents=True)
    (root / ".git").mkdir()
    a_file = deep / "main.py"
    a_file.write_text("x", encoding="utf-8")

    assert find_project_root(a_file) == root.resolve()


def test_find_project_root_returns_none_without_marker(tmp_path: Path):
    # tmp_path has no .git anywhere up to the FS root boundary we control; the
    # real FS root above tmp_path also lacks .git on CI/dev machines.
    deep = tmp_path / "x" / "y"
    deep.mkdir(parents=True)
    assert find_project_root(deep) is None


def test_find_project_root_custom_markers(tmp_path: Path):
    root = tmp_path / "proj"
    root.mkdir()
    (root / "pyproject.toml").write_text("[tool]", encoding="utf-8")
    sub = root / "pkg"
    sub.mkdir()

    assert find_project_root(sub, markers=("pyproject.toml",)) == root.resolve()


# --------------------------------------------------------------------------- #
# load_project_instructions — concatenation
# --------------------------------------------------------------------------- #
def test_load_concatenates_root_to_cwd_in_order(tmp_path: Path):
    root = tmp_path / "proj"
    mid = root / "pkg"
    cwd = mid / "work"
    cwd.mkdir(parents=True)
    (root / ".git").mkdir()
    (root / "AGENTS.md").write_text("ROOT_RULES", encoding="utf-8")
    (mid / "AGENTS.md").write_text("MID_RULES", encoding="utf-8")
    (cwd / "AGENTS.md").write_text("CWD_RULES", encoding="utf-8")

    result = load_project_instructions(cwd)

    # All three present, in root→cwd order.
    assert "ROOT_RULES" in result
    assert "MID_RULES" in result
    assert "CWD_RULES" in result
    assert result.index("ROOT_RULES") < result.index("MID_RULES") < result.index("CWD_RULES")


def test_load_skips_dirs_without_agents_md(tmp_path: Path):
    root = tmp_path / "proj"
    mid = root / "pkg"  # no AGENTS.md here
    cwd = mid / "work"
    cwd.mkdir(parents=True)
    (root / ".git").mkdir()
    (root / "AGENTS.md").write_text("ROOT", encoding="utf-8")
    (cwd / "AGENTS.md").write_text("CWD", encoding="utf-8")

    result = load_project_instructions(cwd)
    assert "ROOT" in result and "CWD" in result
    assert result.index("ROOT") < result.index("CWD")


def test_load_when_cwd_is_root(tmp_path: Path):
    root = tmp_path / "proj"
    root.mkdir()
    (root / ".git").mkdir()
    (root / "AGENTS.md").write_text("ONLY", encoding="utf-8")

    assert load_project_instructions(root) == "ONLY"


# --------------------------------------------------------------------------- #
# load_project_instructions — override precedence
# --------------------------------------------------------------------------- #
def test_override_replaces_sibling_agents_md(tmp_path: Path):
    root = tmp_path / "proj"
    root.mkdir()
    (root / ".git").mkdir()
    (root / "AGENTS.md").write_text("BASE", encoding="utf-8")
    (root / "AGENTS.override.md").write_text("OVERRIDE", encoding="utf-8")

    result = load_project_instructions(root)
    assert result == "OVERRIDE"
    assert "BASE" not in result


def test_override_only_affects_its_own_directory(tmp_path: Path):
    root = tmp_path / "proj"
    sub = root / "sub"
    sub.mkdir(parents=True)
    (root / ".git").mkdir()
    (root / "AGENTS.md").write_text("ROOT_BASE", encoding="utf-8")
    # sub has an override; root keeps its plain AGENTS.md.
    (sub / "AGENTS.md").write_text("SUB_BASE", encoding="utf-8")
    (sub / "AGENTS.override.md").write_text("SUB_OVERRIDE", encoding="utf-8")

    result = load_project_instructions(sub)
    assert "ROOT_BASE" in result  # root's plain file still used
    assert "SUB_OVERRIDE" in result
    assert "SUB_BASE" not in result  # sibling replaced
    assert result.index("ROOT_BASE") < result.index("SUB_OVERRIDE")


# --------------------------------------------------------------------------- #
# load_project_instructions — byte budget
# --------------------------------------------------------------------------- #
def test_byte_budget_truncates_last_file(tmp_path: Path):
    root = tmp_path / "proj"
    sub = root / "sub"
    sub.mkdir(parents=True)
    (root / ".git").mkdir()
    (root / "AGENTS.md").write_text("R", encoding="utf-8")
    # The last (cwd-most) file is the one whose tail must be trimmed.
    (sub / "AGENTS.md").write_text("SUBCONTENT", encoding="utf-8")

    # "R" + "\n\n" + "SUBCONTENT" = 13 bytes; budget 5 -> "R\n\nSU".
    result = load_project_instructions(sub, max_bytes=5)

    assert len(result.encode("utf-8")) <= 5
    assert result == "R\n\nSU"
    # Root/shallower file is fully preserved; only the last file is truncated.
    assert result.startswith("R")
    assert "SUBCONTENT" not in result


def test_byte_budget_truncates_single_file(tmp_path: Path):
    root = tmp_path / "proj"
    root.mkdir()
    (root / ".git").mkdir()
    (root / "AGENTS.md").write_text("ROOT_CONTENT", encoding="utf-8")

    result = load_project_instructions(root, max_bytes=5)
    assert result == "ROOT_"
    assert len(result.encode("utf-8")) <= 5


def test_byte_budget_keeps_full_text_when_under_limit(tmp_path: Path):
    root = tmp_path / "proj"
    root.mkdir()
    (root / ".git").mkdir()
    (root / "AGENTS.md").write_text("short", encoding="utf-8")

    assert load_project_instructions(root, max_bytes=8192) == "short"


def test_byte_budget_truncation_keeps_valid_utf8(tmp_path: Path):
    # Multi-byte tail must not produce a broken codepoint.
    root = tmp_path / "proj"
    root.mkdir()
    (root / ".git").mkdir()
    # "é" is 2 bytes in UTF-8 (0xC3 0xA9); cutting at 1 byte must drop it cleanly.
    (root / "AGENTS.md").write_text("aé", encoding="utf-8")

    result = load_project_instructions(root, max_bytes=1)
    assert result.encode("utf-8") == b"a"  # valid, no orphan byte


# --------------------------------------------------------------------------- #
# load_project_instructions — empty cases
# --------------------------------------------------------------------------- #
def test_no_marker_returns_empty(tmp_path: Path):
    deep = tmp_path / "x" / "y"
    deep.mkdir(parents=True)
    (deep / "AGENTS.md").write_text("IGNORED", encoding="utf-8")
    # No marker anywhere -> no root -> "" (even though an AGENTS.md exists).
    assert load_project_instructions(deep) == ""


def test_no_agents_md_returns_empty(tmp_path: Path):
    root = tmp_path / "proj"
    root.mkdir()
    (root / ".git").mkdir()  # root exists, but no AGENTS.md anywhere
    assert load_project_instructions(root) == ""


# --------------------------------------------------------------------------- #
# project_instructions_fragment
# --------------------------------------------------------------------------- #
def test_fragment_shape_and_render():
    frag = project_instructions_fragment("hello")
    assert isinstance(frag, ContextFragment)
    assert frag.name == "project_instructions"
    assert frag.tag == "project_instructions"
    assert frag.body == "hello"
    rendered = frag.render()
    assert rendered == "<project_instructions>\nhello\n</project_instructions>"


def test_fragment_empty_body_renders_blank():
    frag = project_instructions_fragment("")
    assert frag.body == ""
    assert frag.render() == ""


def test_fragment_wraps_real_loader_output(tmp_path: Path):
    root = tmp_path / "proj"
    root.mkdir()
    (root / ".git").mkdir()
    (root / "AGENTS.md").write_text("RULES", encoding="utf-8")

    text = load_project_instructions(root)
    frag = project_instructions_fragment(text)
    assert "RULES" in frag.body
    assert "<project_instructions>" in frag.render()
