"""Permission context for granular tool execution control."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field


@dataclass(frozen=True)
class ToolPermissionContext:
    """Context defining which tools are allowed or denied.

    Inspired by claw-code architecture for fine-grained safety.
    """

    deny_names: set[str] = field(default_factory=frozenset)  # type: ignore[arg-type]
    deny_prefixes: tuple[str, ...] = field(default_factory=tuple)
    allow_names: set[str] | None = None  # If set, ONLY these are allowed

    def blocks(self, tool_name: str) -> bool:
        """Check if a tool is blocked by this context."""
        lowered = tool_name.lower()

        # 1. Check explicit allows
        if self.allow_names is not None and lowered not in self.allow_names:
            return True

        # 2. Check explicit denies
        if lowered in self.deny_names:
            return True

        # 3. Check prefix denies
        return bool(any(lowered.startswith(p) for p in self.deny_prefixes))

    @classmethod
    def from_iterables(
        cls,
        deny_names: Iterable[str] | None = None,
        deny_prefixes: Iterable[str] | None = None,
        allow_names: Iterable[str] | None = None,
    ) -> ToolPermissionContext:
        """Create a context from iterables of strings."""
        return cls(
            deny_names=frozenset(n.lower() for n in (deny_names or [])),  # type: ignore[arg-type]
            deny_prefixes=tuple(p.lower() for p in (deny_prefixes or [])),
            allow_names=frozenset(n.lower() for n in allow_names)  # type: ignore[arg-type]
            if allow_names is not None
            else None,
        )


# Default permissive context
DEFAULT_PERMISSION_CONTEXT = ToolPermissionContext()

# Read-only restricted context. D-15 (release-readiness audit 2026-08-28):
# the previous denylist version (blocking a handful of write tools + git/npm/
# pip/docker prefixes) was FAIL-OPEN — any tool not on the deny list passed
# through, including future mutating tools added to the surface. Rebuilt on
# `allow_names` so it FAILS CLOSED: only the explicitly allowed read-only
# tools pass. An allowlist is strict by construction (security_baseline.toml:
# "allowlists (frozenset) for identifiers").
_READ_ONLY_ALLOW = frozenset(
    {
        "read",
        "grep",
        "list",
        "tree",
        "search",
        "query",
        "get",
        "show",
        "inspect",
        "describe",
        "cat",
        "head",
        "tail",
        "view",
        "status",
        "fetch",
        "resolve",
        "validate",
        "lint",
        "check",
        "diff",
        "log",
        "help",
    }
)
READ_ONLY_PERMISSION_CONTEXT = ToolPermissionContext(allow_names=set(_READ_ONLY_ALLOW))
