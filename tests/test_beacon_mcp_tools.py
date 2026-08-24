# Copyright (c) 2026 Matt Creigh. All rights reserved.
"""Tests for beagle.infrastructure.mcp_coord_server — the beagle-coord MCP surface.

See plans/beagle-beacon-coordination.xml WP-7: decision D-08, hard
constraint C-02.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from beagle.infrastructure import mcp_coord_server as m


@dataclass
class _FakeCoordConfig:
    enabled: bool = True
    channels_enabled: bool = True


@dataclass
class _FakeConfig:
    coord: _FakeCoordConfig = field(default_factory=_FakeCoordConfig)


class TestToolSurfaceIsFrozen:
    """D-08: adding a tool is a scope change, not an improvement."""

    def test_exactly_fourteen_coord_tools(self) -> None:
        names = sorted(t for t in dir(m) if t.startswith("coord_"))
        assert len(names) == 14, f"D-08 fixes the surface at 14 tools, found {len(names)}: {names}"

    def test_no_tcp_construct_in_this_module(self) -> None:
        """C-01 applies here too — the coord server must not open a TCP socket."""
        text = Path(m.__file__).read_text()
        pattern = re.compile(r"AF_INET|TcpFakeServer|127\.0\.0\.1|bind\(\(")
        assert not pattern.search(text)


class TestDisabledIsANoOp:
    """C-02: Beacon must never become a hard requirement of an existing session."""

    @pytest.fixture(autouse=True)
    def _disable_coord(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setattr(
            m, "get_config", lambda: _FakeConfig(coord=_FakeCoordConfig(enabled=False))
        )

    @pytest.mark.parametrize(
        "coro_factory",
        [
            lambda: m.coord_attach(workdir="/tmp/x"),
            lambda: m.coord_detach(),
            lambda: m.coord_heartbeat(),
            lambda: m.coord_list_agents(),
            lambda: m.coord_agent_info(agent_id="x"),
            lambda: m.coord_whoami(),
            lambda: m.coord_lock_file(path="a.py"),
            lambda: m.coord_unlock_file(path="a.py"),
            lambda: m.coord_register_plan(plan_id="p", status="active"),
            lambda: m.coord_activity(),
            lambda: m.coord_open_channel(peer_id="x"),
            lambda: m.coord_accept_channel(),
            lambda: m.coord_close_channel(channel_id="x"),
            lambda: m.coord_list_channels(),
        ],
    )
    @pytest.mark.asyncio
    async def test_every_tool_is_disabled_and_raises_nothing(self, coro_factory) -> None:
        result = await coro_factory()
        assert json.loads(result) == {"status": "disabled"}


class TestChannelToolsRespectChannelsEnabled:
    """The four rendezvous tools additionally gate on channels_enabled."""

    @pytest.fixture(autouse=True)
    def _coord_on_channels_off(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setattr(
            m,
            "get_config",
            lambda: _FakeConfig(coord=_FakeCoordConfig(enabled=True, channels_enabled=False)),
        )

    @pytest.mark.parametrize(
        "coro_factory",
        [
            lambda: m.coord_open_channel(peer_id="x"),
            lambda: m.coord_accept_channel(),
            lambda: m.coord_close_channel(channel_id="x"),
            lambda: m.coord_list_channels(),
        ],
    )
    @pytest.mark.asyncio
    async def test_channel_tool_disabled_when_channels_disabled(self, coro_factory) -> None:
        result = await coro_factory()
        assert json.loads(result) == {"status": "disabled"}

    @pytest.mark.asyncio
    async def test_non_channel_tool_still_works_when_channels_disabled(self) -> None:
        result = await m.coord_activity()
        payload = json.loads(result)
        # not attached in this test, but the point is it's NOT {"status":"disabled"}
        assert payload != {"status": "disabled"}


class TestFullLifecycle:
    """A real attach -> heartbeat -> lock -> plan -> detach flow."""

    @pytest.fixture(autouse=True)
    def _enabled(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
        monkeypatch.setattr(m, "get_config", lambda: _FakeConfig())
        m._session = None
        m._paths = None
        yield
        if m._session is not None:
            m._session.detach()
            m._session.close()
            m._session = None
            m._paths = None

    @pytest.mark.asyncio
    async def test_attach_heartbeat_lock_plan_activity_detach(self, tmp_path: Path) -> None:
        attach_result = json.loads(await m.coord_attach(workdir=str(tmp_path), model="test-model"))
        assert "agent_id" in attach_result

        heartbeat_result = json.loads(await m.coord_heartbeat(phase="writing"))
        assert heartbeat_result["status"] == "ok"

        whoami = json.loads(await m.coord_whoami())
        assert whoami["agent_id"] == attach_result["agent_id"]

        roster = json.loads(await m.coord_list_agents())
        assert any(a["agent_id"] == attach_result["agent_id"] for a in roster["agents"])

        lock_result = json.loads(await m.coord_lock_file(path="shared.py"))
        assert lock_result["ok"] is True

        unlock_result = json.loads(await m.coord_unlock_file(path="shared.py"))
        assert unlock_result["status"] == "ok"

        plan_result = json.loads(
            await m.coord_register_plan(plan_id="beacon-coordination", status="in-flight")
        )
        assert plan_result["status"] == "ok"

        activity_result = json.loads(await m.coord_activity())
        assert "events" in activity_result

        detach_result = json.loads(await m.coord_detach())
        assert detach_result["status"] == "ok"

    @pytest.mark.asyncio
    async def test_agent_info_for_unknown_agent_is_a_clean_error(self, tmp_path: Path) -> None:
        await m.coord_attach(workdir=str(tmp_path))
        result = json.loads(await m.coord_agent_info(agent_id="nonexistent-agent"))
        assert result["status"] == "error"


class TestChannelLifecycleThroughTools:
    """coord_open_channel / coord_accept_channel / coord_close_channel / coord_list_channels."""

    @pytest.fixture(autouse=True)
    def _enabled(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setattr(m, "get_config", lambda: _FakeConfig())
        yield

    @pytest.mark.asyncio
    async def test_open_to_unreachable_peer_returns_reason_not_a_path(self, tmp_path: Path) -> None:
        await m.coord_attach(workdir=str(tmp_path))
        try:
            result = json.loads(await m.coord_open_channel(peer_id="nobody-home", kind="handoff"))
            assert result.get("unreachable") is True
            assert "reason" in result
        finally:
            await m.coord_detach()

    @pytest.mark.asyncio
    async def test_list_channels_empty_when_none_opened(self, tmp_path: Path) -> None:
        await m.coord_attach(workdir=str(tmp_path))
        try:
            result = json.loads(await m.coord_list_channels())
            assert result["channels"] == []
        finally:
            await m.coord_detach()

    @pytest.mark.asyncio
    async def test_close_nonexistent_channel_is_false_not_an_error(self, tmp_path: Path) -> None:
        await m.coord_attach(workdir=str(tmp_path))
        try:
            result = json.loads(await m.coord_close_channel(channel_id="never-existed"))
            assert result["closed"] is False
        finally:
            await m.coord_detach()
