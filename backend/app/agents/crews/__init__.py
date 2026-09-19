"""Crews: multi-role agent teams for complex tasks."""
from app.agents.crews.debate import build_debate_stages
from app.agents.crews.parallel_research import build_parallel_research_stages
from app.agents.crews.research_crew import (
    AnalystFinding,
    ConflictNote,
    EvidenceItem,
    ResearchEvidence,
    build_research_crew,
    build_research_stages,
    review_crew_output,
)
from app.agents.crews.stage import StageSpec
from app.agents.crews.task_decomposition import build_task_decomposition_stages
from app.agents.crews.write_review import build_write_review_stages

__all__ = [
    "AnalystFinding",
    "ConflictNote",
    "EvidenceItem",
    "ResearchEvidence",
    "StageSpec",
    "build_debate_stages",
    "build_parallel_research_stages",
    "build_research_crew",
    "build_research_stages",
    "build_task_decomposition_stages",
    "build_write_review_stages",
    "review_crew_output",
]
