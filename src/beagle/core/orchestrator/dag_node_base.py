"""Shared base class for BeagleDAGNode — v13.12.9 deduplication.

Closes HIGH finding B: BeagleDAGNode was duplicated across executor.py and
agent_spawner.py with ~60% overlap. The ``from_node()`` factory is truly
identical in both locations and is now defined once here.

Each module's BeagleDAGNode inherits from this base and provides its own
``execute()`` with module-specific Goose subprocess logic.
"""

from __future__ import annotations

from beagle.core.orchestrator_types import DAGNode


class BeagleDAGNodeBase(DAGNode):
    """Shared base for executor.py and agent_spawner.py BeagleDAGNode variants."""

    @classmethod
    def from_node(cls, node: DAGNode) -> BeagleDAGNodeBase:
        """Create an BeagleDAGNode from a generic DAGNode.

        Identical in both executor.py and agent_spawner.py — centralized here.
        """
        return cls(
            name=node.name,
            skill_name=node.skill_name,
            state_mutator=node.state_mutator,
            prompt_builder=node.prompt_builder,
            dependencies=node.dependencies,
            timeout=node.timeout,
            retries=getattr(node, "retries", 3),
            model_override=getattr(node, "model_override", None),
            output_key=getattr(node, "output_key", None),
        )
