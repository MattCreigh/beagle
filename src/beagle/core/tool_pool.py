"""Unified Tool Pool for Beagle workflow execution.

Centralizes tool discovery, assembly, and permission-based filtering.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from ..permission_context import DEFAULT_PERMISSION_CONTEXT, ToolPermissionContext

logger = logging.getLogger("Beagle.tools")


@dataclass(frozen=True)
class ToolDefinition:
    """Metadata for an available tool."""

    name: str
    description: str
    parameters: dict[str, Any]
    source: str  # "builtin", "plugin", "skill", "mcp"


@dataclass(frozen=True)
class ToolPool:
    """Collection of assembled tools with filtering state.

    Inspired by claw-code ToolPool.
    """

    tools: dict[str, ToolDefinition]
    simple_mode: bool
    include_mcp: bool
    permission_context: ToolPermissionContext

    def get_allowed_tools(self) -> list[ToolDefinition]:
        """Return tools not blocked by the permission context."""
        return [t for t in self.tools.values() if not self.permission_context.blocks(t.name)]


async def assemble_tool_pool(
    simple_mode: bool = False,
    include_mcp: bool = True,
    permission_context: ToolPermissionContext | None = None,
) -> ToolPool:
    """Discover and filter available tools.

    Args:
        simple_mode: If True, only include essential builtin tools.
        include_mcp: If True, include tools discovered via MCP servers.
        permission_context: Optional security context for filtering.

    Returns:
        Assembled ToolPool

    """
    logger.info(f"Assembling tool pool (simple={simple_mode}, mcp={include_mcp})")

    # 1. Builtin Tools
    tools = _load_builtin_tools(simple_mode)

    # 2. Skill-based Tools
    if not simple_mode:
        tools.update(_load_skill_tools())

    # 3. MCP Tools
    if include_mcp:
        mcp_tools = await _discover_mcp_tools()
        tools.update(mcp_tools)

    return ToolPool(
        tools=tools,
        simple_mode=simple_mode,
        include_mcp=include_mcp,
        permission_context=permission_context or DEFAULT_PERMISSION_CONTEXT,
    )


def _load_builtin_tools(_simple: bool) -> dict[str, ToolDefinition]:
    """Mock for loading builtin tools."""
    # In full implementation, this would return real tool specs
    return {
        "read_file": ToolDefinition("read_file", "Read file content", {}, "builtin"),
        "grep_search": ToolDefinition("grep_search", "Search file content", {}, "builtin"),
    }


def _load_skill_tools() -> dict[str, ToolDefinition]:
    """Mock for loading skill-based tools."""
    return {}


async def _discover_mcp_tools() -> dict[str, ToolDefinition]:
    """Mock for MCP tool discovery."""
    return {}
