"""Sandbox runner policy: docker command-builder flags + LocalRunner dev gate.

The docker command builder is a PURE function — it produces the ``docker run``
argv list without ever invoking docker, so these tests run anywhere. The
LocalRunner (the only runner that actually execs) must refuse non-dev/test
environments so the unsandboxed subprocess path can never run in production.
"""
from __future__ import annotations

import pytest

from app.agents.sandbox.base import RunnerError, RunResult
from app.agents.sandbox.docker import DockerRunnerConfig, build_docker_command
from app.agents.sandbox.local import LocalRunner


# --------------------------------------------------------------------------- #
# Docker command builder — enterprise isolation flags (no docker needed)
# --------------------------------------------------------------------------- #
def test_docker_runner_has_enterprise_isolation_flags():
    cfg = DockerRunnerConfig(image="sandbox:latest", workspace_mount="/ws")
    cmd = build_docker_command(cfg, ["python", "script.py"], "/host/ws")

    # Default-deny network.
    assert "--network=none" in cmd
    # Read-only root filesystem.
    assert "--read-only" in cmd
    # Drop ALL capabilities + forbid privilege escalation.
    assert "--cap-drop=ALL" in cmd
    assert "--security-opt=no-new-privileges" in cmd


def test_docker_runner_runs_as_non_root_user():
    cfg = DockerRunnerConfig(image="sandbox:latest", workspace_mount="/ws")
    cmd = build_docker_command(cfg, ["ls"], "/host/ws")
    i = cmd.index("--user")
    uid = cmd[i + 1]
    assert uid not in ("root", "0"), "container must not run as root"


def test_docker_runner_has_bounded_resources():
    cfg = DockerRunnerConfig(
        image="sandbox:latest",
        workspace_mount="/ws",
        memory_mb=512,
        cpu_quota=1.0,
        pids_limit=64,
    )
    cmd = build_docker_command(cfg, ["ls"], "/host/ws")
    assert "--memory" in cmd
    i_mem = cmd.index("--memory")
    assert cmd[i_mem + 1].endswith("m")
    assert "--pids-limit" in cmd
    assert "--cpus" in cmd


def test_docker_runner_mounts_workspace_read_only_root_but_writable_mount():
    cfg = DockerRunnerConfig(image="sandbox:latest", workspace_mount="/ws")
    cmd = build_docker_command(cfg, ["ls"], "/host/ws")
    # There must be a bind mount of the host workspace -> container workspace.
    mounts = [c for c in cmd if c.startswith("/host/ws:")]
    assert mounts, "expected a -v bind mount of the host workspace"
    # Mount shape is "<host>:<container>:rw" — the container path is /ws.
    assert any("/ws" in m.split(":")[1] for m in mounts), mounts
    # Working directory inside the container is the mount.
    iw = cmd.index("-w") if "-w" in cmd else cmd.index("--workdir")
    assert cmd[iw + 1] == "/ws"


def test_docker_command_appends_image_and_command():
    cfg = DockerRunnerConfig(image="sandbox:latest", workspace_mount="/ws")
    cmd = build_docker_command(cfg, ["python", "-c", "print(1)"], "/host/ws")
    # Image name precedes the user command tail.
    assert "sandbox:latest" in cmd
    img_i = cmd.index("sandbox:latest")
    assert cmd[img_i + 1 :] == ["python", "-c", "print(1)"]


def test_docker_command_uses_tmpfs_for_writable_dirs_when_read_only():
    """A read-only root FS still needs a writable /tmp; the builder adds a tmpfs."""
    cfg = DockerRunnerConfig(image="sandbox:latest", workspace_mount="/ws", read_only=True)
    cmd = build_docker_command(cfg, ["ls"], "/host/ws")
    assert any(c == "--tmpfs" for c in cmd)


def test_docker_command_skips_network_none_when_disabled():
    """If an operator explicitly enables network, the flag is omitted (audit-visible)."""
    cfg = DockerRunnerConfig(image="sandbox:latest", workspace_mount="/ws", network_none=False)
    cmd = build_docker_command(cfg, ["ls"], "/host/ws")
    assert "--network=none" not in cmd


# --------------------------------------------------------------------------- #
# LocalRunner — dev/test gate (the unsandboxed path must never run in prod)
# --------------------------------------------------------------------------- #
async def test_local_runner_refuses_production_environment():
    runner = LocalRunner(env="production")
    with pytest.raises(RunnerError):
        await runner.run(["echo", "hi"])


async def test_local_runner_refuses_staging_environment():
    runner = LocalRunner(env="staging")
    with pytest.raises(RunnerError):
        await runner.run(["echo", "hi"])


async def test_local_runner_allows_dev_environment(tmp_path):
    runner = LocalRunner(env="dev")
    res = await runner.run(
        ["python", "-c", "print('hi')"], cwd=str(tmp_path), timeout=10
    )
    assert isinstance(res, RunResult)
    assert res.exit_code == 0
    assert "hi" in res.stdout
    assert res.timed_out is False


async def test_local_runner_enforces_timeout(tmp_path):
    runner = LocalRunner(env="test")
    res = await runner.run(
        ["python", "-c", "import time; print('start'); time.sleep(5); print('end')"],
        cwd=str(tmp_path),
        timeout=1,
    )
    assert res.timed_out is True


async def test_local_runner_truncates_output_to_limit(tmp_path):
    runner = LocalRunner(env="test")
    res = await runner.run(
        ["python", "-c", "print('x' * 5000)"], cwd=str(tmp_path), timeout=10, output_limit=16
    )
    assert len(res.stdout) <= 16
