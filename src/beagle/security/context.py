"""Unified SecurityContext for compositional block execution.

Threads through every code block invocation to enforce:
- Filesystem containment
- Command allowlisting
- Execution time limits
- Caller identity and permissions
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class SecurityContext:
    """Unified security boundary for block execution.

    Every ``invoke_tool()`` call in ``mcp_exposure.py`` should receive
    a SecurityContext that encodes the caller's identity, permissions,
    and containment parameters. Each block validates against this context
    before executing any privileged operation.

    Attributes:
        caller_id: Identity extracted from JWT sub claim or "anonymous".
        permissions: RBAC permission set (e.g. {"execution", "monitoring"}).
        allowed_paths: Root directory for filesystem containment.
        allowed_commands: Set of executable commands (empty = none allowed).
        max_execution_time: Hard timeout in seconds for block execution.
        created_at: Monotonic timestamp when the context was created.

    """

    caller_id: str
    permissions: set[str] = field(default_factory=set)
    allowed_paths: Path = field(default_factory=Path.cwd)
    allowed_commands: set[str] = field(default_factory=set)
    max_execution_time: int = 30
    created_at: float = field(default_factory=time.monotonic)

    def can_access_path(self, path: Path) -> bool:
        """Check whether *path* is contained within the allowed root.

        Resolves symlinks and rejects absolute paths that escape
        the containment boundary.

        Returns True if the path is safe; False otherwise.
        """
        try:
            resolved = (self.allowed_paths / path).resolve(strict=False)
            root = self.allowed_paths.resolve(strict=False)
            # Must start with root and not backtrack with .. after resolution
            resolved_parts = resolved.parts
            root_parts = root.parts
            if len(resolved_parts) < len(root_parts):
                return False
            return resolved_parts[: len(root_parts)] == root_parts
        except (OSError, ValueError):
            return False

    def can_execute(self, command: str) -> bool:
        """Check whether *command* is in the allowlist.

        Args:
            command: The executable name (e.g. "git", "ls").

        Returns True if the command is allowed; False otherwise.

        """
        if not self.allowed_commands:
            return False
        return command in self.allowed_commands

    @property
    def elapsed(self) -> float:
        """Seconds since this context was created."""
        return time.monotonic() - self.created_at

    @property
    def expired(self) -> bool:
        """Whether the context has exceeded its max execution time."""
        return self.elapsed > self.max_execution_time
