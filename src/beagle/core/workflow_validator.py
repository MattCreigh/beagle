"""Validation logic for workflow definitions.

This module contains validation functions extracted from workflow_loader.py.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml


def validate_workflow_schema(spec: dict[str, Any] | None) -> list[str]:
    """Validate a parsed workflow schema.

    Args:
        spec: Parsed YAML workflow dictionary

    Returns:
        List of validation errors (empty if valid)

    """
    errors: list[str] = []

    if spec is None:
        return ["Empty workflow file"]

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

    # Validate each phase
    phase_names: set[str] = set()
    for i, phase in enumerate(spec["phases"]):
        errors.extend(_validate_phase(phase, i, phase_names))
        if isinstance(phase, dict) and "name" in phase:
            phase_names.add(phase["name"])

    # Validate dependencies reference valid phases
    for phase in spec["phases"]:
        if not isinstance(phase, dict):
            continue
        for dep in phase.get("depends_on", []):
            if dep not in phase_names:
                phase_name = phase.get("name", "unknown")
                errors.append(f"Phase '{phase_name}' depends on unknown phase '{dep}'")

    # Check for self-dependency
    for phase in spec["phases"]:
        if not isinstance(phase, dict):
            continue
        deps = set(phase.get("depends_on", []))
        if phase.get("name") in deps:
            errors.append(f"Phase '{phase['name']}' depends on itself")

    # Validate agent names match existing recipes
    agent_names = [phase.get("agent", "") for phase in spec["phases"] if isinstance(phase, dict)]
    agent_names = [a for a in agent_names if a]  # Filter empty
    if agent_names:
        missing = _validate_agent_names(agent_names)
        if missing:
            from ..utils.env_manager import get_workspace_root

            recipes_dir = get_workspace_root() / "recipes"
            available = {f.stem for f in recipes_dir.glob("*.xml")}
            errors.append(f"Unknown agents referenced: {missing}. Available: {sorted(available)}")

    return errors


def _validate_phase(phase: Any, index: int, phase_names: set[str]) -> list[str]:
    """Validate a single phase definition.

    Args:
        phase: Phase to validate
        index: Phase index for error messages
        phase_names: Set of known phase names

    Returns:
        List of validation errors

    """
    errors: list[str] = []

    if not isinstance(phase, dict):
        errors.append(f"Phase {index} must be a dict")
        return errors

    # Check name
    phase_name = phase.get("name", index)
    if "name" not in phase:
        errors.append(f"Phase {index} missing 'name'")
    else:
        name = phase["name"]
        if name in phase_names:
            errors.append(f"Duplicate phase name: {name}")

    # Check agent
    if "agent" not in phase:
        errors.append(f"Phase '{phase_name}' missing 'agent'")

    # Check prompt_template
    if "prompt_template" not in phase:
        errors.append(f"Phase '{phase_name}' missing 'prompt_template'")

    # Validate require_approval field if present
    if "require_approval" in phase and not isinstance(phase["require_approval"], bool):
        errors.append(f"Phase '{phase_name}': 'require_approval' must be a boolean")

    return errors


def _validate_agent_names(agent_names: list[str]) -> list[str]:
    """Check that agent names match existing recipes.

    Args:
        agent_names: List of agent/recipe names referenced in workflow

    Returns:
        List of missing agent names

    """
    from ..utils.env_manager import get_workspace_root

    recipes_dir = get_workspace_root() / "recipes"
    available = {f.stem for f in recipes_dir.glob("*.xml")}
    return [name for name in agent_names if name not in available]


def validate_workflow_file(path: Path | str) -> list[str]:
    """Validate a workflow file without executing it.

    Args:
        path: Path to the workflow file

    Returns:
        List of validation errors (empty if valid)

    """
    path = Path(path)

    try:
        with open(path, encoding="utf-8") as f:
            spec = yaml.safe_load(f)
    except FileNotFoundError:
        return [f"File not found: {path}"]
    except yaml.YAMLError as e:
        return [f"YAML parse error: {e}"]

    return validate_workflow_schema(spec)


def extract_workflow_mode(path: Path | str) -> str | None:
    """Extract the workflow_mode from a YAML workflow file.

    Args:
        path: Path to the workflow file (absolute or relative to metaprompts/)

    Returns:
        The mode string ("audit", "develop", or "research") if declared,
        or None if not present in the YAML.

    """
    path = Path(path)

    try:
        with open(path, encoding="utf-8") as f:
            spec = yaml.safe_load(f)
    except (FileNotFoundError, yaml.YAMLError):
        return None

    if not isinstance(spec, dict):
        return None

    return spec.get("mode")
