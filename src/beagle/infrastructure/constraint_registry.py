"""Constraint Registry for persistent user constraint management.

Extracts, stores, and retrieves user constraints across compaction boundaries.
Constraints are first-class entities that survive session resets and context
compaction cycles.

Categories:
    ARCHITECTURE: Design decisions (e.g., "Use Orpheus IPC, not Docker socket")
    PREFERENCE: User preferences (e.g., "Prefer type hints in all functions")
    RESTRICTION: Must-not constraints (e.g., "NO Docker socket mounts")
    REQUIREMENT: Must-have constraints (e.g., "All code must be typed")

Priority ordering ensures critical constraints are never dropped.
"""

from __future__ import annotations

import json
import logging
import os
import re
import time
import uuid
from dataclasses import dataclass, field
from enum import IntEnum
from pathlib import Path
from typing import Any

from beagle.config.paths import (
    get_constraints_dir as _get_constraints_dir,
)

logger = logging.getLogger("Beagle.constraint_registry")

# FHS-compliant path management


class ConstraintCategory(str):
    """Constraint categories for classification."""

    ARCHITECTURE = "architecture"  # Design decisions
    PREFERENCE = "preference"  # User preferences
    RESTRICTION = "restriction"  # Must-not constraints (NO/NEVER)
    REQUIREMENT = "requirement"  # Must-have constraints (MUST/ALWAYS)


class ConstraintPriority(IntEnum):
    """Priority levels for constraint ordering.

    Higher priority = more important = always included in context.
    """

    CRITICAL = 1  # Always include, never drop
    IMPORTANT = 2  # Include by default, drop only if critical space needed
    NICE_TO_HAVE = 3  # Include if space permits


@dataclass
class Constraint:
    """A user constraint that persists across compaction boundaries.

    Attributes:
        id: Unique identifier (UUID)
        category: Type of constraint (architecture/preference/restriction/requirement)
        description: Human-readable summary
        content: Full constraint text
        priority: Importance level (critical/important/nice_to_have)
        provenance: Source of constraint (message_id, session_id, file)
        created_at: Timestamp when constraint was created
        last_used: Timestamp of last inclusion in context
        use_count: Number of times constraint has been included
        project: Project/workspace this constraint applies to
        tags: Optional tags for filtering

    """

    id: str = field(default_factory=lambda: str(uuid.uuid4()))
    category: str = ConstraintCategory.RESTRICTION
    description: str = ""
    content: str = ""
    priority: int = ConstraintPriority.IMPORTANT
    provenance: dict[str, str] = field(default_factory=dict)
    created_at: float = field(default_factory=time.time)
    last_used: float = field(default_factory=time.time)
    use_count: int = 0
    project: str = ""
    tags: list[str] = field(default_factory=list)

    def to_json(self) -> dict[str, Any]:
        """Serialize constraint to JSON-compatible dict."""
        return {
            "id": self.id,
            "category": self.category,
            "description": self.description,
            "content": self.content,
            "priority": int(self.priority),
            "provenance": self.provenance,
            "created_at": self.created_at,
            "last_used": self.last_used,
            "use_count": self.use_count,
            "project": self.project,
            "tags": self.tags,
            "version": "1.0",
        }

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> Constraint:
        """Deserialize constraint from JSON dict."""
        return cls(
            id=data.get("id", str(uuid.uuid4())),
            category=data.get("category", ConstraintCategory.RESTRICTION),
            description=data.get("description", ""),
            content=data.get("content", ""),
            priority=ConstraintPriority(data.get("priority", ConstraintPriority.IMPORTANT)),
            provenance=data.get("provenance", {}),
            created_at=data.get("created_at", time.time()),
            last_used=data.get("last_used", time.time()),
            use_count=data.get("use_count", 0),
            project=data.get("project", ""),
            tags=data.get("tags", []),
        )

    def __str__(self) -> str:
        """Human-readable representation."""
        priority_name = ConstraintPriority(self.priority).name
        return f"[{priority_name}] [{self.category.upper()}] {self.description}"

    def format_for_context(self) -> str:
        """Format constraint for injection into agent context."""
        prefix = {  # type: ignore[call-overload]
            ConstraintPriority.CRITICAL: "⚠️ CRITICAL",
            ConstraintPriority.IMPORTANT: "📌 IMPORTANT",
            ConstraintPriority.NICE_TO_HAVE: "💡 NOTE",
        }.get(self.priority, "📌")

        return f"{prefix}: {self.content}"


