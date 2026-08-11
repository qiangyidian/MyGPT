"""Workspace-confined tools: every path is resolved and required to remain under
the assigned workspace root. Reads/list/search are low-risk; write/patch/shell
mutate. Escape vectors (``..``, absolute-outside, symlink-out) must all reject.

These tests do NOT touch the durable layer, the workflow engine, or the budget
code — they exercise only the confinement helper + the workspace tool classes
against a ``tmp_path`` workspace root.
"""
from __future__ import annotations

import os
import subprocess

import pytest

from app.agents.apply_patch import PatchError
from app.agents.sandbox.local import LocalRunner
from app.tools.base import ToolError
from app.tools.workspace import (
    WorkspaceApplyPatchTool,
    WorkspaceGitDiffTool,
    WorkspaceGitStatusTool,
    WorkspaceListTool,
    WorkspaceReadTool,
    WorkspaceSearchTool,
    WorkspaceShellTool,
    WorkspaceWriteTool,
    resolve_under_root,
)


# --------------------------------------------------------------------------- #
# Fixtures
# --------------------------------------------------------------------------- #
@pytest.fixture
def workspace(tmp_path):
    """A workspace root (a SUBDIR of tmp_path) seeded with files + a subdir.

    Using a subdir — not tmp_path itself — so genuine escapes (siblings under
    tmp_path) are reachable by the symlink/``..`` tests.
    """
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "a.txt").write_text("hello\nworld\n", encoding="utf-8")
    (ws / "sub").mkdir()
    (ws / "sub" / "b.py").write_text("def f():\n    return 1\n", encoding="utf-8")
    return ws


@pytest.fixture
def runner():
    """A dev-gated LocalRunner (test env) the shell/git tools can use."""
    return LocalRunner(env="test")


# --------------------------------------------------------------------------- #
# resolve_under_root — the security core
# --------------------------------------------------------------------------- #
def test_resolve_rejects_dotdot_escape(workspace):
    with pytest.raises(ToolError):
        resolve_under_root("../secret", workspace)


def test_resolve_rejects_deep_dotdot_escape(workspace):
    with pytest.raises(ToolError):
        resolve_under_root("sub/../../evil", workspace)


def test_resolve_rejects_absolute_outside(workspace, tmp_path):
    # An absolute path that does NOT live under the workspace root.
    outside = tmp_path.parent / "elsewhere"
    with pytest.raises(ToolError):
        resolve_under_root(str(outside), workspace)


