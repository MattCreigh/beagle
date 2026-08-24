"""Wrap Beagle MCP tools as CrewAI-compatible BaseTool instances."""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger("Beagle.bridges.crewai.tools")


class BeagleCrewAITool:
    """Adapter that presents an Beagle MCP tool as a CrewAI BaseTool.

    CrewAI agents see standard tool interface. Internally, calls are
    routed through Beagle's MCP servers with Guardian approval and
    rate limiting.
    """

    name: str = ""
    description: str = ""

    def __init__(
        self,
        name: str,
        description: str,
        func: Any,
    ) -> None:
        self.name = name
        self.description = description
        self._func = func

    def _run(self, *args: Any, **kwargs: Any) -> str:
        """Execute the tool — CrewAI's call interface."""
        import asyncio

        # Guardian approval check
        try:
            from beagle.guardian import (
                ApprovalDecision,
                GuardianAction,
                RiskLevel,
                get_guardian,
            )

            guardian = get_guardian()
            action = GuardianAction(
                action_type="tool_call",
                description=f"CrewAI tool: {self.name}",
                details={"tool": self.name, "args": str(kwargs)[:200]},
                risk_level=RiskLevel.LOW,
            )
            result = guardian.check_approval(action)
            if result.decision == ApprovalDecision.DENIED:
                return f"Tool {self.name} denied by Guardian"
        except ImportError as exc:
            # A security control that cannot load must be loud. Logged at error,
            # not warning: the tool runs WITHOUT a Guardian approval check.
            logger.error(
                "Cannot import the Guardian approval check (%s); tool %r is executing "
                "with NO approval gate.",
                exc,
                self.name,
            )

        # Execute
        if asyncio.iscoroutinefunction(self._func):
            try:
                # v1.2.0 (RG-7, BGL-008): this is a synchronous boundary
                # (CrewAI's _run interface). The prior code called
                # asyncio.get_event_loop() and, when a loop was running,
                # scheduled the coroutine on that same loop via
                # run_coroutine_threadsafe then blocked on future.result() —
                # the loop could not advance, so the call hung for 60s.
                # asyncio.run() is the correct sync-boundary primitive.
                return str(asyncio.run(self._func(*args, **kwargs)))
            except Exception as e:  # ruff: ignore[BLE001]  # broad catch intentional
                return f"Tool error: {e}"
        else:
            return str(self._func(*args, **kwargs))


def wrap_mcp_tools_for_crewai() -> list[BeagleCrewAITool]:
    """Wrap all Beagle MCP tools as CrewAI tools.

    Returns tools from the utility server (code_search, web_search, etc.)
    and RAG server (rag_search) as CrewAI-compatible tools.
    """
    tools = []
    try:
        from beagle.infrastructure.mcp_rag_server import (
            rag_search,
        )

        tools.append(
            BeagleCrewAITool(
                name="rag_search",
                description="Search the codebase knowledge base using hybrid RAG",
                func=rag_search,
            )
        )
    except ImportError as exc:
        logger.warning(
            "Cannot import rag_search (%s); the CrewAI tool list is built without it.",
            exc,
        )

    # Add more tools as they become available from mcp_utility_server
    return tools
