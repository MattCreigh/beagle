"""The sub-agent execution runtime protocol and its value types.

This module is axis 2 only. It defines the interface that any sub-agent
execution backend must satisfy so Beagle can swap ``goose_cli`` (a local
``goose`` subprocess) for ``http_agent`` (an A2A remote) without touching
the orchestrator. It deliberately does NOT model the user-facing front
end (goose CLI, pi, OpenClaw), which is a different replaceability axis.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable


@dataclass(frozen=True)
class AgentSpec:
    """Static description of an agent to spawn.

    Attributes:
        name: Stable identifier for the agent (e.g. ``sota-dev``).
        role: The role preset to apply (e.g. ``default``, ``coding``).
        model: Model identifier, or ``None`` to use the role default.
        system_prompt: The compiled system prompt / doctrine payload.
        environment: Extra environment variables for the runtime.

    """

    name: str
    role: str = "default"
    model: str | None = None
    system_prompt: str = ""
    environment: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class AgentHandle:
    """A live handle to a spawned sub-agent.

    Attributes:
        agent_id: Runtime-local identifier for the spawned instance.
        runtime_name: Name of the runtime that owns the handle
            (e.g. ``goose_cli``).
        process: Runtime-specific process/connection object, if any.

    """

    agent_id: str
    runtime_name: str
    process: Any | None = None


@dataclass(frozen=True)
class RuntimeHealth:
    """Health snapshot for a runtime.

    Attributes:
        healthy: Whether the runtime is usable.
        detail: Human-readable summary of the runtime's state.
        binary_path: Resolved binary the runtime would spawn, if known.

    """

    healthy: bool
    detail: str = ""
    binary_path: str = ""


@runtime_checkable
class AgentRuntime(Protocol):
    """Protocol for a sub-agent execution backend.

    A runtime is responsible for spawning a sub-agent (as a subprocess or
    via a remote protocol), exchanging messages with it, streaming its
    output, terminating it, and reporting its own health.

    Implementations SHOULD be importable with no side effects beyond
    constructing value objects, so a machine without the runtime's
    underlying binary can still import the module and defer the failure
    to spawn time.
    """

    name: str

    async def spawn(self, _spec: AgentSpec) -> AgentHandle:
        """Spawn a sub-agent from a spec.

        Args:
            _spec: The agent description to spawn.

        Returns:
            A handle to the spawned agent.

        Raises:
            RuntimeError: When the runtime cannot start the agent (e.g.
                the binary is missing or lacks execute permission).

        """
        ...  # protocol: implemented by concrete runtimes

    async def send_message(self, _handle: AgentHandle, _message: str) -> str:
        """Send a message to a running sub-agent.

        Args:
            _handle: Handle returned by :meth:`spawn`.
            _message: The message / prompt payload to send.

        Returns:
            The sub-agent's reply text.

        """
        ...  # protocol: implemented by concrete runtimes

    def stream(self, _handle: AgentHandle, _message: str) -> AsyncIterator[str]:
        """Stream a sub-agent's incremental output for a message.

        Args:
            _handle: Handle returned by :meth:`spawn`.
            _message: The message / prompt payload to send.

        Yields:
            Incremental text chunks as they arrive.

        Raises:
            StopAsyncIteration: When the stream ends.

        """
        ...  # protocol: implemented by concrete runtimes

    async def terminate(self, _handle: AgentHandle) -> None:
        """Terminate a running sub-agent and release its resources.

        Args:
            _handle: Handle returned by :meth:`spawn`.

        """
        ...  # protocol: implemented by concrete runtimes

    async def health_check(self) -> RuntimeHealth:
        """Report the runtime's own health.

        Returns:
            A :class:`RuntimeHealth` snapshot.

        """
