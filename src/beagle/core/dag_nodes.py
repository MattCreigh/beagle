"""DAG Node factories and utilities for workflow construction.

This module provides factory functions for standard node types and DAG utilities
like topological sorting and validation.
"""

from __future__ import annotations

import logging

from .orchestrator_types import DAGNode

logger = logging.getLogger("Beagle.dag_nodes")


# ── Standard Node Factories ──────────────────────────────────────────────────────


def create_planning_node(timeout: int = 300) -> DAGNode:
    """Create the planning phase node."""
    return DAGNode(
        name="planning",
        skill_name="research-planner",
        dependencies=[],
        timeout=timeout,
        retries=3,
    )


def create_execution_node(skill_name: str, dependencies: list[str], timeout: int = 600) -> DAGNode:
    """Create an execution phase node."""
    return DAGNode(
        name=f"exec_{skill_name}",  # Fixed missing f-string
        skill_name=skill_name,
        dependencies=dependencies,
        timeout=timeout,
        retries=2,
    )


def create_verification_node(dependencies: list[str], timeout: int = 300) -> DAGNode:
    """Create the verification phase node."""
    return DAGNode(
        name="verification",
        skill_name="fact-checker",
        dependencies=dependencies,
        timeout=timeout,
        retries=3,
    )


def create_synthesis_node(dependencies: list[str], timeout: int = 600) -> DAGNode:
    """Create the synthesis phase node."""
    return DAGNode(
        name="synthesis",
        skill_name="synthesis-writer",
        dependencies=dependencies,
        timeout=timeout,
        retries=2,
    )


# ── Node Graph Utilities ─────────────────────────────────────────────────────────


def topological_sort(nodes: list[DAGNode]) -> list[DAGNode]:
    """Sort nodes by dependency order.

    Args:
        nodes: List of DAGNode objects

    Returns:
        Sorted list where dependencies come before dependents

    Raises:
        ValueError: If circular dependency detected

    """
    # Build name -> node mapping
    node_map = {n.name: n for n in nodes}

    # Track visited nodes
    visited = set()
    sorted_nodes = []
    temp_marks = set()

    def visit(node: DAGNode):
        if node.name in visited:
            return
        if node.name in temp_marks:
            raise ValueError(
                f"Circular dependency detected at {node.name}"
            )  # Fixed missing f-string

        temp_marks.add(node.name)

        for dep_name in node.dependencies:
            if dep_name in node_map:
                visit(node_map[dep_name])

        temp_marks.remove(node.name)
        visited.add(node.name)
        sorted_nodes.append(node)

    for node in nodes:
        if node.name not in visited:
            visit(node)

    return sorted_nodes


def validate_dag(nodes: list[DAGNode]) -> list[str]:
    """Validate DAG for common issues.

    Returns:
        List of warning/error messages (empty if valid)

    """
    issues = []
    node_names = {n.name for n in nodes}

    # Check for missing dependencies
    for node in nodes:
        for dep in node.dependencies:
            if dep not in node_names:
                issues.append(
                    f"Node '{node.name}' depends on missing node '{dep}'"
                )  # Fixed missing f-string

    # Check for isolated nodes
    if len(nodes) > 1:
        connected = set()
        for node in nodes:
            connected.add(node.name)
            connected.update(node.dependencies)
        isolated = node_names - connected
        if isolated:
            issues.append(f"Isolated nodes (no connections): {isolated}")  # Fixed missing f-string

    # Check for cycles
    try:
        topological_sort(nodes)
    except ValueError as e:
        issues.append(str(e))

    return issues


__all__ = [
    "create_execution_node",
    "create_planning_node",
    "create_synthesis_node",
    "create_verification_node",
    "topological_sort",
    "validate_dag",
]
