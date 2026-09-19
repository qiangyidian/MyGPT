"""沙箱 runner 工厂 —— 生产路径上唯一的 runner 构造点。

在接线之前，``DockerRunner``/``DockerRunnerConfig`` 没有任何生产构造点，
而 :mod:`app.tools.registry_init` 与 ``python_exec`` 各自 ``LocalRunner()``，
结果是「配了沙箱」这句话在代码里没有任何落点：生产的 workspace shell/git 工具
一律撞上 LocalRunner 的 dev/test 硬拒绝，用户看到的是静默废掉的功能。
本模块把「用哪个 runner、带什么限额、开哪些能力」收敛成一处，两边都从这里取。

模式（``SANDBOX_MODE``，取值定义在 :mod:`app.core.config`）：

  * ``local`` —— 子进程 runner，**只在 dev/test 真的能执行**：LocalRunner 自己
    按 ENV 硬拒绝生产环境，工厂不会绕过它（``SANDBOX_MODE=local`` 不代表
    「生产可用」，只代表「这台机器是开发机」）。
  * ``docker`` —— 生产 runner：容器隔离 + ``--memory/--cpus/--pids-limit`` +
    ``--network=none``，由 :func:`build_docker_config` 从配置和权限档案编译。

非法取值（含大小写/空白之外的任何拼错）在 :func:`sandbox_mode` 直接抛
:class:`SandboxConfigError`；``Settings._validate_sandbox_mode`` 还会在启动期
就把它挡下来，健康检查 ``/ready`` 的 ``runner`` 项负责暴露给运维。

部署约束（不要在本模块里悄悄绕过）：沙箱 docker 守护进程**不在应用节点上**。
``deploy/k8s/sandbox-runner.yaml`` 把特权 DinD 固定到独立的污点节点池
（nodeSelector ``mygpt/sandbox: "true"``），API/worker 通过 ``DOCKER_HOST`` 连它，
这样容器逃逸只影响一次性 runner pod，碰不到数据面。任何「复用应用节点 docker
socket」的改法都是在拆这堵墙。
"""
from __future__ import annotations

import shutil
import sys
from typing import TYPE_CHECKING, Any

from app.agents.exec_policy import ExecPolicy
from app.agents.permission_profiles import (
    CapabilityPolicy,
    PermissionProfileError,
    resolve_profile,
)
from app.agents.sandbox.base import Runner, RunnerError
from app.agents.sandbox.docker import DockerRunner, DockerRunnerConfig
from app.agents.sandbox.local import LocalRunner
from app.core.config import SANDBOX_MODE_DOCKER, SANDBOX_MODE_LOCAL, SANDBOX_MODES

if TYPE_CHECKING:  # pragma: no cover
    from app.core.config import Settings

# 默认可选档案：只放行两个安全内置档案。:danger-full-access（有网络 +
# 可写 rootfs）必须被运维显式列进 WORKSPACE_PROFILES_ALLOWED 才可选。
DEFAULT_ALLOWED_PROFILES: frozenset[str] = frozenset({":read-only", ":workspace-write"})
# 档案非法/未配时回落到的最严档案（不是 :workspace-write！）。
FALLBACK_PROFILE = ":read-only"


class SandboxConfigError(RunnerError):
    """沙箱配置非法（模式、档案、镜像缺失等）。执行期与启动期都拒绝。"""


def _settings(settings: Settings | None = None) -> Any:
    if settings is not None:
        return settings
    from app.core.config import get_settings

    return get_settings()


# --------------------------------------------------------------------------- #
# 模式 / 档案 / 限额
# --------------------------------------------------------------------------- #
def sandbox_mode(settings: Settings | None = None) -> str:
    """规范化后的 ``SANDBOX_MODE``；未知取值抛 :class:`SandboxConfigError`。"""
    s = _settings(settings)
    raw = str(getattr(s, "SANDBOX_MODE", SANDBOX_MODE_LOCAL) or "")
    mode = raw.strip().lower()
    if not mode:
        mode = SANDBOX_MODE_LOCAL
    if mode not in SANDBOX_MODES:
        raise SandboxConfigError(
            f"非法 SANDBOX_MODE={raw!r}：只支持 {', '.join(SANDBOX_MODES)}。"
            "生产请显式配 docker（local runner 会在非 dev/test 环境拒绝执行）"
        )
    return mode


