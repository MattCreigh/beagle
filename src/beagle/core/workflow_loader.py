"""Load and execute YAML-defined workflows for Goose.

Enables declarative workflow definitions that can be loaded
and executed dynamically, supporting machine-readable metaprompts.

Supports both legacy DAGOrchestrator and new LangGraph StateGraph.
"""

from __future__ import annotations

import contextlib
import logging
import re
from collections.abc import Callable
from pathlib import Path
from typing import Any

import yaml  # type: ignore[import-untyped]

# v1.1.1 (S5): workflow/metaprompts data moved to the canonical config root;
# resolve it through find_metaprompts_dir() / find_recipes_dir().
from ..config._config_path import find_metaprompts_dir, find_recipes_dir
from ..utils.env_manager import get_workspace_root
from .autonomous_orchestrator import DAGOrchestrator
from .orchestrator_types import AgentState, DAGNode

logger = logging.getLogger(__name__)

try:
    from langgraph.graph import StateGraph

    _HAS_LANGGRAPH = True
except ImportError:
    _HAS_LANGGRAPH = False


def _validate_workflow_path(path: Path) -> bool:
    """Validate workflow path to prevent path traversal attacks.

    Args:
        path: The path to validate

    Returns:
        True if path is safe, False otherwise

    """
    workspace = get_workspace_root()

    # Convert to absolute and resolve symlinks
    try:
        check_path = workspace / path if not path.is_absolute() else path
        resolved = check_path.resolve()

        # Ensure resolved path is within workspace (relative_to raises ValueError if not)
        try:
            resolved.relative_to(workspace.resolve())
        except ValueError:
            return False

        # Check for dangerous patterns
        path_str = str(path)
        dangerous = ["..", "~", "$", "`", ";", "|", "&", "\n", "\r", "\0"]
        return all(pattern not in path_str for pattern in dangerous)
    except (TypeError, ValueError, AttributeError):
        return False


def load_workflow(path: Path | str) -> DAGOrchestrator:
    """Load a workflow from YAML definition.

    Args:
        path: Path to the YAML workflow file (absolute or relative to metaprompts/)

    Returns:
        Configured DAGOrchestrator ready for execution

    Raises:
        FileNotFoundError: If the workflow file doesn't exist
        ValueError: If the workflow spec is invalid

    """
    path = Path(path)

    # Validate path to prevent traversal attacks
    if not _validate_workflow_path(path):
        raise ValueError(
            f"Invalid workflow path: {path}. Path traversal is not allowed. "
            f"Workflows must be within: {get_workspace_root()}"
        )

    # If relative path, look in metaprompts directory
    if not path.is_absolute():
        path = find_metaprompts_dir() / path

    if not path.exists():
        # Try appending .yaml extension
        yaml_path = path.parent / f"{path.name}.yaml"
        if yaml_path.exists():
            path = yaml_path
        else:
            raise FileNotFoundError(f"Workflow file not found: {path}")

    # v13.22.4: Render Jinja templates ({{ preset.xxx }}) before parsing.
    from beagle.config.yaml_template import load_yaml_with_templates

    spec = load_yaml_with_templates(path)

    return _build_orchestrator(spec, path.parent, steering_prompt="")


def load_workflow_from_string(yaml_content: str, steering_prompt: str = "") -> DAGOrchestrator:
    """Load a workflow from YAML string.

    Args:
        yaml_content: YAML workflow definition
        steering_prompt: Optional caller-supplied steering directive.
            Forwarded to the steering-mode validator.

    Returns:
        Configured DAGOrchestrator

    """
    spec = yaml.safe_load(yaml_content)
    return _build_orchestrator(
        spec,
        find_metaprompts_dir(),
        steering_prompt=steering_prompt,
    )


