"""The ``http_agent`` sub-agent execution runtime (over A2A).

This runtime satisfies :class:`beagle.runtime.base.AgentRuntime` by driving
a remote A2A agent over HTTP, using the existing signed A2A client in
``beagle/bridges/a2a_client.py``. It is axis 2 only: it replaces the
sub-agent EXECUTION layer, not the user-facing front end.

Each ``spawn`` maps a spec to an A2A remote agent URL and returns a handle;
``send_message`` / ``stream`` dispatch the task to the remote agent via the
signed ``call_remote_agent`` bridge. There is no persistent subprocess, so
``terminate`` is a no-op and ``health_check`` reports whether a remote
agent URL is configured.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any

from beagle.bridges.a2a_client import A2AClientBridge, get_a2a_config
from beagle.runtime.base import AgentHandle, AgentRuntime, AgentSpec, RuntimeHealth


@dataclass
class HTTPAgentRuntime(AgentRuntime):
    """Drive remote A2A sub-agents over HTTP.

    Attributes:
        name: Stable runtime name, ``http_agent``.
        default_agent_url: URL of the remote A2A server. Defaults to the
            first entry in ``A2ABridgeConfig.remote_agents`` when available.

    """

    name: str = "http_agent"
    default_agent_url: str = ""

    def __post_init__(self) -> None:
        """Resolve the default agent URL from A2A config when unset."""
        if not self.default_agent_url:
            cfg = get_a2a_config()
            if cfg.remote_agents:
                self.default_agent_url = next(iter(cfg.remote_agents.values()))
        self._client = A2AClientBridge()

    def _url_for_spec(self, spec: AgentSpec) -> str:
        """Pick the A2A URL for a spec.

        Args:
            spec: The agent spec.

        Returns:
            The remote A2A base URL.

        Raises:
            RuntimeError: When no agent URL is configured.

        """
        url = spec.environment.get("A2A_AGENT_URL") or self.default_agent_url
        if not url:
            raise RuntimeError(
                "no A2A agent URL configured; set [runtime].plugin = 'http_agent' "
                "with a remote agent in config or pass A2A_AGENT_URL in the spec env"
            )
        return url

    async def spawn(self, spec: AgentSpec) -> AgentHandle:
        """Create a handle for a remote A2A agent.

        A2A is stateless per call; the handle carries the resolved URL and
        the spec so ``send_message`` / ``stream`` can dispatch to it.

        Args:
            spec: The agent description.

        Returns:
            An :class:`AgentHandle`.

        Raises:
            RuntimeError: When no agent URL is configured.

        """
        url = self._url_for_spec(spec)
        return AgentHandle(
            agent_id=spec.name,
            runtime_name=self.name,
            process={"url": url, "spec": spec},
        )

    async def send_message(self, handle: AgentHandle, message: str) -> str:
        """Dispatch a task to the remote A2A agent and return the reply.

        Args:
            handle: Handle returned by :meth:`spawn`.
            message: The prompt payload.

        Returns:
            The remote agent's reply text.

        """
        if handle.process is None:
            raise RuntimeError("agent handle has no process payload")
        process: dict[str, Any] = handle.process
        url = process["url"]
        spec: AgentSpec = process["spec"]
        result = await self._client.call_remote_agent(
            agent_url=url,
            agent_name=spec.name,
            task_input={"query": message},
        )
        if result.get("status") == "failed":
            raise RuntimeError(f"A2A remote agent {spec.name!r} failed: {result.get('error')}")
        output = result.get("output")
        if isinstance(output, dict):
            return str(output.get("text", output))
        return str(output or "")

    async def stream(self, handle: AgentHandle, message: str) -> AsyncIterator[str]:
        """Stream a remote A2A agent's reply.

        The A2A bridge is request/response; the whole reply is yielded once.

        Args:
            handle: Handle returned by :meth:`spawn`.
            message: The prompt payload.

        Yields:
            The remote agent's reply text.

        """
        reply = await self.send_message(handle, message)
        if reply:
            yield reply

    async def terminate(self, handle: AgentHandle) -> None:
        """No-op: an A2A remote call has no persistent process.

        Args:
            handle: Unused; kept for the protocol contract.

        """
        return None

    async def health_check(self) -> RuntimeHealth:
        """Report whether an A2A agent URL is configured.

        Returns:
            A :class:`RuntimeHealth` snapshot.

        """
        if self.default_agent_url:
            return RuntimeHealth(
                healthy=True,
                detail=f"A2A remote agent configured at {self.default_agent_url}",
                binary_path=self.default_agent_url,
            )
        return RuntimeHealth(
            healthy=False,
            detail="no A2A remote agent configured",
        )


def http_agent_factory() -> HTTPAgentRuntime:
    """Entry-point factory for the ``http_agent`` runtime plugin.

    Returns:
        A configured :class:`HTTPAgentRuntime` instance.

    """
    return HTTPAgentRuntime()


# ── Minimal reference agent for local tests ────────────────────────────────


@dataclass
class EchoAgent:
    """A minimal reference A2A agent for local tests.

    Responds with an echo of the query (plus a health marker), so a workflow
    can be run end to end against ``http_agent`` without a real remote.
    """

    agent_name: str = "echo"
    _log: list[dict[str, Any]] = field(default_factory=list)

    async def handle(self, task_input: dict[str, Any]) -> dict[str, Any]:
        """Handle an A2A execute request.

        Args:
            task_input: The task input dict (expected to carry ``query``).

        Returns:
            A result dict with ``status`` and ``output``.

        """
        query = str(task_input.get("query", ""))
        self._log.append({"query": query})
        return {
            "status": "completed",
            "output": {"text": f"echo: {query}", "health": "ok"},
        }

    async def health(self) -> dict[str, Any]:
        """Report health.

        Returns:
            A simple health dict.

        """
        return {"status": "healthy", "agent": self.agent_name}
