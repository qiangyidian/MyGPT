"""Crews: multi-role agent teams for complex tasks."""
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

__all__ = [
    "StageSpec",
    "build_research_stages",
    "build_research_crew",
    "build_parallel_research_stages",
    "review_crew_output",
    "ResearchEvidence",
    "AnalystFinding",
    "EvidenceItem",
    "ConflictNote",
]
