"""Steering types shared across the steering system."""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class SteeringDirective:
    """Guidance for the orchestrator to modify execution.

    Captures steering input from any source (file, TUI, API, env) and
    provides it to the orchestrator to influence workflow execution.

    Attributes:
        workflow_id: ID of the workflow this directive applies to
        has_guidance: True if this directive contains actual guidance
        priority_guidance: High-priority text guidance to inject into prompts
        skip_nodes: List of node names to skip during execution
        budget_override_usd: Override the budget cap for this run
        stop_after_node: Node name after which to stop the workflow
        source: Which input source provided this directive

    """

    workflow_id: str
    has_guidance: bool = False
    priority_guidance: str = ""
    skip_nodes: list[str] = field(default_factory=list)
    budget_override_usd: float | None = None
    stop_after_node: str | None = None
    source: str = "file"  # "file", "tui", "api", "env"
