"""Regression tests for the /api/tools/test ad-hoc execution path.

Covers the RCE fixed in this commit: ``POST /api/tools/test`` used to call
``tool.run()`` directly, bypassing the ToolGateway, so ``python_exec`` (which
has no internal env gate) was arbitrary code execution for any authenticated
user in production. Both layers must now refuse it:

  * the service-layer ``is_tool_allowed`` gate in ``tool_service.test_tool``
  * the defense-in-depth self-guard inside ``PythonExecTool.run``

The test env is ``ENV=test`` (``is_dev=False``) with no opt-in, so
``is_tool_allowed("python_exec", None)`` is False here — the production posture.
"""
from __future__ import annotations

import pytest

from app.agents.policies.tool_policy import is_tool_allowed
from app.services import tool_service
from app.tools.builtin import PythonExecTool


@pytest.mark.asyncio
async def test_test_tool_blocks_python_exec() -> None:
    """The ad-hoc test endpoint must refuse python_exec in non-dev envs."""
    # Sanity: the test env really is fail-closed for python_exec.
    assert is_tool_allowed("python_exec", None) is False

    result = await tool_service.test_tool(
        "python_exec", {"code": "import os; os.system('echo pwned')"}, user=None
    )
    assert result.ok is False
    assert result.result is None
    assert result.error is not None
    assert "not permitted" in result.error or "disabled" in result.error.lower()


@pytest.mark.asyncio
async def test_python_exec_tool_self_guard() -> None:
    """PythonExecTool.run must self-guard even if a caller bypasses the gateway."""
    tool = PythonExecTool()
    result = await tool.run(code="print('should not run')")
    assert result.get("ok") is False
    # Self-guard returns the structured 'blocked' envelope (not the normal output).
    assert result.get("blocked") is True
    assert result.get("stdout") == ""
    assert result.get("returncode") is None


@pytest.mark.asyncio
async def test_test_tool_unknown_tool_is_error() -> None:
    """An unknown tool name surfaces as an error result (not a crash)."""
    result = await tool_service.test_tool("no_such_tool", {}, user=None)
    assert result.ok is False
    assert result.error is not None
