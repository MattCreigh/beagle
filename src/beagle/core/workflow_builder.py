"""Node and graph building for workflows.

This module contains logic to build DAG nodes and StateGraphs
from workflow specifications. Extracted from workflow_loader.py.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Callable
from pathlib import Path
from string import Template
from typing import Any

# Import DAG types - package-relative (no sys.path hack needed).
from .autonomous_orchestrator import AgentState, DAGNode, DAGOrchestrator
from .workflow_loader import _parse_condition as parse_condition
from .workflow_schema import (
    FIELD_MAPPING,
)

# Optional LangGraph imports
try:
    from langgraph.graph import StateGraph

    from beagle.core.graph import build_workflow_graph

    _HAS_LANGGRAPH = True
except ImportError:
    _HAS_LANGGRAPH = False
    StateGraph = None  # type: ignore[assignment,misc]

logger = logging.getLogger("Beagle.workflow_builder")


def create_mutator(key: str) -> Callable[[AgentState, str], None]:
    """Create a state mutator function.

    The mutator stores results in the appropriate AgentState field
    based on the key name.

    Args:
        key: The key/field to store the result under

    Returns:
        Mutator function

    """
    target_field = FIELD_MAPPING.get(key)

    def mutator(state: AgentState, result: str) -> None:
        # Store in metadata for all keys
        state.metadata[key] = result

        # Also store in specific field if mapped
        if target_field:
            setattr(state, target_field, result)

    return mutator


# ── Prompt templating ──────────────────────────────────────────────────────


class _BeagleTemplate(Template):
    """string.Template subclass that permits dotted state.<field> lookups
    via ${state.field}. Falls back to simple ${field}."""

    # Override braced identifier pattern to accept dotted names.
    braceidpattern = r"(?a:[a-zA-Z_][a-zA-Z0-9_]*(?:\.[a-zA-Z_][a-zA-Z0-9_]*)?)"


def create_prompt_builder(template: str) -> Callable[[AgentState], str]:
    """Create a prompt builder that performs ${var} substitution.

    Uses string.Template.safe_substitute so unresolved variables are
    LEFT VISIBLE in the output (not silently stripped). Agents can then
    flag the missing context back to the orchestrator.

    Use $$ to escape a literal $.

    For backward compatibility, this also supports legacy {var} syntax
    by converting it internally to ${var} before Template processing.
    """
    # Backward-compat: convert {var} → ${var}, but leave {{literal}} as {literal}.
    # ORDERING MATTERS: strip {{literal}} FIRST, then convert {var} to ${var}.
    # If we convert {var} first, the regex would greedily match the inner braces
    # of {{literal}} and produce corrupted output.
    de_escaped = re.sub(
        r"\{\{([a-zA-Z_][a-zA-Z0-9_.]*)\}\}",
        r"\1",
        template,
    )
    migrated = re.sub(
        r"\{([a-zA-Z_][a-zA-Z0-9_.]*)\}",
        r"${\1}",
        de_escaped,
    )
    compiled = _BeagleTemplate(migrated)

    def builder(state: AgentState) -> str:
        subs: dict[str, str] = {
            "query": state.query,
            "research_plan": state.research_plan,
            "raw_execution_context": state.raw_execution_context,
            "search_results": state.raw_execution_context,
            "verified_facts": state.verified_facts,
            "final_report": state.final_report,
        }
        for k, v in state.metadata.items():
            if isinstance(v, str):
                subs[k] = v

        result = compiled.safe_substitute(subs)

        # Emit a WARNING for each unresolved variable — do not silently strip.
        unresolved = {
            m.group(1)
            for m in _BeagleTemplate.pattern.finditer(result)
            if m.group("braced") or m.group("named")
        }
        if unresolved:
            logger.warning(
                f"Prompt template has unresolved variables: {sorted(unresolved)}. "
                f"Template: {template[:120]}..."
            )

        return result.strip()

    return builder


def build_dag_node(phase: dict[str, Any], spec: dict[str, Any]) -> DAGNode:
    """Build a DAGNode from a phase spec.

    Args:
        phase: Phase specification dict
        spec: Full workflow spec for context

    Returns:
        Configured DAGNode

    Raises:
        ValueError: If required fields are missing

    """
    # Validate required fields
    if "name" not in phase:
        raise ValueError("Phase must have 'name' field")
    if "agent" not in phase:
        raise ValueError(f"Phase '{phase['name']}' must have 'agent' field")
    if "prompt_template" not in phase:
        raise ValueError(f"Phase '{phase['name']}' must have 'prompt_template' field")

    output_key = phase.get("output_key", phase["name"])
    enable_validation = spec.get("enable_validation", True)
    requires_validation = enable_validation and phase.get("validator") is not None

    return DAGNode(
        name=phase["name"],
        skill_name=phase["agent"],
        state_mutator=create_mutator(output_key),
        prompt_builder=create_prompt_builder(phase["prompt_template"]),
        requires_validation=requires_validation,
    )


def build_transitions(
    dag: DAGOrchestrator, phases: list[dict[str, Any]], valid_names: set[str]
) -> None:
    """Build transitions between nodes based on dependencies.

    Args:
        dag: The orchestrator to add transitions to
        phases: List of phase specifications
        valid_names: Set of valid phase names for validation

    """
    phase_list = list(phases)

    for i, phase in enumerate(phase_list):
        current_name = phase["name"]
        depends_on = phase.get("depends_on", [])
        condition_str = phase.get("condition")

        # Validate dependencies exist
        for dep in depends_on:
            if dep not in valid_names:
                raise ValueError(f"Phase '{current_name}' depends on unknown phase '{dep}'")

        # If no dependencies, this phase follows the previous one sequentially
        if not depends_on and i > 0:
            prev_name = phase_list[i - 1]["name"]
            condition = parse_condition(condition_str)
            dag.add_transition(
                prev_name,
                current_name,
                condition or (lambda _state: True),
            )

        # If has dependencies, add transitions from each dependency
        for dep in depends_on:
            condition = parse_condition(condition_str)
            dag.add_transition(dep, current_name, condition or (lambda _state: True))


def build_orchestrator(spec: dict[str, Any], _base_path: Path) -> DAGOrchestrator:
    """Build an orchestrator from a parsed spec.

    Args:
        spec: Parsed YAML specification
        _base_path: Base path for relative references

    Returns:
        Configured DAGOrchestrator

    """
    # Guard against None from yaml.safe_load on empty files
    if not isinstance(spec, dict):
        raise ValueError(f"Workflow spec must be a YAML mapping, got {type(spec).__name__}")

    # Validate spec
    if "phases" not in spec:
        raise ValueError("Workflow must have 'phases' key")

    if not isinstance(spec["phases"], list):
        raise ValueError("'phases' must be a list")

    if len(spec["phases"]) == 0:
        raise ValueError("'phases' list cannot be empty")

    # Create orchestrator
    # v13.22.4 (P2-2): pass workflow_name through to the replay manifest
    # so the manifest can identify the originating workflow file.
    dag = DAGOrchestrator(workflow_name=str(spec.get("name", "") or ""))

    # Build nodes from phases
    phase_names: set[str] = set()
    first_node = True

    for i, phase in enumerate(spec["phases"]):
        if not isinstance(phase, dict):
            raise ValueError(f"Phase {i} must be a mapping, got {type(phase).__name__}")

        node = build_dag_node(phase, spec)

        # Track for dependency validation
        name = phase["name"]
        if name in phase_names:
            raise ValueError(f"Duplicate phase name: {name}")
        phase_names.add(name)

        # Add node to DAG
        dag.add_node(node, is_start=first_node)
        first_node = False

    # Build transitions from dependencies
    build_transitions(dag, spec["phases"], phase_names)

    return dag


def build_graph_from_spec(
    spec: dict[str, Any],
    workflow_query: str = "",
    complexity: str = "normal",
) -> StateGraph:
    """Build a LangGraph StateGraph from a parsed YAML spec.

    Args:
        spec: Parsed YAML workflow dictionary
        workflow_query: The original user query (for complexity routing)
        complexity: Pre-assessed complexity level ("trivial"/"normal"/"complex")

    """

    if not _HAS_LANGGRAPH:
        raise ImportError(
            "langgraph is required for graph workflows. Install with: pip install langgraph"
        )

    if not isinstance(spec, dict) or "phases" not in spec:
        raise ValueError("Workflow spec must have 'phases' key")

    nodes = []
    for phase in spec["phases"]:
        nodes.append(
            {
                "name": phase["name"],
                "skill_name": phase["agent"],
                "prompt_template": phase["prompt_template"],
                "output_key": phase.get("output_key", phase["name"]),
                "model": phase.get("model"),
                "enable_grpo": phase.get("enable_grpo", False),
                "budget_weight": phase.get("budget_weight", 1.0),
                "require_approval": phase.get("require_approval", False),
            }
        )

    # Build transitions
    transitions = []
    phase_list = spec["phases"]
    for i, phase in enumerate(phase_list):
        depends_on = phase.get("depends_on", [])
        condition = phase.get("condition")
        condition_field = None
        if condition and "is not empty" in condition:
            field_part = condition.replace(" is not empty", "").strip()
            condition_field = field_part.replace("state.", "").strip()

        if not depends_on and i > 0:
            transitions.append((phase_list[i - 1]["name"], phase["name"], condition_field))
        for dep in depends_on:
            transitions.append((dep, phase["name"], condition_field))

    return build_workflow_graph(nodes, transitions, workflow_query, complexity)
