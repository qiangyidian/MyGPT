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

This module also owns the one child-process environment builder
(:func:`build_child_env`). It is deliberately an **allow-list**: the backend
process holds ``DATABASE_URL`` / ``JWT_SECRET`` / ``FERNET_KEY`` / every model
API key, and an unsandboxed ``env`` command (or a Python snippet printing
``os.environ``) would exfiltrate all of them. ``os.environ`` is therefore never
inherited implicitly — a Runner passes only what this function whitelists, and
``HOME``/``TMP*`` are re-pointed at the sandbox directory so the child never
sees the real home tree either.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

# --------------------------------------------------------------------------- #
# Child-process environment allow-list
# --------------------------------------------------------------------------- #
# Locale / interpreter lookup: harmless and needed to exec anything at all.
_PASSTHROUGH_ENV: tuple[str, ...] = (
    "PATH",
    "PATHEXT",  # Windows: how `git` resolves to git.exe
    "SYSTEMROOT",  # Windows: the CRT needs it to spawn processes
    "WINDIR",
    "COMSPEC",
    "LANG",
    "LC_ALL",
    "LC_CTYPE",
    "LANGUAGE",
    "TZ",
)
# Docker *client* variables. The sandbox container itself stays --network=none;
# these only let the host-side `docker` CLI reach the dedicated runner daemon
# (deploy/k8s/sandbox-runner.yaml runs DinD on its own node pool and is reached
# over DOCKER_HOST). Never forwarded into the container.
_DOCKER_CLIENT_ENV: tuple[str, ...] = (
    "DOCKER_HOST",
    "DOCKER_TLS_VERIFY",
    "DOCKER_CERT_PATH",
    "DOCKER_CONFIG",
)
# Re-pointed at the per-command sandbox dir instead of inherited (the backend's
# HOME holds ~/.ssh, ~/.aws, pip tokens...).
_SANDBOX_LOCAL_VARS: tuple[str, ...] = ("HOME", "TMPDIR", "TMP", "TEMP")
# Interpreter hygiene: readable output immediately, no .pyc litter inside the
# workspace, deterministic codec. The hijack vectors (PYTHONPATH, PYTHONSTARTUP,
# PYTHONHOME, PYTHONEXECUTABLE, …) are simply never forwarded — they are not in
# any allow-list above.
_INTERPRETER_DEFAULTS: dict[str, str] = {
    "PYTHONUNBUFFERED": "1",
    "PYTHONDONTWRITEBYTECODE": "1",
    "PYTHONIOENCODING": "utf-8",
}


def build_child_env(
    *,
    cwd: str | os.PathLike[str] | None = None,
    extra: dict[str, str] | None = None,
    base: dict[str, str] | None = None,
    include_docker_client: bool = False,
) -> dict[str, str]:
    """Build the ONLY environment a sandboxed child may see (allow-list, not deny).

    ``base`` defaults to ``os.environ`` and is injectable so tests can prove that
    a secret-shaped variable never survives. Unknown shapes (non-str keys/values)
    are dropped rather than coerced — fail closed.
    """
    source = os.environ if base is None else base
    keys = list(_PASSTHROUGH_ENV)
    if include_docker_client:
        keys += list(_DOCKER_CLIENT_ENV)

    env: dict[str, str] = {}
    for key in keys:
        try:
            value = source.get(key)
        except Exception:  # pragma: no cover - a hostile mapping impl
            continue
        if isinstance(value, str) and value:
            env[key] = value
    if cwd is not None:
        home = str(cwd)
        for key in _SANDBOX_LOCAL_VARS:
            env[key] = home
    env.update(_INTERPRETER_DEFAULTS)
    for key, value in (extra or {}).items():
        # 调用方点名的变量也必须是非空字符串；白名单只约束「隐式继承」，
        # 不会替调用方把它明确传进来的值再过滤一遍（所以 extra 只放必要项）。
        if isinstance(key, str) and isinstance(value, str) and value:
            env[key] = value
    return env


class RunnerError(RuntimeError):
    """Raised when a Runner refuses to run (e.g. LocalRunner in production)."""


@dataclass(frozen=True)
class RunResult:
    """The outcome of a single command execution."""

    stdout: str
    stderr: str
    exit_code: int
    timed_out: bool = False
    # True only when the requested CPU/memory/output caps were ACTUALLY applied
    # by the platform. A runner that could not enforce them must either refuse to
    # exec (fail closed) or report False here — never pretend success.
    limits_enforced: bool = False

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
      * truncate ``stdout``/``stderr`` to ``output_limit`` characters;
      * never inherit the backend process environment implicitly (see
        :func:`build_child_env`);
      * either enforce CPU/memory caps or refuse to run (:class:`RunnerError`).
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


__all__ = ["RunResult", "Runner", "RunnerError", "build_child_env"]
