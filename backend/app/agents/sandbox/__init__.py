"""Sandbox runners for workspace tooling (Task 8).

Two implementations of the :class:`Runner` protocol:

* :class:`LocalRunner <app.agents.sandbox.local.LocalRunner>` — a subprocess
  runner used ONLY in development/test. It execs commands with the backend
  process's privileges, so it hard-refuses to run outside dev/test environments
  (and, since the production wiring landed, also refuses to run where its CPU /
  memory caps cannot be applied — i.e. Windows — unless an operator explicitly
  opts out via ``SANDBOX_REQUIRE_LIMITS=false``).
* :class:`DockerRunner <app.agents.sandbox.docker.DockerRunner>` — the
  production runner. It builds an enterprise-isolated ``docker run`` command
  (read-only root FS, ``--cap-drop=ALL``, ``--network=none``, bounded
  CPU/memory/PIDs) and executes it. The command builder is a pure function so
  its flags can be asserted without docker installed.

:class:`app.agents.sandbox.factory` is the ONLY place a runner gets constructed
from configuration (``SANDBOX_MODE`` + the permission profile); both
:mod:`app.tools.registry_init` and the ``python_exec`` tool take their runner
from there instead of each picking a default.

Workspace tools (reads/search/write/patch/shell/git) live in
:mod:`app.tools.workspace` and go through whichever Runner they are given.
"""
from app.agents.sandbox.base import Runner, RunnerError, RunResult, build_child_env
from app.agents.sandbox.factory import (
    SandboxConfigError,
    build_docker_config,
    get_sandbox_runner,
    permission_policy,
    runner_descriptor,
    sandbox_mode,
)

__all__ = [
    "RunResult",
    "Runner",
    "RunnerError",
    "SandboxConfigError",
    "build_child_env",
    "build_docker_config",
    "get_sandbox_runner",
    "permission_policy",
    "runner_descriptor",
    "sandbox_mode",
]
