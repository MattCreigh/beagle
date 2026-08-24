"""End-to-end tests for the HTTPAgentRuntime (B3).

These exercise the :class:`beagle.runtime.http_agent.HTTPAgentRuntime`
against the bundled :class:`EchoAgent` reference agent. The A2A client
bridge is stubbed so the tests run without a live remote server while still
proving the spawn / send_message / stream / health lifecycle and the
``http_agent`` plugin selection.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, patch

import pytest

from beagle.runtime.base import AgentRuntime, AgentSpec
from beagle.runtime.http_agent import EchoAgent, HTTPAgentRuntime


@pytest.fixture
def echo_agent() -> EchoAgent:
    """A local EchoAgent reference instance."""
    return EchoAgent()


def test_http_agent_satisfies_protocol() -> None:
    """HTTPAgentRuntime is structurally a valid AgentRuntime."""
    assert isinstance(HTTPAgentRuntime(), AgentRuntime)


def test_echo_agent_round_trip(echo_agent: EchoAgent) -> None:
    """The reference agent echoes the query and marks health."""
    result = asyncio.run(echo_agent.handle({"query": "analyze auth"}))
    assert result["status"] == "completed"
    assert result["output"]["text"] == "echo: analyze auth"
    assert result["output"]["health"] == "ok"
    health = asyncio.run(echo_agent.health())
    assert health["status"] == "healthy"


def test_http_runtime_spawn_requires_url() -> None:
    """spawn raises RuntimeError when no A2A agent URL is configured."""

    async def _scenario() -> None:
        with patch("beagle.runtime.http_agent.get_a2a_config") as mock_cfg:
            cfg = mock_cfg.return_value
            cfg.remote_agents = {}
            runtime = HTTPAgentRuntime()
            with pytest.raises(RuntimeError):
                await runtime.spawn(AgentSpec(name="echo"))

    asyncio.run(_scenario())


def test_http_runtime_send_message_via_reference(echo_agent: EchoAgent) -> None:
    """send_message dispatches to the remote agent and returns its output."""

    async def _fake_call(**kwargs):
        # Route through the actual reference agent to prove the contract.
        # The mock passes agent_url/agent_name keywords; read task_input.
        task_input = kwargs["task_input"]
        result = await echo_agent.handle(task_input)
        return result

    async def _scenario() -> None:
        with (
            patch("beagle.runtime.http_agent.get_a2a_config") as mock_cfg,
            patch(
                "beagle.runtime.http_agent.A2AClientBridge.call_remote_agent",
                new=AsyncMock(side_effect=_fake_call),
            ),
        ):
            cfg = mock_cfg.return_value
            cfg.remote_agents = {"echo": "http://localhost:8420/a2a"}
            runtime = HTTPAgentRuntime()
            handle = await runtime.spawn(AgentSpec(name="echo"))
            reply = await runtime.send_message(handle, "hello world")
            assert reply == "echo: hello world"

    asyncio.run(_scenario())


def test_http_runtime_stream_yields_reply(echo_agent: EchoAgent) -> None:
    """stream yields the remote agent's reply as a single chunk."""

    async def _fake_call(**kwargs):
        task_input = kwargs["task_input"]
        return await echo_agent.handle(task_input)

    async def _scenario() -> None:
        with (
            patch("beagle.runtime.http_agent.get_a2a_config") as mock_cfg,
            patch(
                "beagle.runtime.http_agent.A2AClientBridge.call_remote_agent",
                new=AsyncMock(side_effect=_fake_call),
            ),
        ):
            cfg = mock_cfg.return_value
            cfg.remote_agents = {"echo": "http://localhost:8420/a2a"}
            runtime = HTTPAgentRuntime()
            handle = await runtime.spawn(AgentSpec(name="echo"))
            chunks = [c async for c in runtime.stream(handle, "stream me")]
            assert chunks == ["echo: stream me"]

    asyncio.run(_scenario())


def test_http_runtime_health_configured() -> None:
    """health_check reports healthy when an agent URL is configured."""

    async def _scenario() -> None:
        with patch("beagle.runtime.http_agent.get_a2a_config") as mock_cfg:
            cfg = mock_cfg.return_value
            cfg.remote_agents = {"echo": "http://localhost:8420/a2a"}
            runtime = HTTPAgentRuntime()
            health = await runtime.health_check()
            assert health.healthy is True
            assert "localhost:8420" in health.detail

    asyncio.run(_scenario())


def test_http_runtime_health_unconfigured() -> None:
    """health_check reports unhealthy (not raises) when no URL is set."""

    async def _scenario() -> None:
        with patch("beagle.runtime.http_agent.get_a2a_config") as mock_cfg:
            cfg = mock_cfg.return_value
            cfg.remote_agents = {}
            runtime = HTTPAgentRuntime()
            health = await runtime.health_check()
            assert health.healthy is False

    asyncio.run(_scenario())
