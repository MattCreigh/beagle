"""CrewAI Runtime Bridge — run CrewAI crews inside Beagle.

Provides adapter classes that implement CrewAI's interfaces but
delegate execution to Beagle's hardened pipeline:
- LLM calls → Beagle model resolution + learned routing + cost tracking
- Tools → Beagle MCP tools wrapped as CrewAI BaseTool
- Memory → Beagle HierarchicalMemory
- Approval → Beagle Guardian gates
- Security → Beagle semantic firewall on all inputs/outputs

Usage:
    from beagle.bridges.crewai import (
        BeagleAgent, BeagleTool, BeagleTask, BeagleCrew
    )

    agent = BeagleAgent(role="researcher", goal="Find facts", backstory="Expert")
    task = BeagleTask(description="Research AI", agent=agent)
    crew = BeagleCrew(agents=[agent], tasks=[task])
    result = crew.kickoff(inputs={"topic": "AI safety"})
"""

from __future__ import annotations

from .agent import BeagleCrewAIAgent as BeagleAgent
from .crew import BeagleCrewAICrew as BeagleCrew
from .llm import BeagleCrewAILLM as BeagleLLM
from .task import BeagleCrewAITask as BeagleTask
from .tools import BeagleCrewAITool as BeagleTool

__all__ = ["BeagleAgent", "BeagleCrew", "BeagleLLM", "BeagleTask", "BeagleTool"]