def test_resolve_rejects_symlink_escape(workspace, tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.txt").write_text("sensitive", encoding="utf-8")
    link = workspace / "escape"
    link.symlink_to(outside)
    with pytest.raises(ToolError):
        resolve_under_root("escape/secret.txt", workspace)


def test_resolve_accepts_path_inside_root(workspace):
    p = resolve_under_root("a.txt", workspace)
    assert p == (workspace / "a.txt").resolve()


def test_resolve_accepts_absolute_path_inside_root(workspace):
    abs_inside = (workspace / "sub" / "b.py").resolve()
    p = resolve_under_root(str(abs_inside), workspace)
    assert p == abs_inside


def test_register_workspace_tools_rejects_empty_root():
    # An empty/whitespace workspace_root must be rejected up front: Path(""),
    # Path(".") are truthy and would silently bind the workspace to the process
    # CWD (the application source tree in production).
    from app.tools.base import ToolRegistry
    from app.tools.registry_init import register_workspace_tools

    for bad in ("", "   ", None, 0):
        reg = ToolRegistry()
        with pytest.raises(ToolError):
            register_workspace_tools(reg, bad)  # type: ignore[arg-type]


# --------------------------------------------------------------------------- #
# Read / List / Search (low-risk reads)
# --------------------------------------------------------------------------- #
async def test_read_returns_content(workspace):
    tool = WorkspaceReadTool(workspace)
    res = await tool.run(path="a.txt")
    assert "hello" in res["content"]
    assert res["bytes"] > 0


async def test_read_rejects_workspace_escape(workspace):
    tool = WorkspaceReadTool(workspace)
    with pytest.raises(ToolError):
        await tool.run(path="../secret")


async def test_read_rejects_symlink_escape(workspace, tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.txt").write_text("nope", encoding="utf-8")
    (workspace / "lnk").symlink_to(outside)
    tool = WorkspaceReadTool(workspace)
    with pytest.raises(ToolError):
        await tool.run(path="lnk/secret.txt")


async def test_read_missing_file_raises(workspace):
    tool = WorkspaceReadTool(workspace)
    with pytest.raises(ToolError):
        await tool.run(path="nope.txt")


async def test_list_lists_entries(workspace):
    tool = WorkspaceListTool(workspace)
    res = await tool.run(path=".")
    names = [e["name"] for e in res["entries"]]
    assert "a.txt" in names
    assert "sub" in names


async def test_list_rejects_escape(workspace):
    tool = WorkspaceListTool(workspace)
    with pytest.raises(ToolError):
        await tool.run(path="../")


async def test_search_finds_pattern(workspace):
    tool = WorkspaceSearchTool(workspace)
    res = await tool.run(pattern="hello", path=".")
    paths = [m["path"] for m in res["matches"]]
    assert any("a.txt" in p for p in paths)


async def test_search_rejects_escape(workspace):
    tool = WorkspaceSearchTool(workspace)
    with pytest.raises(ToolError):
        await tool.run(pattern="x", path="../")


# --------------------------------------------------------------------------- #
# Write — atomic, no partial writes, escape rejected
# --------------------------------------------------------------------------- #
async def test_write_creates_file_atomically(workspace):
    tool = WorkspaceWriteTool(workspace)
    await tool.run(path="new.txt", content="data")
    assert (workspace / "new.txt").read_text(encoding="utf-8") == "data"
    # No leftover temp files in the workspace.
    assert not any(p.name.startswith(".ws-") for p in workspace.iterdir())


async def test_write_rejects_escape(workspace):
    tool = WorkspaceWriteTool(workspace)
    with pytest.raises(ToolError):
        await tool.run(path="../evil.txt", content="x")


async def test_write_overwrites_existing(workspace):
    tool = WorkspaceWriteTool(workspace)
    await tool.run(path="a.txt", content="replaced\n")
    assert (workspace / "a.txt").read_text(encoding="utf-8") == "replaced\n"


async def test_write_creates_parent_dirs(workspace):
    tool = WorkspaceWriteTool(workspace)
    await tool.run(path="nested/deep/c.py", content="x = 1\n")
    assert (workspace / "nested" / "deep" / "c.py").read_text(encoding="utf-8") == "x = 1\n"


async def test_write_is_atomic_no_partial_on_failure(workspace, tmp_path):
    """If the write target's parent is a symlink that escapes, the original file
    (if any) is untouched and no temp leftovers remain."""
    outside = tmp_path / "outside"
    outside.mkdir()
    link = workspace / "badlink"
    link.symlink_to(outside)
    tool = WorkspaceWriteTool(workspace)
    # resolve_under_root rejects the escaped symlink BEFORE any temp file is written.
    with pytest.raises(ToolError):
        await tool.run(path="badlink/new.txt", content="x")
    # Nothing created through the escape.
    assert not (outside / "new.txt").exists()


# --------------------------------------------------------------------------- #
# apply_patch — atomic, records accepted lines, fails cleanly
# --------------------------------------------------------------------------- #
async def test_apply_patch_add_file(workspace):
    tool = WorkspaceApplyPatchTool(workspace)
    patch = "*** Begin Patch\n*** Add File: c.py\n+print('hi')\n*** End Patch"
    res = await tool.run(patch=patch)
    assert (workspace / "c.py").read_text(encoding="utf-8").strip() == "print('hi')"
    assert res["files_changed"] >= 1


async def test_apply_patch_update_file(workspace):
    tool = WorkspaceApplyPatchTool(workspace)
    upd = (
        "*** Begin Patch\n"
        "*** Update File: a.txt\n"
        "@@ hello\n"
        "-world\n"
        "+earth\n"
        "*** End Patch"
    )
    res = await tool.run(patch=upd)
    text = (workspace / "a.txt").read_text(encoding="utf-8")
    assert "earth" in text and "world" not in text
    assert res["applied"] is True


async def test_apply_patch_failure_leaves_original_untouched(workspace):
    tool = WorkspaceApplyPatchTool(workspace)
    original = (workspace / "a.txt").read_text(encoding="utf-8")
    bad = (
        "*** Begin Patch\n"
        "*** Update File: a.txt\n"
        "@@ no_such_anchor\n"
        "-zzz\n"
        "+qqq\n"
        "*** End Patch"
    )
    with pytest.raises(ToolError):
        await tool.run(patch=bad)
    # a.txt MUST be byte-identical to before (atomic: no partial write).
    assert (workspace / "a.txt").read_text(encoding="utf-8") == original
    # No temp leftovers.
    assert not any(p.name.startswith(".ws-") for p in workspace.iterdir())


async def test_apply_patch_rejects_escape(workspace):
    tool = WorkspaceApplyPatchTool(workspace)
    patch = "*** Begin Patch\n*** Add File: ../escape.txt\n+x\n*** End Patch"
    with pytest.raises(ToolError):
        await tool.run(patch=patch)


# --------------------------------------------------------------------------- #
# Shell — non-interactive argv, timeout, output cap
# --------------------------------------------------------------------------- #
async def test_shell_runs_command(workspace, runner):
    tool = WorkspaceShellTool(workspace, runner=runner)
    res = await tool.run(command=["python", "-c", "print('ok')"], timeout=10)
    assert res["exit_code"] == 0
    assert "ok" in res["stdout"]
    assert res["timed_out"] is False


async def test_shell_rejects_string_command(workspace, runner):
    """The shell tool takes an argv LIST only — no ``sh -c <string>`` injection."""
    tool = WorkspaceShellTool(workspace, runner=runner)
    with pytest.raises(ToolError):
        await tool.run(command="python -c 'print(1)'", timeout=10)


async def test_shell_enforces_timeout(workspace, runner):
    tool = WorkspaceShellTool(workspace, runner=runner)
    res = await tool.run(
        command=["python", "-c", "import time; time.sleep(5)"], timeout=1
    )
    assert res["timed_out"] is True


async def test_shell_truncates_output(workspace, runner):
    tool = WorkspaceShellTool(workspace, runner=runner, output_limit=12)
    res = await tool.run(
        command=["python", "-c", "print('y' * 2000)"], timeout=10
    )
    assert len(res["stdout"]) <= 12


# --------------------------------------------------------------------------- #
# Git status/diff — low-risk reads via the runner (skip if git absent)
# --------------------------------------------------------------------------- #
def _git_available() -> bool:
    try:
        subprocess.run(["git", "--version"], capture_output=True, check=True)
        return True
    except (FileNotFoundError, subprocess.CalledProcessError):
        return False


@pytest.fixture
def git_workspace(tmp_path):
    if not _git_available():
        pytest.skip("git not installed")
    subprocess.run(["git", "init"], cwd=str(tmp_path), capture_output=True, check=True)
    subprocess.run(
        ["git", "config", "user.email", "t@t.test"], cwd=str(tmp_path), capture_output=True
    )
    subprocess.run(
        ["git", "config", "user.name", "Test"], cwd=str(tmp_path), capture_output=True
    )
    (tmp_path / "a.txt").write_text("hello\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=str(tmp_path), capture_output=True)
    subprocess.run(
        ["git", "commit", "-m", "init"], cwd=str(tmp_path), capture_output=True
    )
    return tmp_path


async def test_git_status_runs(git_workspace, runner):
    tool = WorkspaceGitStatusTool(git_workspace, runner=runner)
    res = await tool.run(timeout=10)
    # Either clean or listing the branch — but exit_code should be 0 for a real repo.
    assert res["exit_code"] == 0


async def test_git_diff_runs(git_workspace, runner):
    # Make a change so diff is non-empty.
    (git_workspace / "a.txt").write_text("changed\n", encoding="utf-8")
    tool = WorkspaceGitDiffTool(git_workspace, runner=runner)
    res = await tool.run(timeout=10)
    assert res["exit_code"] == 0


# --------------------------------------------------------------------------- #
# Registry — default stable, workspace tools opt-in
# --------------------------------------------------------------------------- #
def test_default_registry_is_unchanged_without_workspace_flag():
    """get_default_registry() must NOT include workspace tools (additive only)."""
    from app.tools.registry_init import get_default_registry

    reg = get_default_registry()
    names = {t.name for t in reg.list()}
    assert not any(n.startswith("workspace_") for n in names)


def test_workspace_registry_registers_confined_tools(tmp_path):
    """get_workspace_registry() binds the confined tool set to a root."""
    from app.tools.registry_init import get_workspace_registry

    reg = get_workspace_registry(tmp_path)
    names = {t.name for t in reg.list()}
    # Builtins preserved.
    assert "datetime_now" in names
    # All workspace tools present.
    for required in (
        "workspace_read",
        "workspace_list",
        "workspace_search",
        "workspace_write",
        "workspace_apply_patch",
        "workspace_shell",
        "workspace_git_status",
        "workspace_git_diff",
    ):
        assert required in names


def test_workspace_registry_excludes_builtins_when_requested(tmp_path):
    from app.tools.registry_init import get_workspace_registry

    reg = get_workspace_registry(tmp_path, include_builtins=False)
    names = {t.name for t in reg.list()}
    assert "datetime_now" not in names
    assert "workspace_read" in names


async def test_workspace_tools_in_registry_share_one_workspace_root(tmp_path):
    """Two workspace tools built from the same registry reject an escape the
    same way (confinement is bound at construction)."""
    from app.tools.registry_init import get_workspace_registry

    reg = get_workspace_registry(tmp_path)
    read_tool = reg.get("workspace_read")
    with pytest.raises(ToolError):
        await read_tool.run(path="../outside")
