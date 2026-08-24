"""Wrap Beagle agent recipes as CrewAI Agent instances."""

from __future__ import annotations

import logging
from typing import Any

from .llm import BeagleCrewAILLM
from .tools import BeagleCrewAITool, wrap_mcp_tools_for_crewai

logger = logging.getLogger("Beagle.bridges.crewai.agent")


class BeagleCrewAIAgent:
    """CrewAI-compatible Agent backed by an Beagle recipe.

    Can be constructed from CrewAI parameters (role/goal/backstory)
    or loaded from an existing Beagle recipe file.
    """

    def __init__(
        self,
        role: str = "",
        goal: str = "",
        backstory: str = "",
        tools: list[BeagleCrewAITool] | None = None,
        llm: BeagleCrewAILLM | None = None,
        memory: bool = True,
        max_iter: int = 25,
        max_retry_limit: int = 2,
        recipe_name: str = "",
        **kwargs: Any,
    ) -> None:
        self.role = role
        self.goal = goal
        self.backstory = backstory
        self.tools = tools or wrap_mcp_tools_for_crewai()
        self.llm = llm or BeagleCrewAILLM()
        self.memory = memory
        self.max_iter = max_iter
        self.max_retry_limit = max_retry_limit
        self.recipe_name = recipe_name
        self._kwargs = kwargs

        # If recipe_name provided, load from Beagle recipes
        if recipe_name and not role:
            self._load_from_recipe(recipe_name)

    def _load_from_recipe(self, recipe_name: str) -> None:
        """Load agent parameters from an Beagle recipe file."""
        try:
            from beagle.config.agent_config import get_agent

            profile = get_agent(recipe_name)
            # AgentProfile is a dataclass, not a dict. Map its fields onto the
            # CrewAI agent's role/goal/backstory. The profile's name is the
            # role; its description doubles as the goal and backstory.
            self.role = profile.name or recipe_name
            self.goal = profile.description or ""
            self.backstory = profile.description or ""
            if profile.model:
                self.llm = BeagleCrewAILLM(model=profile.model)
        except Exception as e:  # ruff: ignore[BLE001]  # broad catch intentional
            logger.warning(f"Failed to load recipe {recipe_name}: {e}")

    def execute_task(self, task: Any) -> str:
        """Execute a task — called by CrewAI's Crew.kickoff()."""
        # Build prompt from task context
        prompt_parts = [
            f"You are a {self.role}.",
            f"Your goal: {self.goal}",
            f"Background: {self.backstory}",
            "",
            f"Task: {task.description}",
            f"Expected output: {task.expected_output}",
        ]
        if hasattr(task, "_context_results") and task._context_results:
            prompt_parts.append("\nContext from previous tasks:")
            for ctx in task._context_results:
                prompt_parts.append(f"  {ctx[:500]}")

        prompt = "\n".join(prompt_parts)
        messages = [
            {"role": "system", "content": self.backstory or f"You are a {self.role}."},
            {"role": "user", "content": prompt},
        ]
        return self.llm.call(messages)

    @classmethod
    def from_recipe(cls, recipe_name: str, **kwargs: Any) -> BeagleCrewAIAgent:
        """Factory: create agent from an Beagle recipe name."""
        return cls(recipe_name=recipe_name, **kwargs)
