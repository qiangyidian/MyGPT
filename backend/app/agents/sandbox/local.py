"""LocalRunner — subprocess-backed Runner for DEVELOPMENT/TEST only.

This is NOT a real sandbox: the command runs with the backend process's own
uid/gid, filesystem view, and network. It exists so the workspace tools can be
exercised end-to-end in dev without docker. To make sure it can never become an
accidental production code-exec path, :meth:`run` hard-refuses any environment
that is not ``dev`` or ``test`` (raise :class:`RunnerError`).

Two more hard rules were added when this runner got wired into the production
code path (see :mod:`app.agents.sandbox.factory`), because "dev-only" turned out
to be a weaker promise than it sounds:

  * **no implicit environment**: the child gets :func:`build_child_env` output
    only (PATH/locale-shaped variables), never ``os.environ``. The backend
    process holds the DB DSN, the JWT secret, the Fernet key and every model API
    key, and one ``env``/``os.environ`` in a snippet would dump them all.
  * **resource caps or refusal**: CPU time (``RLIMIT_CPU``), address space
    (``RLIMIT_AS``), output file size (``RLIMIT_FSIZE``), no core dumps, plus a
    wall-clock timeout that kills the whole process group. Caps are applied in
    the child itself (a tiny ``exec``-after-``setrlimit`` wrapper, so no
    ``preexec_fn`` fork-in-a-threaded-process hazard). On platforms where
    ``resource`` does not exist — i.e. **Windows** — there is no equivalent
    means, so with ``require_limits=True`` (the default the factory uses) the
    runner **refuses to exec** instead of reporting a run it never bounded.

Production deployments use :class:`~app.agents.sandbox.docker.DockerRunner`.
"""
from __future__ import annotations

import asyncio
import json
import os
import signal
import sys
from typing import Any

from app.agents.sandbox.base import build_child_env, RunnerError, RunResult

# Environments in which the unsandboxed LocalRunner may exec.
_ALLOWED_ENVS: frozenset[str] = frozenset({"dev", "test"})


def rlimits_supported() -> bool:
    """True when this platform can apply POSIX resource limits at all."""
    return os.name == "posix" and hasattr(os, "fork")


def build_rlimit_wrapper(
    command: list[str],
    *,
    memory_mb: int | None = None,
    cpu_seconds: int | None = None,
    max_fsize_mb: int | None = None,
    interpreter: str | None = None,
) -> list[str]:
    """Wrap ``command`` in a argv-only ``setrlimit`` + ``exec`` preamble (PURE).

    Returns ``[python, "-c", WRAPPER, <limits-json>, *command]``. The wrapper runs
    first, tightens its own limits (exiting non-zero if it cannot), then
    ``execv``'s the real command — so the caps apply to the command and every
    child it spawns, without ``preexec_fn`` (unsafe in a threaded parent) and
    without ever going through a shell.
    """
    if not isinstance(command, list) or not command:
        raise RunnerError("command must be a non-empty argv list")
    payload = json.dumps(
        {
            "memory_mb": int(memory_mb) if memory_mb else None,
            "cpu_seconds": int(cpu_seconds) if cpu_seconds else None,
            "max_fsize_mb": int(max_fsize_mb) if max_fsize_mb else None,
        },
        separators=(",", ":"),
    )
    return [interpreter or sys.executable, "-c", _RLIMIT_WRAPPER_SRC, payload, *command]


# Executed in the child. stdlib only; any failure to tighten a limit is fatal
# (exit 126) rather than "continue unbounded" — fail closed.
_RLIMIT_WRAPPER_SRC = """
import json, os, resource, sys

limits = json.loads(sys.argv[1])
argv = sys.argv[2:]


def tighten(name, want):
    if not want:
        return
    res = getattr(resource, name)
    try:
        soft, hard = resource.getrlimit(res)
        target = int(want)
        if hard != resource.RLIM_INFINITY and target > hard:
            target = hard
        resource.setrlimit(res, (target, hard))
    except (ValueError, OSError, AttributeError) as exc:
        sys.stderr.write("sandbox: 无法施加 %s 限额: %s\\n" % (name, exc))
        sys.exit(126)


tighten("RLIMIT_AS", limits.get("memory_mb") and int(limits["memory_mb"]) * 1024 * 1024)
tighten("RLIMIT_CPU", limits.get("cpu_seconds"))
tighten("RLIMIT_FSIZE", limits.get("max_fsize_mb") and int(limits["max_fsize_mb"]) * 1024 * 1024)
tighten("RLIMIT_CORE", 0)
os.execv(argv[0], argv)
"""


