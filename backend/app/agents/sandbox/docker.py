"""DockerRunner — production sandbox runner.

The command construction is split into a PURE function
(:func:`build_docker_command`) so the enterprise-isolation flags can be asserted
without docker installed. :class:`DockerRunner.run` is the thin executor that
shells the built argv out to ``docker run`` and parses the result; it is only
exercised by an opt-in integration test (skip if docker is absent).

Enterprise isolation defaults (all asserted by the policy tests):

  * ``--network=none``           — default-deny network (no egress).
  * ``--read-only``              — root filesystem immutable.
  * ``--cap-drop=ALL``           — drop every Linux capability.
  * ``--security-opt=no-new-privileges`` — forbid privilege escalation.
  * ``--user nobody``            — never run as root.
  * ``--memory``/``--cpus``/``--pids-limit`` — bounded CPU/mem/PIDs.
  * ``--tmpfs /tmp``             — a writable scratch dir under the read-only root.
  * bind-mount the host workspace read-write at a fixed container path and
    ``-w`` there.
"""
from __future__ import annotations

import asyncio
import shutil
from dataclasses import dataclass
from pathlib import Path

from app.agents.sandbox.base import RunnerError, RunResult


@dataclass(frozen=True)
class DockerRunnerConfig:
    """Configuration for :func:`build_docker_command` / :class:`DockerRunner`.

    Defaults are intentionally restrictive; an operator relaxes a flag
    deliberately (e.g. ``network_none=False`` to allow egress), which is
    audit-visible in the produced argv.
    """

    image: str
    workspace_mount: str = "/workspace"
    # Resource limits.
    cpu_quota: float = 1.0
    memory_mb: int = 512
    pids_limit: int = 64
    timeout_s: int = 30
    output_limit: int = 8192
    # Isolation toggles (default-most-restrictive).
    read_only: bool = True
    network_none: bool = True
    cap_drop_all: bool = True
    no_new_privileges: bool = True
    user: str = "nobody"


def build_docker_command(
    cfg: DockerRunnerConfig,
    command: list[str],
    host_workspace: str | Path,
) -> list[str]:
    """Build the full ``docker run ...`` argv for ``command`` (PURE).

    The returned list is suitable for ``subprocess.run(argv, ...)`` /
    ``asyncio.create_subprocess_exec(*argv, ...)``. It is intentionally a flat
    argv list (no shell) so the host command tail is never re-interpreted by a
    shell. Does NOT execute docker.
    """
    if not isinstance(command, list) or not command:
        raise RunnerError("command must be a non-empty argv list")
    if not cfg.image:
        raise RunnerError("DockerRunnerConfig.image must be set")

    host = str(host_workspace)
    argv: list[str] = ["docker", "run", "--rm"]

    # Network.
    if cfg.network_none:
        argv.append("--network=none")
    # Filesystem.
    if cfg.read_only:
        argv.append("--read-only")
        # Keep a writable scratch dir even on a read-only root FS.
        argv += ["--tmpfs", "/tmp:rw,noexec,nosuid,size=64m"]
    # Capabilities + privilege escalation.
    if cfg.cap_drop_all:
        argv.append("--cap-drop=ALL")
    if cfg.no_new_privileges:
        argv.append("--security-opt=no-new-privileges")
    # Non-root user.
    argv += ["--user", cfg.user]
    # Bounded resources.
    argv += ["--memory", f"{cfg.memory_mb}m"]
    argv += ["--cpus", str(cfg.cpu_quota)]
    argv += ["--pids-limit", str(cfg.pids_limit)]
    # Workspace bind mount + working directory.
    argv += ["-v", f"{host}:{cfg.workspace_mount}:rw"]
    argv += ["-w", cfg.workspace_mount]
    # Image + command tail.
    argv.append(cfg.image)
    argv += list(command)
    return argv


class DockerRunner:
    """Runner that execs the enterprise-isolated ``docker run`` argv.

    Not exercised by unit tests (docker may be absent); integration tests guard
    on ``shutil.which('docker')``.
    """

    def __init__(self, cfg: DockerRunnerConfig, *, env: dict[str, str] | None = None) -> None:
        self._cfg = cfg
        self._env = env

    async def run(
        self,
        command: list[str],
        *,
        cwd: str | None = None,
        env: dict[str, str] | None = None,
        timeout: float | None = None,
        output_limit: int | None = None,
    ) -> RunResult:
        if not shutil.which("docker"):
            raise RunnerError("docker binary not found on PATH")
        # ``cwd`` here is the HOST workspace path (mounted into the container);
        # the container cwd is set via ``-w`` on the builder. Require it
        # explicitly — falling back to ``"."`` would silently mount the docker
        # daemon's CWD, which is not a confinement boundary the caller intends.
        if not cwd:
            raise RunnerError("DockerRunner.run requires an explicit cwd (host workspace)")
        host_workspace = cwd
        limit = output_limit if output_limit is not None else self._cfg.output_limit
        to = timeout if timeout is not None else self._cfg.timeout_s
        argv = build_docker_command(self._cfg, command, host_workspace)

        try:
            proc = await asyncio.create_subprocess_exec(
                *argv,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=env or self._env,
            )
        except FileNotFoundError as exc:
            return RunResult(stdout="", stderr=str(exc), exit_code=127, timed_out=False)

        try:
            stdout_b, stderr_b = await asyncio.wait_for(proc.communicate(), timeout=to)
        except TimeoutError:
            try:
                proc.kill()
            except ProcessLookupError:
                pass
            await proc.wait()
            return RunResult(
                stdout="", stderr=f"timed out after {to}s", exit_code=-1, timed_out=True
            )

        stdout = (stdout_b or b"").decode("utf-8", errors="replace")
        stderr = (stderr_b or b"").decode("utf-8", errors="replace")
        return RunResult(
            stdout=stdout[:limit],
            stderr=stderr[:limit],
            exit_code=proc.returncode if proc.returncode is not None else -1,
            timed_out=False,
        )


__all__ = ["DockerRunner", "DockerRunnerConfig", "build_docker_command"]
