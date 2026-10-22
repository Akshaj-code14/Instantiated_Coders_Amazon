"""CampusPilot agent & LLM layer.

Public interface for the UI layer:

    from agent import CampusPilotAgent, AgentResponse

Nothing in this package imports Streamlit. State crosses the boundary as
plain dicts so two browser sessions can never share agent state.
"""

from __future__ import annotations

from typing import Any

__all__ = ["CampusPilotAgent", "AgentResponse"]


def __getattr__(name: str) -> Any:
    # Lazy import keeps `from agent.schemas import ...` cheap and avoids
    # import cycles while the package initialises.
    if name == "CampusPilotAgent":
        from agent.controller import CampusPilotAgent

        return CampusPilotAgent
    if name == "AgentResponse":
        from agent.schemas import AgentResponse

        return AgentResponse
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