@dataclass
class ConstraintSet:
    """A collection of constraints with ordering and filtering."""

    constraints: list[Constraint] = field(default_factory=list)

    def add(self, constraint: Constraint) -> None:
        """Add a constraint, avoiding duplicates."""
        # Check for duplicates by content similarity
        for existing in self.constraints:
            if self._is_duplicate(existing, constraint):
                logger.debug(f"Skipping duplicate constraint: {constraint.description}")
                return

        self.constraints.append(constraint)
        self._sort()

    def _is_duplicate(self, a: Constraint, b: Constraint) -> bool:
        """Check if two constraints are duplicates."""
        # Same content
        if a.content == b.content:
            return True

        # Similar description (fuzzy match)
        if a.description and b.description:
            a_words = set(a.description.lower().split())
            b_words = set(b.description.lower().split())
            overlap = len(a_words & b_words) / max(len(a_words), len(b_words), 1)
            if overlap > 0.8:
                return True

        return False

    def _sort(self) -> None:
        """Sort constraints by priority (critical first)."""
        self.constraints.sort(key=lambda c: c.priority)

    def get_for_context(self, max_tokens: int = 2000, current_tokens: int = 0) -> list[Constraint]:
        """Get constraints for context window, respecting token budget.

        Args:
            max_tokens: Maximum tokens for constraints section
            current_tokens: Current context token usage

        Returns:
            List of constraints to include, ordered by priority

        """
        available = max_tokens - current_tokens
        result = []
        token_count = 0

        for constraint in self.constraints:
            # Rough token estimate: ~4 chars per token
            constraint_tokens = len(constraint.format_for_context()) // 4

            if token_count + constraint_tokens <= available:
                result.append(constraint)
                token_count += constraint_tokens

        return result

    def to_json(self) -> dict[str, Any]:
        """Serialize constraint set."""
        return {
            "constraints": [c.to_json() for c in self.constraints],
            "version": "1.0",
        }

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> ConstraintSet:
        """Deserialize constraint set."""
        constraints = [Constraint.from_json(c) for c in data.get("constraints", [])]
        return cls(constraints=constraints)