def load_workflow_graph(
    path: Path | str,
    workflow_query: str = "",
    complexity: str | None = None,
    steering_prompt: str = "",
) -> StateGraph:
    """Load a workflow as a LangGraph StateGraph.

    Args:
        path: Path to the YAML workflow file
        workflow_query: The original user query (for complexity assessment)
        complexity: Pre-assessed complexity level. If None, auto-detected from query.

    Returns:
        Compiled StateGraph ready for execution

    Raises:
        FileNotFoundError: If workflow file doesn't exist
        ImportError: If langgraph is not installed

    """
    from ..config.config import assess_task_complexity

    if not _HAS_LANGGRAPH:
        raise ImportError(
            "langgraph is required for graph workflows. Install with: pip install langgraph"
        )

    path = Path(path)

    # PATH VALIDATION: Prevent path traversal attacks
    if not _validate_workflow_path(path):
        raise ValueError(
            f"Invalid workflow path: {path}. Path traversal is not allowed. "
            f"Workflows must be within: {get_workspace_root()}"
        )

    if not path.is_absolute():
        # v1.1.1 (S5): resolve via canonical metaprompts dir
        path = find_metaprompts_dir() / path
    else:
        # Absolute path: resolve symlinks and check existence
        with contextlib.suppress(Exception):
            path = path.resolve()

    # v13.22.4: Render Jinja templates before parsing.
    from beagle.config.yaml_template import load_yaml_with_templates

    spec = load_yaml_with_templates(path)

    if complexity is None:
        complexity = assess_task_complexity(workflow_query)

    return _build_graph_from_spec(spec, workflow_query, complexity, steering_prompt=steering_prompt)