def allowed_profiles(settings: Settings | None = None) -> frozenset[str]:
    """``WORKSPACE_PROFILES_ALLOWED`` 解析；为空时用 :data:`DEFAULT_ALLOWED_PROFILES`。"""
    s = _settings(settings)
    raw = str(getattr(s, "WORKSPACE_PROFILES_ALLOWED", "") or "")
    listed = {item.strip() for item in raw.split(",") if item.strip()}
    return frozenset(listed) if listed else DEFAULT_ALLOWED_PROFILES


def permission_policy(settings: Settings | None = None) -> CapabilityPolicy:
    """把 ``WORKSPACE_PERMISSION_PROFILE`` 编译成能力集（fail closed）。

    档案名未知、不在白名单里、或解析不出（本仓库只内置 ``:``-前缀档案，
    自定义 extends 尚未接线）都抛 :class:`SandboxConfigError` —— 权限档案解析
    不出来时悄悄给到 :data:`FALLBACK_PROFILE` 之外的宽松能力，等于没有档案。
    """
    s = _settings(settings)
    name = str(getattr(s, "WORKSPACE_PERMISSION_PROFILE", "") or "").strip()
    if not name:
        raise SandboxConfigError(
            "WORKSPACE_PERMISSION_PROFILE 为空：必须显式指定权限档案"
            f"（例如 {FALLBACK_PROFILE}）"
        )
    allow = allowed_profiles(s)
    if name not in allow:
        raise SandboxConfigError(
            f"权限档案 {name!r} 不在 WORKSPACE_PROFILES_ALLOWED 允许范围内"
            f"（允许：{', '.join(sorted(allow)) or '（空）'}）"
        )
    try:
        # decls={} —— 只解析内置档案；自定义 extends 需要一个配置来源，未接线。
        return resolve_profile(name, {})
    except PermissionProfileError as exc:
        raise SandboxConfigError(f"权限档案 {name!r} 无法解析: {exc}") from exc


def local_limits(settings: Settings | None = None) -> dict[str, Any]:
    """local 模式的资源上限（内存/CPU 时间/落盘大小）+ 是否强制。"""
    s = _settings(settings)
    return {
        "memory_mb": int(getattr(s, "SANDBOX_LOCAL_MEMORY_MB", 512)),
        "cpu_seconds": int(getattr(s, "SANDBOX_CPU_SECONDS", 30)),
        "max_fsize_mb": int(getattr(s, "SANDBOX_MAX_FSIZE_MB", 64)),
        "require_limits": bool(getattr(s, "SANDBOX_REQUIRE_LIMITS", True)),
    }


def build_docker_config(
    settings: Settings | None = None,
    *,
    policy: CapabilityPolicy | None = None,
) -> DockerRunnerConfig:
    """从配置 + 权限档案编译 :class:`DockerRunnerConfig`（纯函数，可断言）。

    档案 → 开关的映射：``network`` → ``--network=none`` 是否加；
    ``danger_full_access`` → rootfs 是否只读；``fs_write`` → 工作区挂载 rw/ro。
    ``--cap-drop=ALL`` / ``--security-opt=no-new-privileges`` / ``--user nobody``
    是恒定的，不从配置读 —— 没有任何一档配置应当允许沙箱容器持 capability。
    """
    s = _settings(settings)
    pol = policy if policy is not None else permission_policy(s)
    image = str(getattr(s, "SANDBOX_DOCKER_IMAGE", "") or "").strip()
    if not image:
        raise SandboxConfigError(
            "SANDBOX_MODE=docker 但 SANDBOX_DOCKER_IMAGE 为空 —— 无法确定执行镜像"
        )
    return DockerRunnerConfig(
        image=image,
        cpu_quota=float(getattr(s, "SANDBOX_CPU_QUOTA", 1.0)),
        memory_mb=int(getattr(s, "SANDBOX_MEMORY_MB", 512)),
        pids_limit=int(getattr(s, "SANDBOX_PIDS_LIMIT", 64)),
        timeout_s=int(getattr(s, "SANDBOX_TIMEOUT_SECONDS", 30)),
        output_limit=int(getattr(s, "SANDBOX_OUTPUT_LIMIT", 8192)),
        # 能力越界才会打开这些开关，而能力越界需要档案在白名单里。
        read_only=not pol.danger_full_access,
        network_none=not pol.network,
        cap_drop_all=True,
        no_new_privileges=True,
        user="nobody",
        workspace_mount_ro=not pol.fs_write,
    )


