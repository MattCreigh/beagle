"""Runtime package: the sub-agent execution abstraction.

This is axis 2 of Beagle's replaceability model (constraint C2 in
``plans/beagle-supplementary-pois.xml``). It governs how Beagle spawns
and talks to SUB-AGENT EXECUTION runtimes — ``goose_cli`` (the default,
spawning a ``goose`` subprocess) and ``http_agent`` (an A2A remote). It
does NOT model the user-facing FRONT END (goose CLI, pi, OpenClaw), which
is axis 1 and lives outside this package.
"""

from beagle.runtime.base import (
    AgentHandle,
    AgentRuntime,
    AgentSpec,
    RuntimeHealth,
)
from beagle.runtime.http_agent import HTTPAgentRuntime

__all__ = [
    "AgentHandle",
    "AgentRuntime",
    "AgentSpec",
    "HTTPAgentRuntime",
    "RuntimeHealth",
]
