"""DEMO-ONLY canned stage behaviours.

⚠️  Nothing in this module is real model output. Every string here is a
deterministic fixture used solely by :class:`DemoStageExecutor` so the full
multi-agent panel (real SSE, graph, tool attribution, lifecycle) can be
exercised live WITHOUT a model endpoint. The executor that consumes this is
itself gated behind TWO explicit opt-ins (``AGENT_DEMO_MODE`` env flag AND a
per-request ``demo=True``), so this content can never reach a normal
/api/chat/stream turn — it is isolated here precisely so the canned "writer"
answer is not co-located with the real :class:`CrewAIStageExecutor` /
:class:`FakeStageExecutor` and cannot be picked up by accident.

Keep this file grep-able as demo-only: do not import it from any production
runtime path.
"""
from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from app.agents.runtime.stage_executor import FakeStageExecutor


def build_demo_behaviours() -> dict[str, "FakeStageExecutor.Behavior"]:
    """Return the per-role demo behaviours (canned, non-real).

    Returns the Behaviour dataclass instances keyed by agent role so the
    DemoStageExecutor can simulate a research crew / debate without an LLM.
    """
    # Imported lazily to avoid a circular import (stage_executor is the owner of
    # the Behaviour dataclass and of DemoStageExecutor itself).
    from app.agents.runtime.stage_executor import FakeStageExecutor as _F

    Behavior = _F.Behavior
    return {
        "researcher": Behavior(
            delay=1.2,
            output=(
                "Evidence gathered:\n"
                "[source 1] CrewAI Flows support stateful, router-based orchestration.\n"
                "[source 2] Sequential crews run one agent at a time; gather enables parallel.\n"
                "Gap: none."
            ),
            summary="已收集 2 条证据，无缺口",
            tools=[{"name": "web_search", "args": {"query": "crewai flows"}, "ok": True, "result": "2 hits"}],
        ),
        "web-researcher": Behavior(
            delay=1.4,
            output="[source 1] Web evidence: CrewAI 1.15 supports aexecute_task.",
            summary="网络证据 1 条",
            tools=[{"name": "web_search", "args": {"query": "crewai aexecute_task"}, "ok": True, "result": "1 hit"}],
        ),
        "kb-researcher": Behavior(
            delay=1.1,
            output="[source 2] KB evidence: internal docs confirm dual-runtime design.",
            summary="知识库证据 1 条",
            tools=[{"name": "file_analyze", "args": {"document_id": "demo"}, "ok": True, "result": "doc text"}],
        ),
        "coordinator": Behavior(
            delay=0.4,
            output="web line: 'crewai parallel'; kb line: 'dual runtime'",
            summary="已拆分为网络与知识库两条检索线",
        ),
        "analyst": Behavior(
            delay=0.9,
            output=(
                "Finding: sufficient=true; conflicts=[]; "
                "verified_facts=[flows are stateful, gather enables parallel]; conclusion=ok"
            ),
            summary="证据充分，无冲突",
        ),
        "writer": Behavior(
            delay=0.8,
            output=(
                "CrewAI supports stateful Flows [source 1] and parallel agent "
                "execution via gather [source 2]. The dual-runtime design keeps "
                "native chat unaffected [source 2]."
            ),
            summary="已生成带引用的最终答案",
        ),
        # ---- debate profile (advocate-a ‖ advocate-b → judge) ----
        # Demo content uses Python vs Go; the REAL candidates are parsed from
        # the user's question at runtime by build_debate_stages, so any A-vs-B
        # pair works — this is just deterministic demo output.
        "advocate-a": Behavior(
            delay=1.0,
            output=(
                '{"side":"Python","key_arguments":["开发速度快、生态成熟","pygame 适合快速原型"],'
                '"limitations":["运行性能不如编译型语言","移动端部署弱"],'
                '"best_fit_scenarios":["快速原型","教学","脚本工具"],'
                '"risks":["GIL 限制 CPU 密集场景"],"summary":"Python 适合快速交付贪吃蛇原型。"}'
            ),
            summary="Python 方：快速开发与生态优势",
        ),
        "advocate-b": Behavior(
            delay=1.1,
            output=(
                '{"side":"Go","key_arguments":["编译型性能高","并发模型清晰","单二进制部署"],'
                '"limitations":["游戏生态较弱","开发速度慢于 Python"],'
                '"best_fit_scenarios":["高性能服务","跨平台部署"],'
                '"risks":["GUI/游戏库不如 Python 丰富"],"summary":"Go 适合高性能与部署，但游戏生态弱。"}'
            ),
            summary="Go 方：性能与部署优势",
        ),
        "judge": Behavior(
            delay=0.9,
            output=(
                "## 裁决\n\n"
                "| 维度 | Python | Go |\n|---|---|---|\n"
                "| 开发速度 | ✅ 快 | ⚠️ 较慢 |\n| 运行性能 | ⚠️ 一般 | ✅ 高 |\n"
                "| 游戏生态 | ✅ pygame | ⚠️ 较弱 |\n\n"
                "**结论**：若目标是快速学习/原型，选 Python；若追求高性能或后续部署为服务，选 Go。"
            ),
            summary="已权衡双方并给出条件化结论",
        ),
    }