class LocalRunner:
    """Subprocess Runner; refuses to run outside dev/test and without caps.

    The effective environment is resolved, in order:

      1. an explicit ``env`` argument (test injection);
      2. an explicit ``settings`` argument's ``.ENV``;
      3. the global :func:`~app.core.config.get_settings`.

    so tests can pin the gate without constructing a full Settings object.
    ``memory_mb`` / ``cpu_seconds`` / ``max_fsize_mb`` are the per-command caps;
    ``require_limits`` decides what happens when the platform cannot apply them.
    """

    def __init__(
        self,
        *,
        env: str | None = None,
        settings: Any | None = None,
        memory_mb: int | None = None,
        cpu_seconds: int | None = None,
        max_fsize_mb: int | None = None,
        require_limits: bool = False,
        caps_supported: Any | None = None,
    ) -> None:
        self._env = env
        self._settings = settings
        self._memory_mb = memory_mb
        self._cpu_seconds = cpu_seconds
        self._max_fsize_mb = max_fsize_mb
        self._require_limits = require_limits
        # Injectable so the "platform cannot enforce" branch is testable on any OS.
        self._caps_supported = caps_supported if callable(caps_supported) else rlimits_supported

    def _effective_env(self) -> str:
        if self._env is not None:
            return self._env
        if self._settings is not None:
            return getattr(self._settings, "ENV", "dev")
        # Late import keeps this module importable without the app config stack.
        from app.core.config import get_settings

        return get_settings().ENV

    # ------------------------------------------------------------------ #
    def _guard_caps(self) -> None:
        """Fail closed when hard caps were demanded but the OS cannot do it."""
        if self._require_limits and not self._caps_supported():
            raise RunnerError(
                "local 模式无法在当前平台施加 CPU/内存/文件大小硬限额"
                "（POSIX resource 模块不可用，Windows 无等价手段），"
                "已按 fail-closed 拒绝执行；请改用 SANDBOX_MODE=docker"
            )

    def _spawn_command(self, command: list[str]) -> tuple[list[str], bool]:
        """Return the argv to exec + whether caps were actually applied."""
        self._guard_caps()
        wants_caps = bool(self._memory_mb or self._cpu_seconds or self._max_fsize_mb)
        if wants_caps and self._caps_supported():
            cpu = self._cpu_seconds
            return (
                build_rlimit_wrapper(
                    command,
                    memory_mb=self._memory_mb,
                    cpu_seconds=cpu,
                    max_fsize_mb=self._max_fsize_mb,
                ),
                True,
            )
        return list(command), False

    @staticmethod
    def _popen_kwargs() -> dict[str, Any]:
        """Own process group on POSIX so a timeout can kill the whole tree.

        ``start_new_session`` is POSIX-only; passing it on Windows raises, so the
        kwarg is built per platform instead of being swallowed there.
        """
        if os.name == "posix":
            return {"start_new_session": True}
        return {}

    @staticmethod
    async def _terminate_tree(proc: Any) -> None:
        """Kill a timed-out child: the whole process group on POSIX, else itself.

        ``proc.kill()`` only reaps the direct child, so a snippet that spawned
        workers keeps running after the tool returns — the exact leak a wall-clock
        timeout is supposed to prevent.
        """
        killed_group = False
        if os.name == "posix" and proc.pid:
            try:
                os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
                killed_group = True
            except (ProcessLookupError, PermissionError, OSError):
                killed_group = False
        if not killed_group:
            try:
                proc.kill()
            except ProcessLookupError:
                pass

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
        argv, limits_enforced = self._spawn_command(command)
        # 绝不隐式继承后端进程环境：调用方没给 env 时也要过白名单。
        child_env = env if env is not None else build_child_env(cwd=cwd)
        try:
            proc = await asyncio.create_subprocess_exec(
                *argv,
                cwd=cwd,
                env=child_env,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                **self._popen_kwargs(),
            )
        except FileNotFoundError as exc:
            # Missing binary — surface as a clean non-zero result, not a crash.
            return RunResult(
                stdout="",
                stderr=str(exc),
                exit_code=127,
                timed_out=False,
                limits_enforced=limits_enforced,
            )

        try:
            stdout_b, stderr_b = await asyncio.wait_for(
                proc.communicate(), timeout=timeout
            )
        except TimeoutError:
            # Reap the timed-out child (and its process group) so it cannot
            # outlive the call.
            await self._terminate_tree(proc)
            await proc.wait()
            return RunResult(
                stdout="",
                stderr=f"timed out after {timeout}s",
                exit_code=-1,
                timed_out=True,
                limits_enforced=limits_enforced,
            )

        stdout = (stdout_b or b"").decode("utf-8", errors="replace")
        stderr = (stderr_b or b"").decode("utf-8", errors="replace")
        return RunResult(
            stdout=stdout[:output_limit],
            stderr=stderr[:output_limit],
            exit_code=proc.returncode if proc.returncode is not None else -1,
            timed_out=False,
            limits_enforced=limits_enforced,
        )


__all__ = ["LocalRunner", "build_rlimit_wrapper", "rlimits_supported"]
