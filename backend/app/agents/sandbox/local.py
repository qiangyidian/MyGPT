"""LocalRunner — subprocess-backed Runner for DEVELOPMENT/TEST only.

This is NOT a real sandbox: the command runs with the backend process's own
uid/gid, filesystem view, and network. It exists so the workspace tools can be
exercised end-to-end in dev without docker. To make sure it can never become an
accidental production code-exec path, :meth:`run` hard-refuses any environment
that is not ``dev`` or ``test`` (raise :class:`RunnerError`).

Production deployments use :class:`~app.agents.sandbox.docker.DockerRunner`.
"""
from __future__ import annotations

import asyncio
from typing import Any

from app.agents.sandbox.base import RunnerError, RunResult

# Environments in which the unsandboxed LocalRunner may exec.
_ALLOWED_ENVS: frozenset[str] = frozenset({"dev", "test"})


class LocalRunner:
    """Subprocess Runner; refuses to run outside dev/test.

    The effective environment is resolved, in order:

      1. an explicit ``env`` argument (test injection);
      2. an explicit ``settings`` argument's ``.ENV``;
      3. the global :func:`~app.core.config.get_settings`.

    so tests can pin the gate without constructing a full Settings object.
    """

    def __init__(
        self,
        *,
        env: str | None = None,
        settings: Any | None = None,
    ) -> None:
        self._env = env
        self._settings = settings

    def _effective_env(self) -> str:
        if self._env is not None:
            return self._env
        if self._settings is not None:
            return getattr(self._settings, "ENV", "dev")
        # Late import keeps this module importable without the app config stack.
        from app.core.config import get_settings

        return get_settings().ENV

    async def run(
        self,
        command: list[str],
        *,
        cwd: str | None = None,
        env: dict[str, str] | None = None,
        timeout: float = 30.0,
        output_limit: int = 8192,
    ) -> RunResult:
        env_now = self._effective_env()
        if env_now not in _ALLOWED_ENVS:
            raise RunnerError(
                "local runner refuses non-development environments "
                f"(ENV={env_now!r}); configure the Docker sandbox for production"
            )
        if not isinstance(command, list) or not command:
            raise RunnerError("command must be a non-empty argv list")
        try:
            proc = await asyncio.create_subprocess_exec(
                *command,
                cwd=cwd,
                env=env,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except FileNotFoundError as exc:
            # Missing binary — surface as a clean non-zero result, not a crash.
            return RunResult(stdout="", stderr=str(exc), exit_code=127, timed_out=False)

        try:
            stdout_b, stderr_b = await asyncio.wait_for(
                proc.communicate(), timeout=timeout
            )
        except asyncio.TimeoutError:
            # Reap the timed-out child so it cannot outlive the call.
            try:
                proc.kill()
            except ProcessLookupError:
                pass
            await proc.wait()
            return RunResult(
                stdout="", stderr=f"timed out after {timeout}s", exit_code=-1, timed_out=True
            )

        stdout = (stdout_b or b"").decode("utf-8", errors="replace")
        stderr = (stderr_b or b"").decode("utf-8", errors="replace")
        return RunResult(
            stdout=stdout[:output_limit],
            stderr=stderr[:output_limit],
            exit_code=proc.returncode if proc.returncode is not None else -1,
            timed_out=False,
        )


__all__ = ["LocalRunner"]
