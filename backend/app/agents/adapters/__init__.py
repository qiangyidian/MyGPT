"""CrewAI adapters: LLM, tool, and event mapping.

These bridge CrewAI's primitives onto the app's existing ModelConfig /
ToolGateway / AgentEvent contracts, so a CrewAI run uses the *same* model
config, the *same* hardened tool path, and the *same* SSE vocabulary as the
native runtime. ``crewai`` is an optional dependency: every module here imports
it lazily so the app boots fine without it.
"""
from __future__ import annotations
