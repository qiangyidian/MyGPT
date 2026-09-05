"""Sandbox runners for workspace tooling (Task 8).

Two implementations of the :class:`Runner` protocol:

* :class:`LocalRunner <app.agents.sandbox.local.LocalRunner>` — a subprocess
  runner used ONLY in development/test. It execs commands with the backend
  process's privileges, so it hard-refuses to run outside dev/test environments.
* :class:`DockerRunner <app.agents.sandbox.docker.DockerRunner>` — the
  production runner. It builds an enterprise-isolated ``docker run`` command
  (read-only root FS, ``--cap-drop=ALL``, ``--network=none``, bounded
  CPU/memory/PIDs) and executes it. The command builder is a pure function so
  its flags can be asserted without docker installed.

Workspace tools (reads/search/write/patch/shell/git) live in
:mod:`app.tools.workspace` and go through whichever Runner they are given.
"""
from app.agents.sandbox.base import Runner, RunnerError, RunResult

__all__ = ["RunResult", "Runner", "RunnerError"]