def _build_graph_from_spec(
    spec: dict[str, Any],
    workflow_query: str = "",
    complexity: str = "normal",
    steering_prompt: str = "",
) -> StateGraph:
    """Build a LangGraph StateGraph from a parsed YAML spec.

    Args:
        spec: Parsed YAML workflow dictionary
        workflow_query: The original user query (for complexity routing)
        complexity: Pre-assessed complexity level ("trivial"/"normal"/"complex")

    """
    if not isinstance(spec, dict) or "phases" not in spec:
        raise ValueError("Workflow spec must have 'phases' key")

    # v13.22.4 (P2-3): enforce steering-mode contract (see helper docstring).
    _validate_steering_mode(spec, steering_prompt)

    # ── v12.3: Normalize legacy YAML field names to canonical BeagleState fields ──
    # Recipes that use 'steps' instead of 'step_count', 'permissions' instead of
    # 'permission_level', etc., would cause KeyError when hydration_node.py tries
    # to write them into the strict BeagleState TypedDict.  Translate here.

    _LEGACY_TOP_LEVEL = {
        "steps": "step_count",
        "permissions": "permission_level",
        "id": "workflow_id",
        "state": "status",
        "context": "metadata",
        "run_mode": "mode",
        "wf_id": "workflow_id",
        "extra_metadata": "metadata",
        "meta": "metadata",
    }

    for legacy_key, canonical_key in _LEGACY_TOP_LEVEL.items():
        if legacy_key in spec and canonical_key not in spec:
            spec[canonical_key] = spec.pop(legacy_key)

    # Normalize per-phase legacy fields
    for phase in spec.get("phases", []):
        for legacy_key, canonical_key in _LEGACY_TOP_LEVEL.items():
            if legacy_key in phase and canonical_key not in phase:
                phase[canonical_key] = phase.pop(legacy_key)

    nodes = []
    for phase in spec["phases"]:
        nodes.append(
            {
                "name": phase["name"],
                "skill_name": phase["agent"],
                "prompt_template": phase["prompt_template"],
                "output_key": phase.get("output_key", phase["name"]),
                # YAML workflow model hints (override per-recipe config routing)
                "model": phase.get("model"),
                # GRPO flag — if true, run 3 parallel trajectories and pick the best
                "enable_grpo": phase.get("enable_grpo", False),
                # Budget weight for cost allocation
                "budget_weight": phase.get("budget_weight", 1.0),
                # Human-in-the-loop: requires approval before execution
                "require_approval": phase.get("require_approval", False),
                # ── LangChain Bridge executors (Phases 2, 3, 5) ──
                # "goose" (default) | "langchain_tool" | "langchain_llm" | "a2a_remote"
                "executor": phase.get("executor", "goose"),
                # Tool/LMM-specific fields (used when executor != "goose")
                "tool_name": phase.get("tool_name"),
                "tool_method": phase.get("tool_method"),
                "input_mapping": phase.get("input_mapping", {}),
                "agent_url": phase.get("agent_url"),
                "agent_name": phase.get("agent_name"),
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

    # SP-7: import from the graph_builder leaf, not core.graph, so the
    # core.graph <-> core.workflow_loader cycle is broken (core.graph._run_workflow_impl
    # lazily imports load_workflow_graph from this module).
    from .graph_builder import build_workflow_graph

    return build_workflow_graph(nodes, transitions, workflow_query, complexity)


def _validate_steering_mode(
    spec: dict[str, Any],
    steering_prompt: str,
) -> None:
    """v13.22.4 (P2-3): validate a workflow's declared steering-mode
    requirement against the caller-supplied ``steering_prompt``.

    Workflows declare their mode with the top-level YAML key
    ``requires_steering_mode``. Allowed values:

    - ``"read-write"`` (default) — workflow mutates the codebase; the
      caller may supply any steering, but a steering marked
      READ-ONLY is rejected as a self-contradictory contract.
    - ``"read-only"`` — workflow must NOT mutate; any steering
      containing mutation markers is rejected.

    Raises:
        ValueError: when the declared mode conflicts with the supplied
            steering_prompt. The error message names the offending
            workflow so the operator can decide whether to (a) change
            the steering, (b) change the workflow's
            ``requires_steering_mode`` declaration, or (c) pick a
            different workflow.

    """
    _requires_mode = str(spec.get("requires_steering_mode", "read-write") or "read-write").lower()
    _READ_ONLY_MARKERS = (
        "read-only",
        "read_only",
        "do not mutate",
        "do not modify",
        "diagnostic only",
        "readonly",
        "no edits",
    )
    _READ_WRITE_MARKERS = (
        "mutate",
        "modify the codebase",
        "write changes",
        "edit source",
        "apply changes",
    )
    _steering_lower = (steering_prompt or "").lower()
    _reads_only = any(m in _steering_lower for m in _READ_ONLY_MARKERS)
    _wants_write = any(m in _steering_lower for m in _READ_WRITE_MARKERS)
    if _requires_mode == "read-only" and _wants_write:
        raise ValueError(
            f"Workflow '{spec.get('name', '?')}' declares "
            f"requires_steering_mode='read-only' but the supplied "
            f"steering_prompt contains mutation markers. Refusing to "
            f"load."
        )
    if _requires_mode == "read-write" and _reads_only:
        raise ValueError(
            f"Workflow '{spec.get('name', '?')}' declares "
            f"requires_steering_mode='read-write' (it mutates the "
            f"codebase) but the supplied steering_prompt says the run "
            f"must be read-only. Either change the workflow's "
            f"requires_steering_mode to 'read-only' or drop the "
            f"read-only directive from steering."
        )


def _build_orchestrator(
    spec: dict[str, Any],
    _base_path: Path,
    steering_prompt: str = "",
) -> DAGOrchestrator:
    """Build an orchestrator from a parsed spec.

    Args:
        spec: Parsed YAML specification
        _base_path: Base path for relative references
        steering_prompt: Optional high-priority directive passed by the
            caller (e.g. ``run_beagle_workflow``'s ``steering_prompt``).
            Used to validate against the workflow's declared mode.

    Returns:
        Configured DAGOrchestrator

    Raises:
        ValueError: If the workflow declares a steering-mode requirement
            that conflicts with the supplied ``steering_prompt`` (v13.22.4
            P2-3).

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

    # v13.22.4 (P2-3): validate the workflow's declared steering_mode
    # against the supplied steering_prompt. Workflows that mutate the
    # codebase (mode=develop) cannot accept READ-ONLY steering, and a
    # READ-ONLY workflow cannot accept steering that asks for
    # mutation. The check is conservative — it looks for explicit
    # markers rather than guessing intent.
    _validate_steering_mode(spec, steering_prompt)

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

        node = _build_node(phase, spec)

        # Track for dependency validation
        name = phase["name"]
        if name in phase_names:
            raise ValueError(f"Duplicate phase name: {name}")
        phase_names.add(name)

        # Add node to DAG
        dag.add_node(node, is_start=first_node)
        first_node = False

    # Build transitions from dependencies
    _build_transitions(dag, spec["phases"], phase_names)

    return dag


def _build_node(phase: dict[str, Any], spec: dict[str, Any]) -> DAGNode:
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
        state_mutator=_create_mutator(output_key),
        prompt_builder=_create_prompt_builder(phase["prompt_template"]),
        requires_validation=requires_validation,
    )


def _build_transitions(
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
            condition = _parse_condition(condition_str)
            # Use default argument capture to avoid closure bug
            dag.add_transition(
                prev_name,
                current_name,
                condition or (lambda _, _c=current_name: True),  # type: ignore[misc]
            )

        # If has dependencies, add transitions from each dependency
        for dep in depends_on:
            condition = _parse_condition(condition_str)
            # Use default argument capture to avoid closure bug
            dag.add_transition(dep, current_name, condition or (lambda _, _c=current_name: True))  # type: ignore[misc]


def _create_mutator(key: str) -> Callable[[AgentState, str], None]:
    """Create a state mutator function.

    The mutator stores results in the appropriate AgentState field
    based on the key name.

    Args:
        key: The key/field to store the result under

    Returns:
        Mutator function

    """
    # Map common keys to AgentState fields
    field_mapping = {
        "research_plan": "research_plan",
        "planning": "research_plan",
        "search_results": "raw_execution_context",
        "search": "raw_execution_context",
        "execution": "raw_execution_context",
        "verified_facts": "verified_facts",
        "verification": "verified_facts",
        "final_report": "final_report",
        "synthesis": "final_report",
    }

    target_field = field_mapping.get(key)

    def mutator(state: AgentState, result: str) -> None:
        # Store in metadata for all keys
        state.metadata[key] = result

        # Also store in specific field if mapped
        if target_field:
            setattr(state, target_field, result)

    return mutator


def _create_prompt_builder(template: str) -> Callable[[AgentState], str]:
    """Create a prompt builder function from a template.

    Supports {variable} substitution from state attributes and metadata.

    Args:
        template: The prompt template string

    Returns:
        Prompt builder function

    """

    def builder(state: AgentState) -> str:
        # Build substitution dict from state
        subs = {
            "query": state.query,
            "research_plan": state.research_plan,
            "raw_execution_context": state.raw_execution_context,
            "search_results": state.raw_execution_context,
            "verified_facts": state.verified_facts,
            "final_report": state.final_report,
        }

        # Add all metadata
        for k, v in state.metadata.items():
            if isinstance(v, str):
                subs[k] = v

        # Perform substitution with safe defaults
        result = template
        for key, value in subs.items():
            result = result.replace(f"{{{key}}}", str(value) if value is not None else "")

        # Handle any remaining unsubstituted variables
        result = re.sub(r"\{[a-zA-Z_][a-zA-Z0-9_]*\}", "", result)

        return result.strip()

    return builder


def _parse_condition(condition_str: str | None) -> Callable[[AgentState], bool] | None:
    """Parse a condition string into a callable.

    Supports simple expressions:
    - "state.query contains 'performance'"
    - "state.research_plan is not empty"
    - "state.raw_execution_context is empty"
    - "always" (always true)
    - "never" (always false)

    Args:
        condition_str: The condition string to parse

    Returns:
        Condition function or None if no condition

    """
    if not condition_str:
        return None

    condition_str = condition_str.strip()

    # Special cases
    if condition_str.lower() == "always":
        return lambda _: True
    if condition_str.lower() == "never":
        return lambda _: False

    # Parse "contains" expressions
    if " contains " in condition_str:
        field_part, _, value_part = condition_str.partition(" contains ")
        field = field_part.replace("state.", "").strip()
        value = value_part.strip().strip("'\"")

        def contains_condition(state: AgentState, f: str = field, v: str = value) -> bool:
            # Check direct attributes
            if hasattr(state, f):
                return v in str(getattr(state, f, ""))
            # Check metadata
            if f in state.metadata:
                return v in str(state.metadata[f])
            return False

        return contains_condition

    # Parse "is not empty" expressions
    if " is not empty" in condition_str:
        field_part = condition_str.replace(" is not empty", "").strip()
        field = field_part.replace("state.", "").strip()

        def not_empty_condition(state: AgentState, f: str = field) -> bool:
            if hasattr(state, f):
                val = getattr(state, f, None)
                return bool(val and str(val).strip())
            return bool(state.metadata.get(f))

        return not_empty_condition

    # Parse "is empty" expressions
    if " is empty" in condition_str:
        field_part = condition_str.replace(" is empty", "").strip()
        field = field_part.replace("state.", "").strip()

        def empty_condition(state: AgentState, f: str = field) -> bool:
            if hasattr(state, f):
                val = getattr(state, f, None)
                return not val or not str(val).strip()
            return not state.metadata.get(f)

        return empty_condition

    # Fail loud — do not silently route to always-true.
    raise ValueError(
        f"Unrecognized workflow condition: {condition_str!r}. "
        f"Supported: 'always', 'never', 'state.<field> is [not] empty', "
        f"'state.<field> contains \\'<value>\\''"
    )


def get_workflow_mode(path: Path | str) -> str | None:
    """Extract the workflow_mode from a YAML workflow file.

    Args:
        path: Path to the workflow file (absolute or relative to metaprompts/)

    Returns:
        The mode string ("audit", "develop", or "research") if declared,
        or None if not present in the YAML.

    """
    path = Path(path)

    # Handle relative paths — resolve short names like "audit" to "audit.yaml"
    if not path.is_absolute():
        path = find_metaprompts_dir() / path

    # Auto-append .yaml extension when the bare name doesn't exist
    if not path.exists() and not str(path).endswith(".yaml"):
        yaml_path = Path(str(path) + ".yaml")
        if yaml_path.exists():
            path = yaml_path

    try:
        from beagle.config.yaml_template import load_yaml_with_templates

        spec = load_yaml_with_templates(path)
    except (FileNotFoundError, yaml.YAMLError, RuntimeError):
        return None

    if not isinstance(spec, dict):
        return None

    return spec.get("mode")  # Returns None if key absent


def get_workflow_nodes(path: Path | str) -> list[dict[str, Any]]:
    """Get the list of nodes defined in a workflow without building the graph.

    Args:
        path: Path to the YAML workflow file

    Returns:
        List of node spec dicts

    """
    path = Path(path)
    if not path.is_absolute():
        path = find_metaprompts_dir() / path

    # Auto-append .yaml extension when the bare name doesn't exist
    if not path.exists() and not str(path).endswith(".yaml"):
        yaml_path = Path(str(path) + ".yaml")
        if yaml_path.exists():
            path = yaml_path

    if not path.exists():
        return []

    try:
        from beagle.config.yaml_template import load_yaml_with_templates

        spec = load_yaml_with_templates(path)
    except (FileNotFoundError, ValueError, RuntimeError, ImportError, OSError):
        return []

    if not isinstance(spec, dict) or "phases" not in spec:
        return []

    nodes = []
    for phase in spec["phases"]:
        nodes.append(
            {
                "name": phase.get("name"),
                "skill_name": phase.get("agent"),
                "model": phase.get("model"),
            }
        )
    return nodes


def validate_workflow(path: Path | str) -> list[str]:
    """Validate a workflow file without executing it.

    Args:
        path: Path to the workflow file

    Returns:
        List of validation errors (empty if valid)

    """
    errors: list[str] = []
    path = Path(path)

    # Handle relative paths — resolve short names like "audit" to "audit.yaml"
    if not path.is_absolute():
        path = find_metaprompts_dir() / path

    # Auto-append .yaml extension when the bare name doesn't exist
    if not path.exists() and not str(path).endswith(".yaml"):
        yaml_path = Path(str(path) + ".yaml")
        if yaml_path.exists():
            path = yaml_path

    try:
        from beagle.config.yaml_template import load_yaml_with_templates

        spec = load_yaml_with_templates(path)
    except FileNotFoundError:
        return [f"File not found: {path}"]
    except yaml.YAMLError as e:
        return [f"YAML parse error: {e}"]
    except RuntimeError as e:
        return [f"Template render error: {e}"]

    if spec is None:
        return ["Empty workflow file"]
    if not isinstance(spec, dict):
        return [f"Workflow must be a YAML mapping, got {type(spec).__name__}"]

    # Check required fields
    if "phases" not in spec:
        errors.append("Missing 'phases' key")
        return errors

    if not isinstance(spec["phases"], list):
        errors.append("'phases' must be a list")
        return errors

    if len(spec["phases"]) == 0:
        errors.append("'phases' list cannot be empty")
        return errors

    # v13.22.4 (P2-3): validate the declared steering mode is one of
    # the allowed values. Runtime validation against the live
    # steering_prompt is performed by _validate_steering_mode at
    # load-time; static validation here just catches malformed YAML.
    _mode = spec.get("requires_steering_mode", "read-write")
    if not isinstance(_mode, str) or _mode.lower() not in ("read-write", "read-only"):
        errors.append(f"requires_steering_mode must be 'read-write' or 'read-only', got {_mode!r}")

    # Validate each phase
    phase_names: set[str] = set()
    for i, phase in enumerate(spec["phases"]):
        if not isinstance(phase, dict):
            errors.append(f"Phase {i} must be a dict")
            continue

        if "name" not in phase:
            errors.append(f"Phase {i} missing 'name'")
        else:
            name = phase["name"]
            if name in phase_names:
                errors.append(f"Duplicate phase name: {name}")
            phase_names.add(name)

        if "agent" not in phase:
            errors.append(f"Phase '{phase.get('name', i)}' missing 'agent'")

        if "prompt_template" not in phase:
            errors.append(f"Phase '{phase.get('name', i)}' missing 'prompt_template'")

        # Validate require_approval field if present
        if "require_approval" in phase and not isinstance(phase["require_approval"], bool):
            errors.append(f"Phase '{phase.get('name', i)}': 'require_approval' must be a boolean")

    # Validate dependencies reference valid phases
    for phase in spec["phases"]:
        for dep in phase.get("depends_on", []):
            if dep not in phase_names:
                errors.append(f"Phase '{phase.get('name')}' depends on unknown phase '{dep}'")

    # Check for self-dependency
    for phase in spec["phases"]:
        deps = set(phase.get("depends_on", []))
        if phase.get("name") in deps:
            errors.append(f"Phase '{phase['name']}' depends on itself")

    # Validate agent names match existing recipes
    agent_names = [phase.get("agent", "") for phase in spec["phases"]]
    agent_names = [a for a in agent_names if a]  # Filter empty
    if agent_names and not _validate_agent_names(agent_names):
        # v1.1.1 (S5): recipes moved to the canonical config root.
        recipes_dir = find_recipes_dir()
        available = {f.stem for f in recipes_dir.glob("*.xml")}
        missing = [a for a in agent_names if a not in available]
        errors.append(f"Unknown agents referenced: {missing}. Available: {sorted(available)}")

    return errors


def _validate_agent_names(agent_names: list[str]) -> bool:
    """Check that agent names match existing recipes.

    Args:
        agent_names: List of agent/recipe names referenced in workflow

    Returns:
        True if all agents have corresponding recipe files

    """
    # v1.1.1 (S5): recipes moved to the canonical config root.
    recipes_dir = find_recipes_dir()
    available = {f.stem for f in recipes_dir.glob("*.xml")}
    return all(name in available for name in agent_names)


def list_workflows() -> list[dict[str, Any]]:
    """List all available workflow files.

    Returns:
        List of workflow metadata dicts with keys: name, path, description

    """
    metaprompts_dir = find_metaprompts_dir()

    if not metaprompts_dir.exists():
        return []

    workflows = []
    for path in sorted(metaprompts_dir.glob("*.yaml")):
        try:
            from beagle.config.yaml_template import load_yaml_with_templates

            spec = load_yaml_with_templates(path)
            workflows.append(
                {
                    "name": spec.get("name", path.stem),
                    "path": str(path),
                    "description": spec.get("description", ""),
                    "phases": len(spec.get("phases", [])),
                }
            )
        except (
            FileNotFoundError,
            ValueError,
            RuntimeError,
            ImportError,
            OSError,
            KeyError,
            TypeError,
        ) as exc:
            logger.warning(
                "Cannot load workflow spec %s (%s); it is omitted from the listing.",
                path,
                exc,
            )
            continue

    return workflows


if __name__ == "__main__":
    # Quick test
    logger.info("Available workflows:")
    for wf in list_workflows():
        logger.info(f"  - {wf['name']}: {wf['description']} ({wf['phases']} phases)")

    logger.info("\nValidating research.yaml...")
    errors = validate_workflow("research.yaml")
    if errors:
        logger.info("Errors:")
        for e in errors:
            logger.info(f"  - {e}")
    else:
        logger.info("Valid!")
