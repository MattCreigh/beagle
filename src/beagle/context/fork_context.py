"""Forked context for subagent cache reuse.

Inherits static prompt parts and memory pointers from a parent orchestrator.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from ..core.orchestrator_types import AgentState
from .prompt_cache import PromptCache

logger = logging.getLogger("Beagle.context.fork")


@dataclass
class ForkContext:
    """Read-only snapshot of parent state with mutable scratchpad."""

    fork_id: str
    parent_static_cache: dict[str, Any]
    parent_memory_pointers: str | None
    parent_completed_nodes: list[str]
    parent_observations: str

    # Mutable scratchpad for this fork
    scratchpad: list[str] = field(default_factory=list)

    @classmethod
    def from_parent(
        cls,
        parent_cache: PromptCache,
        parent_state: AgentState,
        parent_memory: str | None,
        fork_id: str,
    ) -> ForkContext:
        """Create a fork from parent state."""
        return cls(
            fork_id=fork_id,
            parent_static_cache=parent_cache.snapshot(),
            parent_memory_pointers=parent_memory,
            parent_completed_nodes=list(parent_state.completed_nodes),
            parent_observations=parent_state.raw_execution_context[:2000],
        )

    def build_prompt(self, intent: str, steering: str = "") -> str:
        """Build a prompt using parent's cached static content."""
        # For simplicity in this phase, we'll re-use PromptCache logic
        # but with the parent's static parts.
        # In a full implementation, this would avoid re-tokenization.

        # Construct context about parent's work
        parent_context = f"""
PARENT PROGRESS:
Nodes completed: {", ".join(self.parent_completed_nodes)}
Last observations: {self.parent_observations}
"""

        # Build prompt using a temporary cache initialized with parent's static parts
        temp_cache = PromptCache()
        temp_cache._static_cache = self.parent_static_cache

        # We use a special node name for the fork to reuse the cached part
        # (The orchestrator should ensure the recipe/directive matches)
        node_name = self.parent_completed_nodes[-1] if self.parent_completed_nodes else "root"

        prompt, _ = temp_cache.build_prompt(
            node_name=node_name,
            intent=f"{intent}\n\n{parent_context}",
            steering=steering,
            memory_pointers=self.parent_memory_pointers,
        )

        return prompt

    def add_observation(self, observation: str) -> None:
        """Add work result to scratchpad."""
        self.scratchpad.append(observation)

    def get_scratchpad(self) -> list[str]:
        """Retrieve all observations."""
        return list(self.scratchpad)
