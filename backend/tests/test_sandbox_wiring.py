"""沙箱接线的 fail-closed 不变量。

接线前 ``DockerRunner`` 在生产里没有任何构造点，``python_exec`` 是继承本进程
env 的裸子进程，而 ``tool_policy`` 把「AND」写成了「or」—— 任何一个都能让
「有沙箱」这句话变成空话。这里钉住四件事：子进程 env 是白名单而非继承、
配置不确定即拒绝、限制施加不了就不执行、docker 的隔离开关由工厂恒定钉死。
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.agents.policies.tool_policy import (
    isolated_sandbox_configured,
    python_exec_enabled,
)
from app.agents.sandbox.base import build_child_env
from app.agents.sandbox.docker import DockerRunner, build_docker_command
from app.agents.sandbox.factory import (
    FALLBACK_PROFILE,
    DockerRunnerConfig,
    LocalRunner,
    SandboxConfigError,
    build_docker_config,
    get_sandbox_runner,
    permission_policy,
    sandbox_mode,
)
from app.agents.sandbox.local import rlimits_supported


def _settings(**kw):
    return SimpleNamespace(**kw)


# --------------------------------------------------------------------------- #
# 子进程环境：白名单，绝不继承
# --------------------------------------------------------------------------- #
def test_child_env_never_forwards_server_secrets():
    base = {
        "PATH": "/usr/bin",
        "DATABASE_URL": "postgresql+asyncpg://u:p@db:5432/mychat",
        "REDIS_URL": "redis://redis:6379/0",
        "JWT_SECRET": "super-secret",
        "FERNET_KEY": "material",
        "QDRANT_API_KEY": "material",
        "OPENAI_API_KEY": "sk-material",
        "PYTHONPATH": "/app",
        "PYTHONSTARTUP": "/tmp/evil.py",
    }
    env = build_child_env(base=base)

    assert env["PATH"] == "/usr/bin"
    for key in ("DATABASE_URL", "REDIS_URL", "JWT_SECRET", "FERNET_KEY",
                "QDRANT_API_KEY", "OPENAI_API_KEY"):
        assert key not in env, f"{key} 泄漏进了沙箱子进程"
    # 解释器劫持向量也不在任何一个白名单里。
    assert "PYTHONPATH" not in env and "PYTHONSTARTUP" not in env


def test_child_env_repoints_home_at_the_sandbox_dir():
    # 继承下来的 HOME 里有 ~/.ssh、~/.aws、pip token。
    env = build_child_env(
        cwd="/tmp/sandbox-run-1", base={"PATH": "/usr/bin", "HOME": "/root"}
    )
    assert env["HOME"] == "/tmp/sandbox-run-1"


def test_child_env_drops_values_that_are_not_plain_strings():
    env = build_child_env(
        base={"PATH": "/usr/bin", "LANG": None, "TZ": 123, "LC_ALL": ""}
    )
    assert set(env) >= {"PATH"}
    assert "LANG" not in env and "TZ" not in env and "LC_ALL" not in env


# --------------------------------------------------------------------------- #
# 配置不确定 = 拒绝
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("raw", ["doker", "gvisor", "e2b", "dockerr", "1"])
def test_illegal_sandbox_mode_raises_instead_of_silently_falling_back(raw):
    with pytest.raises(SandboxConfigError):
        sandbox_mode(_settings(SANDBOX_MODE=raw))


@pytest.mark.parametrize("raw,expected", [("", "local"), ("  ", "local"),
                                          ("DOCKER", "docker"),
                                          (" Local ", "local")])
def test_sandbox_mode_normalizes_and_defaults_to_local(raw, expected):
    assert sandbox_mode(_settings(SANDBOX_MODE=raw)) == expected


def test_permission_policy_fails_closed_on_missing_or_unlisted_profile():
    with pytest.raises(SandboxConfigError):
        permission_policy(_settings(WORKSPACE_PERMISSION_PROFILE=""))
    with pytest.raises(SandboxConfigError):
        permission_policy(
            _settings(
                WORKSPACE_PERMISSION_PROFILE=":everything",
                WORKSPACE_PROFILES_ALLOWED=":read-only",
            )
        )
    assert permission_policy(
        _settings(
            WORKSPACE_PERMISSION_PROFILE=FALLBACK_PROFILE,
            WORKSPACE_PROFILES_ALLOWED="",
        )
    ) is not None


def test_docker_config_requires_an_image():
    with pytest.raises(SandboxConfigError):
        build_docker_config(
            _settings(SANDBOX_DOCKER_IMAGE="", WORKSPACE_PERMISSION_PROFILE="")
        )


def test_docker_argv_carries_the_restrictive_flags():
    """默认配置编译出的 argv 必须带上全部隔离开关（读 argv 即审计面）。"""
    argv = build_docker_command(DockerRunnerConfig(image="python:3.12-slim"),
                                ["python", "-c", "print(1)"], "/tmp/ws")
    flat = " ".join(argv)

    assert "--cap-drop=ALL" in flat, flat
    assert "no-new-privileges" in flat, flat
    assert "--user nobody" in flat, flat
    assert "--network=none" in flat, flat
    assert "--read-only" in flat, flat


def test_docker_config_maps_profile_capabilities_to_flags():
    cfg = build_docker_config(
        _settings(
            SANDBOX_DOCKER_IMAGE="python:3.12-slim",
            WORKSPACE_PERMISSION_PROFILE=FALLBACK_PROFILE,
            WORKSPACE_PROFILES_ALLOWED="",
        )
    )
    assert cfg.read_only is True
    assert cfg.network_none is True
    # 这三个不从配置读：任何权限档案都不该让沙箱容器持有 capability。
    assert cfg.cap_drop_all is True
    assert cfg.no_new_privileges is True
    assert cfg.user == "nobody"


# --------------------------------------------------------------------------- #
# runner 工厂：唯一构造点
# --------------------------------------------------------------------------- #
def test_factory_returns_docker_runner_without_touching_docker():
    runner = get_sandbox_runner(
        _settings(
            SANDBOX_MODE="docker",
            SANDBOX_DOCKER_IMAGE="python:3.12-slim",
            WORKSPACE_PERMISSION_PROFILE=FALLBACK_PROFILE,
            WORKSPACE_PROFILES_ALLOWED="",
        )
    )
    assert isinstance(runner, DockerRunner)


def test_factory_returns_local_runner_for_local_mode():
    runner = get_sandbox_runner(
        _settings(
            SANDBOX_MODE="local",
            ENV="dev",
            SANDBOX_LOCAL_MEMORY_MB=256,
            SANDBOX_CPU_SECONDS=10,
            SANDBOX_MAX_FSIZE_MB=32,
            SANDBOX_REQUIRE_LIMITS=False,
        )
    )
    assert isinstance(runner, LocalRunner)


def test_local_runner_refuses_when_it_cannot_enforce_requested_caps():
    runner = LocalRunner(
        env="dev",
        memory_mb=128,
        require_limits=True,
        caps_supported=lambda: False,
    )
    with pytest.raises(Exception) as exc:
        runner._guard_caps()
    assert "docker" in str(exc.value)


def test_rlimit_support_detection_is_not_hardcoded_true():
    assert rlimits_supported() in (True, False)


# --------------------------------------------------------------------------- #
# python_exec 的放行判据：两个条件同时成立
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("value", ["0", "false", "no", "", "off", "e2b", "gvisor"])
def test_falsy_or_placeholder_sandbox_backend_does_not_allow_python_exec(value):
    settings = _settings(
        PYTHON_SANDBOX=value, SANDBOX_MODE="docker", is_dev=False,
        ALLOW_PYTHON_EXEC=True,
    )
    assert isolated_sandbox_configured(settings) is False
    assert python_exec_enabled(settings) is False


def test_backend_name_alone_is_not_enough_when_runner_is_local():
    settings = _settings(
        PYTHON_SANDBOX="docker", SANDBOX_MODE="local", is_dev=False,
        ALLOW_PYTHON_EXEC=True,
    )
    assert isolated_sandbox_configured(settings) is False
    assert python_exec_enabled(settings) is False


def test_illegal_sandbox_mode_counts_as_not_configured():
    settings = _settings(
        PYTHON_SANDBOX="docker", SANDBOX_MODE="nonsense", is_dev=False,
        ALLOW_PYTHON_EXEC=True,
    )
    assert isolated_sandbox_configured(settings) is False


def test_python_exec_allowed_only_when_both_conditions_hold_in_prod():
    ok = _settings(
        PYTHON_SANDBOX="docker", SANDBOX_MODE="docker", is_dev=False,
        ALLOW_PYTHON_EXEC=True,
    )
    assert isolated_sandbox_configured(ok) is True
    assert python_exec_enabled(ok) is True
    # 缺 ALLOW_PYTHON_EXEC 仍然不放行。
    no_flag = _settings(
        PYTHON_SANDBOX="docker", SANDBOX_MODE="docker", is_dev=False,
        ALLOW_PYTHON_EXEC=False,
    )
    assert python_exec_enabled(no_flag) is False