# --------------------------------------------------------------------------- #
# 工厂
# --------------------------------------------------------------------------- #
def get_sandbox_runner(
    settings: Settings | None = None,
    *,
    policy: CapabilityPolicy | None = None,
) -> Runner:
    """按 ``SANDBOX_MODE`` 返回唯一那个 runner 实例。

    构造很便宜（不碰磁盘、不起进程），所以调用方每次都取一次即可 —— 缓存反而会
    让测试里的 settings 打补丁失效。模式非法 → 抛；docker 模式镜像未配 → 抛；
    local 模式在生产 → 构造成功但 :meth:`LocalRunner.run` 自己会拒绝执行。
    """
    s = _settings(settings)
    mode = sandbox_mode(s)
    if mode == SANDBOX_MODE_DOCKER:
        return DockerRunner(build_docker_config(s, policy=policy))
    return LocalRunner(settings=s, **local_limits(s))


def python_interpreter_argv(settings: Settings | None = None) -> list[str]:
    """执行一段 Python 时该用的解释器 argv 前缀。

    local 模式用当前解释器的绝对路径（白名单 env 里没有 venv 激活信息，
    靠 PATH 找 ``python`` 会不稳定）；docker 模式用镜像里的 ``python``。
    """
    if sandbox_mode(settings) == SANDBOX_MODE_DOCKER:
        return ["python"]
    return [sys.executable]


def runner_descriptor(settings: Settings | None = None) -> dict[str, Any]:
    """给健康检查 / 日志用的沙箱现状（不做任何执行，也不抛业务异常）。

    ``ok=False`` 的 ``reason`` 直接给运维看：模式非法、docker 不可用、
    以及「local + 生产 + 却声称要执行代码」这种自相矛盾的配法。
    """
    s = _settings(settings)
    try:
        mode = sandbox_mode(s)
    except SandboxConfigError as exc:
        return {"ok": False, "mode": str(getattr(s, "SANDBOX_MODE", "")), "reason": str(exc)}
    try:
        policy = permission_policy(s)
    except SandboxConfigError as exc:
        return {"ok": False, "mode": mode, "reason": str(exc)}

    info: dict[str, Any] = {
        "mode": mode,
        "profile": str(getattr(s, "WORKSPACE_PERMISSION_PROFILE", "") or ""),
        "caps": {
            "fs_read": policy.fs_read,
            "fs_write": policy.fs_write,
            "network": policy.network,
            "shell": policy.shell,
        },
    }
    if mode == SANDBOX_MODE_DOCKER:
        try:
            cfg = build_docker_config(s, policy=policy)
        except SandboxConfigError as exc:
            return {"ok": False, "mode": mode, "reason": str(exc)}
        if not shutil.which("docker"):
            return {
                **info,
                "ok": False,
                "reason": "SANDBOX_MODE=docker 但宿主机 PATH 上找不到 docker",
            }
        return {**info, "ok": True, "reason": f"docker runner ready (image={cfg.image})"}

    # local：dev/test 里可用；生产里 LocalRunner 会拒绝执行。若同时还声称要开
    # 代码执行工具，那是一次配错的部署，必须在 /ready 上响。
    env_now = str(getattr(s, "ENV", "") or "")
    claims_code_exec = bool(
        getattr(s, "WORKSPACE_TOOLS_ENABLED", False)
        or getattr(s, "ALLOW_PYTHON_EXEC", False)
    )
    if env_now not in ("dev", "test") and claims_code_exec:
        return {
            **info,
            "ok": False,
            "reason": (
                f"SANDBOX_MODE=local 在 ENV={env_now!r} 下无法执行任何命令，"
                "但 WORKSPACE_TOOLS_ENABLED/ALLOW_PYTHON_EXEC 已打开 —— 生产请配 "
                "SANDBOX_MODE=docker（见 deploy/k8s/sandbox-runner.yaml）"
            ),
        }
    return {**info, "ok": True, "reason": f"local runner (ENV={env_now}, dev/test only)"}


def exec_policy(settings: Settings | None = None) -> ExecPolicy:
    """当前生效的命令前缀策略（allow/prompt/forbidden），见
    :func:`app.agents.exec_policy.load_active_exec_policy`。
    """
    from app.agents.exec_policy import load_active_exec_policy

    return load_active_exec_policy(_settings(settings))


__all__ = [
    "DEFAULT_ALLOWED_PROFILES",
    "FALLBACK_PROFILE",
    "SandboxConfigError",
    "allowed_profiles",
    "build_docker_config",
    "exec_policy",
    "get_sandbox_runner",
    "local_limits",
    "permission_policy",
    "python_interpreter_argv",
    "runner_descriptor",
    "sandbox_mode",
]
