"""Workflow generator for Beagle using LLM-based templating.

Enables natural language generation of YAML workflow definitions.
"""

from __future__ import annotations

import logging
import re

from ..core.nodes import execute_headless_goose
from ..utils.env_manager import get_workspace_root

logger = logging.getLogger("Beagle.templating")


class WorkflowGenerator:
    """Generates YAML workflows from natural language descriptions."""

    def __init__(self, model: str = "minimax-m3:cloud"):
        self.model = model

    async def generate(self, description: str, mode: str = "audit") -> str:
        """Generate a YAML workflow definition.

        Args:
            description: User's goal for the workflow.
            mode: Default mode for the workflow.

        Returns:
            YAML string of the generated workflow.

        """
        recipes = self._get_available_recipes()

        prompt = f"""Generate a YAML workflow for the Beagle system.
        (Beagle).

USER GOAL:
{description}

WORKFLOW MODE: {mode}

AVAILABLE AGENT RECIPES:
{", ".join(recipes)}

SCHEMA REQUIREMENTS:
- name: Unique workflow identifier
- description: Human-readable summary
- mode: {mode}
- phases: List of steps
  - name: Unique phase name
  - agent: One of the available recipes listed above
  - prompt_template: Instructions for the agent.
    Use {{query}}, {{research_plan}}, {{raw_execution_context}}, etc.
    for context injection.
  - depends_on: List of phase names that must complete before this one.

OUTPUT FORMAT:
Output ONLY the raw YAML block. No markdown, no explanation.

EXAMPLE:
name: my-workflow
description: Example description
mode: audit
phases:
  - name: planning
    agent: research-planner
    prompt_template: "Generate a plan for {{query}}"
  - name: execution
    agent: search-executor
    depends_on: [planning]
    prompt_template: "Execute this plan: {{research_plan}}"
"""

        logger.info(f"Generating workflow for: {description[:50]}...")

        # Use glm-5.1:cloud as specified in Phase 7.2
        result, _ = await execute_headless_goose(
            prompt=prompt,
            system_directive="You are an expert Beagle workflow designer. Output ONLY valid YAML.",
            node_name="workflow-generator",
            model=self.model,
        )

        return self._clean_yaml(result)

    def _get_available_recipes(self) -> list[str]:
        """List available agent recipes in the workspace."""
        recipes_dir = get_workspace_root() / "recipes"
        if not recipes_dir.exists():
            return [
                "research-planner",
                "search-executor",
                "fact-checker",
                "synthesis-writer",
            ]

        return [f.stem for f in recipes_dir.glob("*.xml")]

    def _clean_yaml(self, text: str) -> str:
        """Strip markdown fences if the LLM included them."""
        # Strip ```yaml ... ```
        text = re.sub(r"```(?:yaml|yml)?\s*(.*?)\s*```", r"\1", text, flags=re.DOTALL)
        return text.strip()
