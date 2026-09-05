"""Runner protocol + shared types for the sandbox layer.

A :class:`Runner` is the isolated execution primitive workspace tools shell out
through (non-interactive shell, ``git status``, ``git diff``). It owns exactly
one concern: run an argv list under resource/time/output limits and report the
captured result. Path confinement, patch parsing, and tool-level policy all live
in the tools layer; a Runner never touches the workspace filesystem directly
beyond what the command it runs does.

The integration with Task 3's per-run ``max_tool_output_chars`` budget happens
in the ToolGateway, NOT here — the runner's own ``output_limit`` is a separate
hard cap on a single command's stdout/stderr.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable


class RunnerError(RuntimeError):
    """Raised when a Runner refuses to run (e.g. LocalRunner in production)."""


@dataclass(frozen=True)
class RunResult:
    """The outcome of a single command execution."""

    stdout: str
    stderr: str
    exit_code: int
    timed_out: bool = False

    @property
    def ok(self) -> bool:
        return self.exit_code == 0 and not self.timed_out


@runtime_checkable
class Runner(Protocol):
    """Execute a non-interactive command under isolation + resource limits.

    Implementations MUST:
      * accept an argv list (never a shell string) so command injection via
        ``sh -c`` is impossible;
      * enforce ``timeout`` and report ``timed_out=True`` on expiry;
      * truncate ``stdout``/``stderr`` to ``output_limit`` characters.
    """

    async def run(
        self,
        command: list[str],
        *,
        cwd: str | None = None,
        env: dict[str, str] | None = None,
        timeout: float = 30.0,
        output_limit: int = 8192,
    ) -> RunResult:
        ...  # pragma: no cover


__all__ = ["RunResult", "Runner", "RunnerError"]
