"""Multi-agent execution graph: nodes (agents), edges (handoffs/dependencies),
and the live state snapshot persisted on :class:`~app.models.AgentRun`.

The graph is **static topology** (built before execution) plus a **live state**
layer (node/edge statuses updated as agents run). This separation is what lets
the frontend render a real DAG and restore it from the API after a refresh,
instead of guessing structure from event ordering.

Two profiles ship today:

  * ``deep_research``  — sequential: Researcher → Analyst → Writer.
  * ``parallel_research`` — Coordinator → (Web Researcher ‖ KB Researcher)
    → Analyst → Writer. The two researchers run concurrently via
    ``asyncio.gather``; Analyst is a *join* that only starts once both finish.

Edges carry a type so the UI can distinguish a dependency wait from an
evidence handoff. ``stage`` groups nodes into horizontal layers (same stage =
parallel candidates); ``lane`` disambiguates nodes within a stage.
"""
from __future__ import annotations

from enum import Enum
from typing import Any

from pydantic import BaseModel, Field


# --------------------------------------------------------------------------- #
# Enums (mirror the frontend AgentNodeStatus / AgentEdgeStatus exactly)
# --------------------------------------------------------------------------- #
class AgentNodeStatus(str, Enum):
    pending = "pending"
    queued = "queued"
    running = "running"
    waiting = "waiting"
    completed = "completed"
    failed = "failed"
    cancelled = "cancelled"


class AgentEdgeStatus(str, Enum):
    pending = "pending"
    active = "active"
    completed = "completed"
    failed = "failed"


class EdgeType(str, Enum):
    dependency = "dependency"
    handoff = "handoff"
    delegation = "delegation"


class GraphMode(str, Enum):
    sequential = "sequential"
    parallel = "parallel"
    hybrid = "hybrid"


# --------------------------------------------------------------------------- #
# Graph models
# --------------------------------------------------------------------------- #
class AgentGraphNode(BaseModel):
    """One agent in the graph. ``id`` is stable across the whole run."""

    id: str
    name: str
    role: str = ""
    description: str = ""
    task_title: str = ""
    task_summary: str = ""
    stage: int = 0
    lane: int = 0
    group_id: str | None = None
    status: AgentNodeStatus = AgentNodeStatus.pending
    started_at: str | None = None
    finished_at: str | None = None
    duration_ms: int | None = None
    current_tool: dict[str, Any] | None = None
    output_summary: str | None = None
    error: str | None = None


class AgentGraphEdge(BaseModel):
    """A directed link source → target. ``handoff`` = evidence/result passed."""

    id: str
    source: str
    target: str
    type: EdgeType = EdgeType.handoff
    status: AgentEdgeStatus = AgentEdgeStatus.pending
    label: str | None = None


class AgentGraph(BaseModel):
    """Full graph topology + live state. Persisted as ``graph_state`` on the run."""

    run_id: str = ""
    runtime: str = "crewai"
    flow_name: str = ""
    mode: GraphMode = GraphMode.sequential
    status: str = "pending"  # mirrors AgentRun.status
    nodes: list[AgentGraphNode] = Field(default_factory=list)
    edges: list[AgentGraphEdge] = Field(default_factory=list)
    active_agent_ids: list[str] = Field(default_factory=list)
    started_at: str | None = None
    finished_at: str | None = None

    # ---- lookups ----------------------------------------------------------
    def node(self, node_id: str) -> AgentGraphNode | None:
        return next((n for n in self.nodes if n.id == node_id), None)

    def edge(self, edge_id: str) -> AgentGraphEdge | None:
        return next((e for e in self.edges if e.id == edge_id), None)

    def predecessors(self, node_id: str) -> list[str]:
        """Node ids whose target is ``node_id`` (this node's dependencies)."""
        return [e.source for e in self.edges if e.target == node_id]

    def recompute_active(self) -> list[str]:
        """Active = all nodes currently ``running``. Recomputed, not stored."""
        self.active_agent_ids = [n.id for n in self.nodes if n.status == AgentNodeStatus.running]
        return self.active_agent_ids

    def to_public_dict(self) -> dict[str, Any]:
        """Compact dict for SSE ``agent_graph`` / API restore."""
        return self.model_dump(mode="json")


# --------------------------------------------------------------------------- #
# Profile builders — produce the static topology (all nodes pending)
# --------------------------------------------------------------------------- #
def build_deep_research_graph(question: str) -> AgentGraph:
    """Sequential Researcher → Analyst → Writer."""
    return AgentGraph(
        run_id="",
        runtime="crewai",
        flow_name="deep_research",
        mode=GraphMode.sequential,
        status="pending",
        nodes=[
            AgentGraphNode(
                id="researcher", name="Researcher", role="资料检索",
                task_title="检索和整理证据",
                task_summary="拆解问题，调用工具收集来源证据",
                stage=0, lane=0,
            ),
            AgentGraphNode(
                id="analyst", name="Analyst", role="证据分析",
                task_title="核对和分析证据",
                task_summary="交叉验证来源、标注冲突、判断证据是否充分",
                stage=1, lane=0,
            ),
            AgentGraphNode(
                id="writer", name="Writer", role="答案撰写",
                task_title="生成最终答案",
                task_summary="仅基于已验证证据撰写带引用的最终回答",
                stage=2, lane=0,
            ),
        ],
        edges=[
            AgentGraphEdge(id="researcher-analyst", source="researcher", target="analyst",
                           type=EdgeType.handoff, label="移交研究证据"),
            AgentGraphEdge(id="analyst-writer", source="analyst", target="writer",
                           type=EdgeType.handoff, label="移交分析结论"),
        ],
    )


