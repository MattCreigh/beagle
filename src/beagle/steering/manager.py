"""Steering manager for Beagle mid-workflow guidance.

Checks for steering directives between workflow nodes and applies them
to modify execution: skip nodes, adjust budget, inject priority guidance,
or stop after a specific node.
"""

from __future__ import annotations

import logging
from pathlib import Path

from .sources import SteeringSourceManager
from .types import SteeringDirective

logger = logging.getLogger("Beagle.steering")


class SteeringManager:
    """Manages steering guidance for workflow execution.

    Coordinates between multiple input sources (file, TUI, API, env) and
    applies steering directives at node transitions.

    Usage:
        steering_manager = SteeringManager(workspace_root, workflow_id)

        # Between nodes:
        directive = steering_manager.check()
        if directive.has_guidance:
            # Apply skip_nodes, budget_override, etc.
            pass
    """

    def __init__(self, workspace_root: Path, workflow_id: str = "default"):
        self.workspace_root = workspace_root
        self.workflow_id = workflow_id
        self._source_manager = SteeringSourceManager(workspace_root, workflow_id)
        self._applied_count: int = 0

    def check(self) -> SteeringDirective:
        """Check for steering guidance from any source.

        Returns:
            SteeringDirective with guidance if found, empty directive otherwise.
            The directive.source field indicates which source provided the guidance.

        """
        directive = self._source_manager.check()

        if directive and directive.has_guidance:
            self._source_manager.acknowledge(directive)
            self._applied_count += 1
            self._log_directive(directive)

        # Always return a directive (even if empty) for convenience
        return directive or SteeringDirective(workflow_id=self.workflow_id)

    def _log_directive(self, directive: SteeringDirective) -> None:
        """Log applied steering directive."""
        parts = [f"Steering applied (#{self._applied_count}):"]

        if directive.priority_guidance:
            # Truncate for logging
            priority = directive.priority_guidance[:100]
            if len(directive.priority_guidance) > 100:
                priority += "..."
            parts.append(f"  Priority: {priority}")

        if directive.skip_nodes:
            parts.append(f"  Skip: {', '.join(directive.skip_nodes)}")

        if directive.budget_override_usd is not None:
            parts.append(f"  Budget: ${directive.budget_override_usd:.2f}")

        if directive.stop_after_node:
            parts.append(f"  Stop after: {directive.stop_after_node}")

        logger.info("\n".join(parts))

    @property
    def applied_count(self) -> int:
        """Number of steering directives applied in this workflow."""
        return self._applied_count

    def reset(self) -> None:
        """Reset applied counter for new workflow."""
        self._applied_count = 0


def get_workspace_root() -> Path:
    """Get the workspace root path for steering files.

    Checks in order:
    1. BEAGLE_WORKSPACE environment variable
    2. Current working directory
    3. ~/.beagle/steer.md as fallback
    """
    import os

    if env_path := os.environ.get("BEAGLE_WORKSPACE"):
        return Path(env_path)

    cwd = Path.cwd()

    # Check if we're in a known project structure
    if cwd.name == "beagle":
        return cwd.parent
    elif cwd.name == "Dev":
        return cwd

    return cwd
