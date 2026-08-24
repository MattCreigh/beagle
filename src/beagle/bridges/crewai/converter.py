"""Convert between CrewAI definitions and Beagle workflow YAML."""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger("Beagle.bridges.crewai.converter")


def crewai_to_beagle_workflow(
    agents: list[dict[str, Any]],
    tasks: list[dict[str, Any]],
    process: str = "sequential",
) -> dict[str, Any]:
    """Convert CrewAI agent/task definitions to Beagle workflow YAML dict.

    Args:
        agents: List of agent dicts with role, goal, backstory, model.
        tasks: List of task dicts with description, expected_output, agent_role.
        process: "sequential" or "hierarchical".

    Returns:
        Beagle workflow specification dict (can be yaml.dump'd).

    """
    # Map agent roles to recipe-style names
    agent_map: dict[str, str] = {}
    for agent in agents:
        role = agent.get("role", "assistant")
        name = role.lower().replace(" ", "-")
        agent_map[role] = name

    phases = []
    for i, task in enumerate(tasks):
        agent_role = task.get("agent_role", "")
        agent_name = agent_map.get(agent_role, "researcher")

        phase = {
            "name": f"phase_{i + 1}",
            "agent": agent_name,
            "prompt_template": task.get("description", ""),
            "output_key": f"phase_{i + 1}_output",
        }
        if i > 0 and process == "sequential":
            phase["depends_on"] = [f"phase_{i}"]

        phases.append(phase)

    return {
        "name": "crewai_imported_workflow",
        "description": "Workflow imported from CrewAI definition",
        "phases": phases,
    }


def beagle_workflow_to_crewai(
    workflow: dict[str, Any],
) -> tuple[list[dict], list[dict]]:
    """Convert Beagle workflow YAML to CrewAI agent/task definitions.

    Returns:
        Tuple of (agents_list, tasks_list) in CrewAI dict format.

    """
    agents: dict[str, dict] = {}
    tasks = []

    for phase in workflow.get("phases", []):
        agent_name = phase.get("agent", "researcher")
        if agent_name not in agents:
            agents[agent_name] = {
                "role": agent_name.replace("-", " ").title(),
                "goal": f"Execute {agent_name} tasks",
                "backstory": f"Expert {agent_name}",
            }

        tasks.append(
            {
                "description": phase.get("prompt_template", ""),
                "expected_output": phase.get("expected_output", "Completed output"),
                "agent_role": agents[agent_name]["role"],
            }
        )

    return list(agents.values()), tasks
