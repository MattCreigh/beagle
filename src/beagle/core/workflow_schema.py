"""Data classes for workflow definitions.

This module contains the schema definitions and types used by workflow_loader.
Extracted from workflow_loader.py for modularity.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass
class WorkflowPhase:
    """Represents a single phase in a workflow.

    Attributes:
        name: Unique phase identifier
        agent: Agent/skill name to execute
        prompt_template: Template string for the prompt
        output_key: Key to store results under
        depends_on: List of phase names this depends on
        condition: Optional condition for execution
        model: Model override hint
        enable_grpo: Enable GRPO parallel trajectories
        budget_weight: Weight for cost allocation
        require_approval: Require human approval before execution
        validator: Optional validator reference

    """

    name: str
    agent: str
    prompt_template: str
    output_key: str = ""
    depends_on: list[str] = field(default_factory=list)
    condition: str | None = None
    model: str | None = None
    enable_grpo: bool = False
    budget_weight: float = 1.0
    require_approval: bool = False
    validator: str | None = None

    def __post_init__(self) -> None:
        if not self.output_key:
            self.output_key = self.name


@dataclass
class WorkflowSpec:
    """Represents a complete workflow specification.

    Attributes:
        name: Workflow name
        description: Human-readable description
        phases: List of workflow phases
        mode: Workflow mode (audit, develop, research)
        enable_validation: Whether to enable validation phases

    """

    name: str = ""
    description: str = ""
    phases: list[WorkflowPhase] = field(default_factory=list)
    mode: str | None = None
    enable_validation: bool = True

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> WorkflowSpec:
        """Create a WorkflowSpec from a parsed YAML dictionary.

        Args:
            data: Parsed YAML workflow definition

        Returns:
            WorkflowSpec instance

        """
        phases = []
        for i, phase_data in enumerate(data.get("phases", [])):
            if not phase_data.get("name"):
                raise ValueError(f"Phase {i} missing required field 'name'")
            if not phase_data.get("agent"):
                raise ValueError(f"Phase {i} ({phase_data.get('name', '?')}) missing 'agent'")
            phases.append(
                WorkflowPhase(
                    name=phase_data["name"],
                    agent=phase_data["agent"],
                    prompt_template=phase_data.get("prompt_template", ""),
                    output_key=phase_data.get("output_key", phase_data.get("name", "")),
                    depends_on=phase_data.get("depends_on", []),
                    condition=phase_data.get("condition"),
                    model=phase_data.get("model"),
                    enable_grpo=phase_data.get("enable_grpo", False),
                    budget_weight=phase_data.get("budget_weight", 1.0),
                    require_approval=phase_data.get("require_approval", False),
                    validator=phase_data.get("validator"),
                )
            )

        return cls(
            name=data.get("name", ""),
            description=data.get("description", ""),
            phases=phases,
            mode=data.get("mode"),
            enable_validation=data.get("enable_validation", True),
        )


# Field mapping for state mutation
FIELD_MAPPING: dict[str, str] = {
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


class WorkflowPaths:
    """Utility class for workflow path handling."""

    @staticmethod
    def get_workspace_root() -> Path:
        """Get workspace root — delegates to the canonical implementation."""
        from ..utils.env_manager import get_workspace_root as _get_ws

        return _get_ws()

    @staticmethod
    def resolve_path(path: Path | str, base_dir: str = "metaprompts") -> Path:
        """Resolve a workflow path, handling relative paths.

        Args:
            path: Path to resolve
            base_dir: Base directory for relative paths

        Returns:
            Resolved absolute path

        """
        path = Path(path)
        if path.is_absolute():
            return path

        workspace = WorkflowPaths.get_workspace_root()
        # Avoid double-prepending if path already references base_dir
        if path.parts and path.parts[0] == base_dir:
            return workspace / path
        return workspace / base_dir / path

    @staticmethod
    def validate_path(path: Path) -> bool:
        """Validate workflow path to prevent path traversal attacks.

        Args:
            path: The path to validate

        Returns:
            True if path is safe, False otherwise

        """
        workspace = WorkflowPaths.get_workspace_root()

        try:
            check_path = workspace / path if not path.is_absolute() else path

            resolved = check_path.resolve()
            workspace_resolved = workspace.resolve()

            # Ensure resolved path is within workspace.
            # ``Path.relative_to`` raises ``ValueError`` (NOT generic Exception) when
            # the resolved path is outside the root, which is the precise condition
            # we want to detect. Using ``str.startswith`` here is a symlink-bypass
            # vector (audit S3, v13.17.0) — see FAR_VIEW_sup_context_optimization.md.
            try:
                resolved.relative_to(workspace_resolved)
            except ValueError:
                return False

            # Check for dangerous patterns
            path_str = str(path)
            dangerous = ["..", "~", "$", "`", ";", "|", "&", "\n", "\r", "\0"]
            return all(pattern not in path_str for pattern in dangerous)
        except (OSError, RuntimeError):
            # Narrow exception set (audit S2, v13.17.0). ``OSError`` covers
            # ``FileNotFoundError`` / permission errors; ``RuntimeError`` covers
            # symlink-loop errors from ``Path.resolve``. Other exceptions are
            # genuinely unexpected and must propagate so they appear in logs.
            return False