class ConstraintRegistry:
    """Persistent registry for user constraints.

    Manages constraint storage, retrieval, and lifecycle across
    compaction boundaries and session resets.

    Storage:
        ~/.cache/beagle/constraints/
            ├── global.json          # Global constraints (all projects)
            └── {project_hash}.json  # Project-specific constraints
    """

    def __init__(self, project: str = ""):
        """Initialize registry.

        Args:
            project: Project/workspace identifier for project-specific constraints

        """
        self.project = project
        self._constraints_dir = None
        self._global_constraints: ConstraintSet = ConstraintSet()
        self._project_constraints: ConstraintSet = ConstraintSet()
        self._dirty = False

    @property
    def constraints_dir(self) -> Path:
        """Get constraints storage directory."""
        if self._constraints_dir is None:
            self._constraints_dir = _get_constraints_dir()  # type: ignore[assignment]
            self._constraints_dir.mkdir(parents=True, exist_ok=True)  # type: ignore[attr-defined]
        return self._constraints_dir  # type: ignore[return-value]

    def _global_path(self) -> Path:
        """Path to global constraints file."""
        return self.constraints_dir / "global.json"

    def _project_path(self) -> Path:
        """Path to project-specific constraints file."""
        # Use deterministic hash to avoid filesystem issues with project names.
        # B-11 (audit v13.22.0): blake2b replaces md5 for consistency with
        # the project doctrine. digest_size=4 → 8 hex chars (same as before).
        import hashlib

        project_hash = int(hashlib.blake2b(self.project.encode(), digest_size=4).hexdigest(), 16)
        return self.constraints_dir / f"project_{project_hash}.json"

    def load(self) -> None:
        """Load constraints from disk."""
        # Load global constraints
        global_path = self._global_path()
        if global_path.exists():
            try:
                with open(global_path, encoding="utf-8") as f:
                    data = json.load(f)
                self._global_constraints = ConstraintSet.from_json(data)
                logger.info(
                    f"Loaded {len(self._global_constraints.constraints)} global constraints"
                )
            except OSError as e:
                logger.warning(f"Failed to load global constraints: {e}")

        # Load project constraints
        if self.project:
            project_path = self._project_path()
            if project_path.exists():
                try:
                    with open(project_path, encoding="utf-8") as f:
                        data = json.load(f)
                    self._project_constraints = ConstraintSet.from_json(data)
                    logger.info(
                        f"Loaded {len(self._project_constraints.constraints)} project constraints"
                    )
                except OSError as e:
                    logger.warning(f"Failed to load project constraints: {e}")

    def save(self) -> None:
        """Save constraints to disk."""
        if not self._dirty:
            return

        # Ensure directories exist
        self.constraints_dir.mkdir(parents=True, exist_ok=True)

        # Save global constraints
        global_path = self._global_path()
        try:
            temp_path = global_path.with_suffix(".tmp")
            with open(temp_path, "w", encoding="utf-8") as f:
                json.dump(self._global_constraints.to_json(), f, indent=2)
            os.replace(temp_path, global_path)
            logger.debug(f"Saved global constraints to {global_path}")
        except (OSError, ValueError, TypeError, KeyError, json.JSONDecodeError) as e:
            logger.error(f"Failed to save global constraints: {e}")

        # Save project constraints
        if self.project:
            project_path = self._project_path()
            try:
                temp_path = project_path.with_suffix(".tmp")
                with open(temp_path, "w", encoding="utf-8") as f:
                    json.dump(self._project_constraints.to_json(), f, indent=2)
                os.replace(temp_path, project_path)
                logger.debug(f"Saved project constraints to {project_path}")
            except (json.JSONDecodeError, ValueError) as e:
                logger.error(f"Failed to save project constraints: {e}")

        self._dirty = False

    def register(self, constraint: Constraint, global_scope: bool = False) -> None:
        """Register a new constraint.

        Args:
            constraint: Constraint to register
            global_scope: If True, constraint applies to all projects

        """
        if not constraint.project:
            constraint.project = self.project

        if global_scope:
            self._global_constraints.add(constraint)
            logger.info(f"Registered global constraint: {constraint}")
        else:
            self._project_constraints.add(constraint)
            logger.info(f"Registered project constraint: {constraint}")

        self._dirty = True

    def get_active(self, include_global: bool = True) -> list[Constraint]:
        """Get all active constraints for current context.

        Args:
            include_global: Whether to include global constraints

        Returns:
            Combined list of constraints, sorted by priority

        """
        result = []

        if include_global:
            result.extend(self._global_constraints.constraints)

        result.extend(self._project_constraints.constraints)

        # Update use tracking
        for c in result:
            c.last_used = time.time()
            c.use_count += 1

        self._dirty = True
        return sorted(result, key=lambda c: c.priority)

    def get_critical(self) -> list[Constraint]:
        """Get only critical constraints (for minimal context)."""
        return [c for c in self.get_active() if c.priority == ConstraintPriority.CRITICAL]

    def get_restrictions(self) -> list[Constraint]:
        """Get all restriction-type constraints (NO/NEVER constraints)."""
        return [c for c in self.get_active() if c.category == ConstraintCategory.RESTRICTION]

    def format_for_prompt(self, max_tokens: int = 2000) -> str:
        """Format all active constraints for injection into prompt.

        Args:
            max_tokens: Maximum tokens for constraints section

        Returns:
            Formatted constraints section string

        """
        constraints = self.get_active()

        if not constraints:
            return ""

        lines = ["## Active Constraints", ""]
        lines.append("The following constraints MUST be respected:")
        lines.append("")

        token_estimate = 50  # Header overhead

        for constraint in constraints:
            formatted = constraint.format_for_context()
            line_tokens = len(formatted) // 4

            if token_estimate + line_tokens > max_tokens:
                break

            lines.append(f"- {formatted}")
            token_estimate += line_tokens

        lines.append("")
        lines.append("---")

        return "\n".join(lines)

    def merge_with_compaction(self, existing_prompt: str = "") -> str:
        """Merge constraints into a compaction resume prompt.

        Args:
            existing_prompt: Existing resume prompt text

        Returns:
            Updated prompt with constraints section

        """
        constraints_section = self.format_for_prompt()

        if not constraints_section:
            return existing_prompt

        if "## Active Constraints" in existing_prompt:
            # Replace existing constraints section
            pattern = r"## Active Constraints.*?(?=\n---|\n##|\Z)"
            return re.sub(pattern, constraints_section, existing_prompt, flags=re.DOTALL)
        else:
            # Add constraints section at appropriate location
            # After context header, before other content
            if "## " in existing_prompt:
                # Insert before first ##
                first_header = existing_prompt.find("## ")
                return (
                    existing_prompt[:first_header]
                    + constraints_section
                    + existing_prompt[first_header:]
                )
            else:
                # Append to end
                return existing_prompt + "\n\n" + constraints_section

    def clear(self, global_scope: bool = False) -> None:
        """Clear constraints.

        Args:
            global_scope: If True, clear global constraints. Otherwise project.

        """
        if global_scope:
            self._global_constraints = ConstraintSet()
        else:
            self._project_constraints = ConstraintSet()

        self._dirty = True

    def __enter__(self) -> ConstraintRegistry:
        """Context manager entry - load constraints."""
        self.load()
        return self

    def __exit__(self, exc_type, exc_val, _exc_tb) -> None:
        """Context manager exit - save constraints."""
        self.save()