def build_parallel_research_graph(question: str) -> AgentGraph:
    """Coordinator → (Web Researcher ‖ KB Researcher) → Analyst → Writer.

    The two researchers are at the same ``stage`` (1) on different lanes, which
    the UI renders side-by-side. Analyst (stage 2) is a join: its two inbound
    edges must both be ``completed`` before it can run.
    """
    return AgentGraph(
        run_id="",
        runtime="crewai",
        flow_name="parallel_research",
        mode=GraphMode.parallel,
        status="pending",
        nodes=[
            AgentGraphNode(
                id="coordinator", name="Coordinator", role="协调",
                task_title="拆解任务并分发",
                task_summary="将问题拆为网络检索与知识库检索两条线",
                stage=0, lane=0,
            ),
            AgentGraphNode(
                id="web-researcher", name="Web Researcher", role="网络检索",
                task_title="联网检索资料",
                task_summary="使用 web_search 收集外部证据",
                stage=1, lane=0,
            ),
            AgentGraphNode(
                id="kb-researcher", name="KB Researcher", role="知识库检索",
                task_title="检索内部知识库",
                task_summary="使用 RAG 检索内部文档",
                stage=1, lane=1,
            ),
            AgentGraphNode(
                id="analyst", name="Analyst", role="证据分析",
                task_title="汇合并分析证据",
                task_summary="合并两条检索线的证据并交叉验证",
                stage=2, lane=0,
            ),
            AgentGraphNode(
                id="writer", name="Writer", role="答案撰写",
                task_title="生成最终答案",
                task_summary="撰写带引用的最终回答",
                stage=3, lane=0,
            ),
        ],
        edges=[
            AgentGraphEdge(id="coord-web", source="coordinator", target="web-researcher",
                           type=EdgeType.dependency, label="分发网络检索"),
            AgentGraphEdge(id="coord-kb", source="coordinator", target="kb-researcher",
                           type=EdgeType.dependency, label="分发知识库检索"),
            AgentGraphEdge(id="web-analyst", source="web-researcher", target="analyst",
                           type=EdgeType.handoff, label="移交网络证据"),
            AgentGraphEdge(id="kb-analyst", source="kb-researcher", target="analyst",
                           type=EdgeType.handoff, label="移交知识库证据"),
            AgentGraphEdge(id="analyst-writer", source="analyst", target="writer",
                           type=EdgeType.handoff, label="移交分析结论"),
        ],
    )


def build_debate_graph(side_a: str, side_b: str) -> AgentGraph:
    """Advocate-A ‖ Advocate-B → Judge.

    Two advocates run in PARALLEL at the same stage (lanes 0/1); the Judge is a
    JOIN that only starts once both advocates' handoff edges are completed.
    Candidate names are dynamic (any A vs B); node ids are stable so the FE
    reducer and persistence stay deterministic.
    """
    sa = (side_a or "A").strip()
    sb = (side_b or "B").strip()
    return AgentGraph(
        run_id="",
        runtime="crewai",
        flow_name="debate",
        mode=GraphMode.parallel,
        status="pending",
        nodes=[
            AgentGraphNode(
                id="advocate-a", name=f"{sa} Advocate", role="支持方 A",
                task_title=f"为 {sa} 提供最强论证",
                task_summary=f"只从 {sa} 的最佳实践出发，给出结构化论证并主动承认其局限",
                stage=0, lane=0,
            ),
            AgentGraphNode(
                id="advocate-b", name=f"{sb} Advocate", role="支持方 B",
                task_title=f"为 {sb} 提供最强论证",
                task_summary=f"只从 {sb} 的最佳实践出发，给出结构化论证并主动承认其局限",
                stage=0, lane=1,
            ),
            AgentGraphNode(
                id="judge", name="Judge", role="中立裁判",
                task_title="按统一标准权衡双方",
                task_summary="同时读取双方论证，区分事实与推测，给出条件化结论",
                stage=1, lane=0,
            ),
        ],
        edges=[
            AgentGraphEdge(id="advocate-a-judge", source="advocate-a", target="judge",
                           type=EdgeType.handoff, label=f"{sa} 方论证"),
            AgentGraphEdge(id="advocate-b-judge", source="advocate-b", target="judge",
                           type=EdgeType.handoff, label=f"{sb} 方论证"),
        ],
    )


def build_single_agent_graph(question: str = "") -> AgentGraph:
    """Single-node graph for the native runtime.

    Scope C: even single-agent turns (auto/search/create/data_analysis/chat, and
    every crewai fallback) surface in the agent panel. One ``assistant`` node at
    stage 0; ``runtime="native"`` so the UI can label it distinctly from crews.
    """
    return AgentGraph(
        run_id="",
        runtime="native",
        flow_name="single_agent",
        mode=GraphMode.sequential,
        status="pending",
        nodes=[
            AgentGraphNode(
                id="assistant", name="助手", role="智能助手",
                task_title="理解问题并生成回答",
                task_summary="单 agent 模式：模型与工具循环",
                stage=0, lane=0,
            ),
        ],
        edges=[],
    )


def build_graph_for_profile(profile: str, question: str) -> AgentGraph:
    """Pick the topology by agent_profile / intent."""
    if profile == "parallel_research":
        return build_parallel_research_graph(question)
    if profile == "debate":
        from app.agents.planning import extract_debate_sides

        sides = extract_debate_sides(question)
        return build_debate_graph(
            sides.side_a if sides else "A", sides.side_b if sides else "B"
        )
    return build_deep_research_graph(question)
