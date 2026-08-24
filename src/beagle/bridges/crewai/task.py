"""Wrap workflow phases as CrewAI Task instances."""

from __future__ import annotations

from typing import Any


class BeagleCrewAITask:
    """CrewAI-compatible Task backed by Beagle workflow phase semantics."""

    def __init__(
        self,
        description: str = "",
        expected_output: str = "",
        agent: Any | None = None,
        tools: list | None = None,
        context: list | None = None,
        async_execution: bool = False,
        guardrail: Any | None = None,
        callback: Any | None = None,
        **kwargs: Any,
    ) -> None:
        self.description = description
        self.expected_output = expected_output or "Complete the task successfully."
        self.agent = agent
        self.tools = tools or []
        self.context = context or []  # List of prerequisite Tasks
        self.async_execution = async_execution
        self.guardrail = guardrail
        self.callback = callback
        self._kwargs = kwargs
        self._context_results: list[str] = []
        self.output: str = ""

    def set_context_results(self, results: list[str]) -> None:
        """Set results from prerequisite tasks as context."""
        self._context_results = results

    @classmethod
    def from_workflow_phase(
        cls,
        phase: dict[str, Any],
        agent: Any | None = None,
    ) -> BeagleCrewAITask:
        """Create a Task from an Beagle workflow YAML phase."""
        return cls(
            description=phase.get("prompt_template", ""),
            expected_output=phase.get("expected_output", ""),
            agent=agent,
        )
