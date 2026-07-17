"""Agent platform: dual-runtime (native + CrewAI) orchestration on top of the
existing chat/RAG/tool stack. See ``docs`` and the project plan for the full
architecture. The public entry points are :class:`ChatOrchestrator` and the
:class:`AgentRuntime` protocol.
"""
from __future__ import annotations
