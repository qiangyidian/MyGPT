"""Per-environment labeled context + starting/ready lifecycle (Codex pattern, reduced).

Codex runs against multiple isolated workspaces and labels every block of
context by environment id (cwd / workspace_roots / shell / status), so the model
never confuses which shell a command targets. Crucially it teaches the model:
*"an environment marked ``starting`` is not yet usable; wait only when the current
task needs it; continue unrelated work."* and resolves ready envs synchronously
while starting ones join lazily — slow container spin-up never stalls the turn.

Reduced core here: an :class:`Environment` + per-env labeled fragment + the
"continue unrelated work" instruction + an :class:`EnvironmentSet` with
``ready()`` / ``wait_until_ready()``.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

from app.agents.context_fragments import ContextFragment

EnvStatus = Literal["starting", "ready", "error"]

_CONTINUE_UNRELATED = (
    "多环境说明：每个环境块标注了自己的 cwd / 工作区 / 状态。标记为 ``starting`` 的"
    "环境尚未可用——仅当当前任务需要该环境时才等待它；对已在 ``ready`` 的环境继续推进"
    "不相关的工作，不要被某个启动中的环境阻塞整轮。"
)


@dataclass
class Environment:
    env_id: str
    cwd: str = ""
    status: EnvStatus = "ready"
    shell: str = ""
    workspace_roots: tuple[str, ...] = ()


def environment_fragment(env: Environment) -> ContextFragment:
    """One labeled <environment_context> block per environment id."""
    parts = [f"id: {env.env_id}", f"cwd: {env.cwd or '(unset)'}", f"status: {env.status}"]
    if env.shell:
        parts.append(f"shell: {env.shell}")
    if env.workspace_roots:
        parts.append("workspace_roots: " + ", ".join(env.workspace_roots))
    return ContextFragment(
        name=f"environment:{env.env_id}",
        tag=f"environment_context_{env.env_id}",
        body="\n".join(parts),
    )


def environments_instructions_fragment() -> ContextFragment:
    return ContextFragment(
        name="environments_instructions", tag="environments_instructions", body=_CONTINUE_UNRELATED
    )


@dataclass
class EnvironmentSet:
    """Holds environments; ``ready()`` returns those already usable; ``wait_until_ready``
    blocks (polls) until a specific env becomes ready (or times out)."""

    envs: dict[str, Environment] = field(default_factory=dict)

    def add(self, env: Environment) -> None:
        self.envs[env.env_id] = env

    def ready(self) -> list[Environment]:
        return [e for e in self.envs.values() if e.status == "ready"]

    def starting(self) -> list[Environment]:
        return [e for e in self.envs.values() if e.status == "starting"]

    def mark(self, env_id: str, status: EnvStatus) -> None:
        if env_id in self.envs:
            self.envs[env_id].status = status

    async def wait_until_ready(self, env_id: str, *, poll_s: float = 0.1, timeout_s: float = 30.0) -> Environment | None:
        import asyncio

        loop = 0.0
        while loop < timeout_s:
            e = self.envs.get(env_id)
            if e is None:
                return None
            if e.status == "ready":
                return e
            if e.status == "error":
                return None
            await asyncio.sleep(poll_s)
            loop += poll_s
        return None
