"""End-to-end tests for the sub-agent runtime interface (B1d).

These exercise the :class:`beagle.runtime.base.AgentRuntime` contract
through the :class:`beagle.runtime.goose_cli.GooseCliRuntime` plugin. The
subprocess spawn is mocked so the tests run anywhere without a real goose
binary or a live LLM call, while still proving the spawn / send / stream /
terminate / health lifecycle end to end through the interface.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from beagle.runtime.base import AgentSpec, RuntimeHealth
from beagle.runtime.goose_cli import GooseCliRuntime


def _fake_process() -> MagicMock:
    """Return a MagicMock that stands in for asyncio.subprocess.Process."""
    proc = MagicMock()
    proc.returncode = None
    proc.stdout = AsyncMock()
    proc.stdout.readline = AsyncMock(side_effect=[b"line one\n", b"line two\n", b""])
    proc.stderr = AsyncMock()
    proc.stderr.readline = AsyncMock(side_effect=[b"", b""])
    proc.stdin = AsyncMock()
    proc.stdin.write = AsyncMock(return_value=5)
    proc.stdin.drain = AsyncMock()
    proc.wait = AsyncMock()
    proc.terminate = MagicMock()
    proc.kill = MagicMock()
    return proc


@pytest.fixture
def _clean_path() -> None:
    """Ensure no GOOSE_BIN override leaks into the binary resolution."""
    import os

    os.environ.pop("GOOSE_BIN", None)


def test_runtime_satisfies_protocol() -> None:
    """GooseCliRuntime is structurally a valid AgentRuntime."""
    from beagle.runtime.base import AgentRuntime

    assert isinstance(GooseCliRuntime(), AgentRuntime)


def test_resolved_binary_raises_when_unset(_clean_path: None) -> None:
    """An unset binary path is a RuntimeError, not a silent empty argv[0]."""
    with patch("beagle.runtime.goose_cli.resolve_goose_bin", return_value=""):
        runtime = GooseCliRuntime()
        with pytest.raises(RuntimeError):
            runtime.resolved_binary()


def test_health_check_reports_absent_binary(_clean_path: None) -> None:
    """health_check reports unhealthy (not raises) when the binary is absent."""
    with patch("beagle.runtime.goose_cli.resolve_goose_bin", return_value=""):
        runtime = GooseCliRuntime()
        health = asyncio.run(runtime.health_check())
        assert isinstance(health, RuntimeHealth)
        assert health.healthy is False
        assert "goose binary not configured" in health.detail


def test_spawn_builds_goose_command_and_returns_handle(_clean_path: None) -> None:
    """spawn resolves the binary and returns an AgentHandle for the process."""
    proc = _fake_process()

    async def _scenario() -> None:
        with (
            patch("beagle.runtime.goose_cli.resolve_goose_bin", return_value="/bin/echo"),
            patch("asyncio.create_subprocess_exec", new=AsyncMock(return_value=proc)),
        ):
            runtime = GooseCliRuntime()
            handle = await runtime.spawn(AgentSpec(name="test-agent", model="minimax-m3:cloud"))
            assert handle.agent_id == "test-agent"
            assert handle.runtime_name == "goose_cli"
            assert handle.process is proc

    asyncio.run(_scenario())


def test_send_message_round_trip(_clean_path: None) -> None:
    """send_message writes stdin and returns the streamed stdout."""
    proc = _fake_process()

    async def _scenario() -> None:
        with (
            patch("beagle.runtime.goose_cli.resolve_goose_bin", return_value="/bin/echo"),
            patch("asyncio.create_subprocess_exec", new=AsyncMock(return_value=proc)),
        ):
            runtime = GooseCliRuntime()
            handle = await runtime.spawn(AgentSpec(name="test-agent", model="minimax-m3:cloud"))
            # stream() drains the two stdout lines through the interface.
            chunks = [c async for c in runtime.stream(handle, "hello")]
            assert any("line one" in c for c in chunks)
            assert any("line two" in c for c in chunks)

    asyncio.run(_scenario())


def test_terminate_cleans_up_running_process(_clean_path: None) -> None:
    """terminate sends SIGTERM and waits for the process."""
    proc = _fake_process()
    proc.returncode = None

    async def _scenario() -> None:
        with (
            patch("beagle.runtime.goose_cli.resolve_goose_bin", return_value="/bin/echo"),
            patch("asyncio.create_subprocess_exec", new=AsyncMock(return_value=proc)),
        ):
            runtime = GooseCliRuntime()
            handle = await runtime.spawn(AgentSpec(name="test-agent", model="minimax-m3:cloud"))
            await runtime.terminate(handle)
            proc.terminate.assert_called_once()
            proc.wait.assert_awaited()

    asyncio.run(_scenario())


def test_default_goose_binary_factory_non_raising(_clean_path: None) -> None:
    """The schema default_factory returns a string, never raises."""
    from beagle.runtime.goose_cli import default_goose_binary

    with patch("beagle.runtime.goose_cli.resolve_goose_bin", return_value=""):
        assert default_goose_binary() == ""
    with patch("beagle.runtime.goose_cli.resolve_goose_bin", return_value="/bin/goose"):
        assert default_goose_binary() == "/bin/goose"
