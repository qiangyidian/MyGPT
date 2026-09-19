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
  * bind-mount the host workspace at a fixed container path (``rw`` only when the
    permission profile grants ``fs_write``) and ``-w`` there.

Deployment note: the daemon this talks to is NOT the app node's docker socket.
``deploy/k8s/sandbox-runner.yaml`` pins a privileged Docker-in-Docker daemon to a
dedicated, tainted node pool and the API/worker reach it over ``DOCKER_HOST`` —
so a container escape is confined to throwaway runner pods, never the data plane.
Keep that constraint when changing how a runner is constructed.

The container never sees the backend process environment: only the *host-side*
``docker`` CLI gets an environment here, and it is built by
:func:`~app.agents.sandbox.base.build_child_env` (PATH + locale + the ``DOCKER_*``
client vars, nothing else).
"""
from __future__ import annotations

import asyncio
import logging
import shutil
import uuid
from dataclasses import dataclass
from pathlib import Path

from app.agents.sandbox.base import build_child_env, RunnerError, RunResult

logger = logging.getLogger(__name__)

# How long we wait for the `docker kill` / the CLI to notice before giving up.
_KILL_GRACE_S = 5.0


@dataclass(frozen=True)
class DockerRunnerConfig:
    """Configuration for :func:`build_docker_command` / :class:`DockerRunner`.

    Defaults are intentionally restrictive; an operator relaxes a flag
    deliberately (e.g. ``network_none=False`` to allow egress), which is
    audit-visible in the produced argv. ``read_only``/``network_none``/
    ``workspace_mount_ro`` are compiled from the permission profile
    (:mod:`app.agents.permission_profiles`) by the runner factory;
    ``cap_drop_all``/``no_new_privileges``/``user`` are NOT operator-tunable at
    runtime — the factory pins them to the restrictive values.
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
    # Mount the workspace read-only (permission profile without fs_write).
    workspace_mount_ro: bool = False


def build_docker_command(
    cfg: DockerRunnerConfig,
    command: list[str],
    host_workspace: str | Path,
    *,
    name: str | None = None,
) -> list[str]:
    """Build the full ``docker run ...`` argv for ``command`` (PURE).

    The returned list is suitable for ``subprocess.run(argv, ...)`` /
    ``asyncio.create_subprocess_exec(*argv, ...)``. It is intentionally a flat
    argv list (no shell) so the host command tail is never re-interpreted by a
    shell. Does NOT execute docker. ``name`` sets ``--name`` so :class:`DockerRunner`
    can kill the CONTAINER (not just the CLI) when a command overruns its timeout.
    """
    if not isinstance(command, list) or not command:
        raise RunnerError("command must be a non-empty argv list")
    if not cfg.image:
        raise RunnerError("DockerRunnerConfig.image must be set")

    host = str(host_workspace)
    argv: list[str] = ["docker", "run", "--rm"]
    if name:
        argv += ["--name", name]

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
    # Workspace bind mount + working directory. The mount is read-write ONLY when
    # the permission profile grants fs_write; otherwise the container cannot
    # mutate the host workspace at all.
    argv += ["-v", f"{host}:{cfg.workspace_mount}:{'ro' if cfg.workspace_mount_ro else 'rw'}"]
    argv += ["-w", cfg.workspace_mount]
    # Image + command tail.
    argv.append(cfg.image)
    argv += list(command)
    return argv


class DockerRunner:
    """Runner that execs the enterprise-isolated ``docker run`` argv.

    Not exercised by unit tests (docker may be absent); integration tests guard
    on ``shutil.which('docker')``. The isolation/caps come from the config, which
    the runner factory compiles from settings + the permission profile — this
    class deliberately has no "relax everything" switch of its own.
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
        """Exec ``command`` inside a throwaway isolated container.

        ``cwd`` is the HOST directory bind-mounted at the configured container
        path (it must exist and be the confined workspace), and ``env`` — unlike
        in :class:`~app.agents.sandbox.local.LocalRunner` — only augments the
        host-side ``docker`` CLI environment; the container never receives it.
        """
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
        # A name we can `docker kill`: killing only the CLI on a timeout would
        # leave the container burning CPU/memory on the runner node forever.
        container_name = f"mygpt-sandbox-{uuid.uuid4().hex[:16]}"
        argv = build_docker_command(self._cfg, command, host_workspace, name=container_name)

        # env 只作为「额外项」并进宿主机 docker CLI 的环境（含 DOCKER_HOST 这类
        # 客户端变量）；容器内的环境由镜像自己决定 —— 调用方与后端进程的秘密
        # 都进不去容器。
        cli_env = self._env or build_child_env(
            cwd=None, extra=env, include_docker_client=True
        )

        try:
            proc = await asyncio.create_subprocess_exec(
                *argv,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=cli_env,
            )
        except FileNotFoundError as exc:
            return RunResult(
                stdout="",
                stderr=str(exc),
                exit_code=127,
                timed_out=False,
                limits_enforced=True,
            )

        try:
            stdout_b, stderr_b = await asyncio.wait_for(proc.communicate(), timeout=to)
        except TimeoutError:
            await self._kill_container(container_name)
            try:
                await asyncio.wait_for(proc.wait(), timeout=_KILL_GRACE_S)
            except TimeoutError:
                try:
                    proc.kill()
                except ProcessLookupError:
                    pass
                await proc.wait()
            return RunResult(
                stdout="", stderr=f"timed out after {to}s", exit_code=-1, timed_out=True,
                limits_enforced=True,
            )

        stdout = (stdout_b or b"").decode("utf-8", errors="replace")
        stderr = (stderr_b or b"").decode("utf-8", errors="replace")
        return RunResult(
            stdout=stdout[:limit],
            stderr=stderr[:limit],
            exit_code=proc.returncode if proc.returncode is not None else -1,
            timed_out=False,
            limits_enforced=True,
        )

    async def _kill_container(self, name: str) -> None:
        """Best-effort ``docker kill`` of our own named container.

        Failure is not fatal here: the caller still reaps/kills the CLI, and the
        container was started with ``--rm`` + bounded resources. It IS logged,
        because a container that outlives its tool call is a leak ops must see.
        """
        try:
            killer = await asyncio.create_subprocess_exec(
                "docker", "kill", name,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
                env=self._env or build_child_env(cwd=None, include_docker_client=True),
            )
            await asyncio.wait_for(killer.wait(), timeout=_KILL_GRACE_S)
        except Exception:
            # 尽力而为的回收：不能因为 kill 失败就把原本的超时结果改掉。
            logger.warning("sandbox: 超时后无法 kill 容器 %s（需人工核对泄漏）", name)


__all__ = ["DockerRunner", "DockerRunnerConfig", "build_docker_command"]
