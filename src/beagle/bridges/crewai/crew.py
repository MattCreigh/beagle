"""Wrap Beagle DAGOrchestrator as a CrewAI Crew."""

from __future__ import annotations

import contextlib
import logging
import time
from dataclasses import dataclass, field
from typing import Any

from .agent import BeagleCrewAIAgent
from .task import BeagleCrewAITask

logger = logging.getLogger("Beagle.bridges.crewai.crew")


@dataclass
class CrewOutput:
    """CrewAI-compatible output from crew execution."""

    raw: str = ""
    tasks_output: list[dict[str, Any]] = field(default_factory=list)
    token_usage: dict[str, int] = field(default_factory=dict)


class BeagleCrewAICrew:
    """CrewAI-compatible Crew that executes through Beagle's pipeline.

    Supports sequential and hierarchical process types.
    All execution goes through Beagle's cost tracking, Guardian,
    and semantic firewall.
    """

    def __init__(
        self,
        agents: list[BeagleCrewAIAgent] | None = None,
        tasks: list[BeagleCrewAITask] | None = None,
        process: str = "sequential",
        verbose: bool = False,
        memory: bool = True,
        **kwargs: Any,
    ) -> None:
        self.agents = agents or []
        self.tasks = tasks or []
        self.process = process
        self.verbose = verbose
        self.memory = memory
        self._kwargs = kwargs

    def kickoff(self, inputs: dict[str, str] | None = None) -> CrewOutput:
        """Execute the crew — main CrewAI entry point.

        Args:
            inputs: Variable substitutions for task descriptions.

        Returns:
            CrewOutput with raw result and per-task outputs.

        """
        inputs = inputs or {}
        task_outputs: list[dict[str, Any]] = []
        context_results: list[str] = []
        total_output = ""

        # Cost tracking
        try:
            from beagle.cost_tracker import (
                ContextAwareCostTracker,
            )

            self._cost_tracker = ContextAwareCostTracker(budget_usd=10.0)
        except ImportError:
            self._cost_tracker = None  # type: ignore[assignment]

        start = time.monotonic()

        if self.process == "sequential":
            total_output = self._run_sequential(inputs, task_outputs, context_results)
        elif self.process == "hierarchical":
            total_output = self._run_hierarchical()

        duration = time.monotonic() - start
        logger.info(
            f"[CrewAI] Crew completed in {duration:.1f}s "
            f"({len(self.tasks)} tasks, process={self.process})"
        )

        return CrewOutput(
            raw=total_output,
            tasks_output=task_outputs,
        )

    def _run_sequential(
        self,
        inputs: dict[str, str],
        task_outputs: list[dict[str, Any]],
        context_results: list[str],
    ) -> str:
        """Execute tasks sequentially."""
        for task in self.tasks:
            # Substitute inputs into description
            desc = task.description
            for key, value in inputs.items():
                desc = desc.replace(f"{{{key}}}", value)
            task.description = desc

            # Pass context from previous tasks
            if task.context:
                ctx_results = [t.output for t in task.context if t.output]
                task.set_context_results(ctx_results)
            elif context_results:
                task.set_context_results(context_results[-3:])

            # Execute via assigned agent
            agent = task.agent or (self.agents[0] if self.agents else None)
            if agent is None:
                task.output = "No agent assigned to task"
            else:
                task.output = agent.execute_task(task)

            # Guardrail check
            if task.guardrail and callable(task.guardrail):
                with contextlib.suppress(Exception):
                    validated = task.guardrail(task.output)
                    if validated is False:
                        task.output = "[GUARDRAIL REJECTED]"

            # Callback
            if task.callback and callable(task.callback):
                with contextlib.suppress(Exception):
                    task.callback(task.output)

            context_results.append(task.output)
            task_outputs.append(
                {
                    "description": task.description[:200],
                    "output": task.output[:1000],
                    "agent": getattr(agent, "role", "unknown"),
                }
            )

            if self.verbose:
                logger.info(f"[CrewAI] Task completed: {task.description[:80]}")

        return context_results[-1] if context_results else ""

    def _run_hierarchical(self) -> str:
        """Execute tasks hierarchically with a manager agent."""
        manager = self.agents[0] if self.agents else None
        if not manager:
            return "No manager agent available"

        # Manager delegates tasks
        from .task import BeagleCrewAITask

        all_descs = "\n".join(f"- {t.description}" for t in self.tasks)
        manager_prompt = (
            "You are managing a team. Delegate and coordinate:\n"
            f"{all_descs}\n\nExecute each task and provide final summary."
        )
        task = BeagleCrewAITask(
            description=manager_prompt,
            expected_output="Complete summary of all delegated tasks",
            agent=manager,
        )
        return manager.execute_task(task)
