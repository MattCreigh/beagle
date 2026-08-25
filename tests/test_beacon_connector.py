# Copyright (c) 2026 Matt Creigh. All rights reserved.
"""Tests for beagle.beacon.connector — the agent-side ring/socket router.

See plans/beagle-beacon-coordination.xml WP-5, decisions D-04 and D-06.
"""

from __future__ import annotations

import threading
import uuid
from datetime import UTC, datetime
from pathlib import Path

import pytest
import redis

from beagle.beacon.connector import CoordSession, EphemeralRingConnector
from beagle.beacon.keys import filehash
from beagle.beacon.records import AgentRecord, stable_colour
from beagle.beacon.server import BeaconServer
from beagle.beacon.spawn import is_live


def _make_record(agent_id: str) -> AgentRecord:
    now = datetime.now(UTC).isoformat()
    return AgentRecord(
        agent_id=agent_id,
        session_id=agent_id,
        pid=1,
        uid=1,
        host="test",
        connected_at=now,
        last_seen=now,
        model="test-model",
        phase="testing",
        current_plan="",
        current_work="",
        files=(),
        colour=stable_colour(agent_id),
    )


@pytest.fixture
def running_server(tmp_path: Path):
    server = BeaconServer(tmp_path)
    server.start()
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    assert is_live(server.paths, connect_timeout_s=2.0)
    yield server
    server.stop()
    thread.join(timeout=5)


def _has_orpheus() -> bool:
    try:
        import orpheus  # noqa: F401
        return True
    except ImportError:
        return False


# Ring fast-path tests exercise the optional proprietary orpheus transport;
# they skip when the wheel is absent. Socket-fallback tests below still run.
_ring_skip = pytest.mark.skipif(
    not _has_orpheus(), reason="orpheus (proprietary ring transport) not installed"
)


@_ring_skip
class TestAttachDetach:
    def test_attach_registers_record_and_creates_ring_file(
        self, running_server: BeaconServer
    ) -> None:
        agent_id = str(uuid.uuid4())
        session = CoordSession(running_server.paths, agent_id)
        session.attach(_make_record(agent_id))

        whoami = session.whoami()
        assert whoami is not None
        assert whoami.agent_id == agent_id

        ring_path = running_server.paths.agent_ring_path(agent_id, direction="in")
        assert ring_path.exists()

        session.detach()
        session.close()

    def test_detach_unlinks_the_agents_ring_file(self, running_server: BeaconServer) -> None:
        agent_id = str(uuid.uuid4())
        session = CoordSession(running_server.paths, agent_id)
        session.attach(_make_record(agent_id))
        ring_path = running_server.paths.agent_ring_path(agent_id, direction="in")
        assert ring_path.exists()

        session.detach()

        assert not ring_path.exists()
        session.close()

    def test_beacon_unlinks_ring_of_an_agent_whose_lease_expired(
        self, running_server: BeaconServer
    ) -> None:
        """A crash (no clean detach) must not leak the ring file forever."""
        agent_id = str(uuid.uuid4())
        session = CoordSession(running_server.paths, agent_id, agent_ttl_s=15)
        session.attach(_make_record(agent_id))
        ring_path = running_server.paths.agent_ring_path(agent_id, direction="in")
        assert ring_path.exists()

        # Simulate a crash: the agent vanishes without calling detach(). The
        # server-side crash-cleanup path is the ring poller's detach(), which
        # is what a lease-expiry sweep (WP-6/WP-9's GC) would call. Exercise
        # that path directly here, since the sweep itself is a later package.
        running_server.poller.detach(agent_id)

        assert not ring_path.exists()
        session.close()


@_ring_skip
class TestRingFastPath:
    def test_heartbeat_goes_over_the_ring(self, running_server: BeaconServer) -> None:
        agent_id = str(uuid.uuid4())
        session = CoordSession(running_server.paths, agent_id)
        session.attach(_make_record(agent_id))

        wrote = session._ring.write("heartbeat", {"phase": "writing", "agent_ttl_s": 15})
        assert wrote is True

        session.detach()
        session.close()


class TestSocketFallback:
    """D-06: when a connector's ring is unavailable, every op still succeeds
    over the socket and produces the same store state as the ring path
    would have. orpheus itself is a required dependency (operator override,
    2026-08-21) — this exercises the per-connector fallback (e.g. the ring
    file vanished from under an established connector), not "orpheus is not
    installed", which is no longer a supported scenario."""

    def test_every_op_succeeds_with_ring_unavailable(self, running_server: BeaconServer) -> None:
        agent_id = str(uuid.uuid4())
        session = CoordSession(running_server.paths, agent_id)
        session.attach(_make_record(agent_id))
        session._ring._ring = None  # simulate the connector's ring going away
        assert session._ring.available is False

        session.heartbeat(phase="writing")
        session.event(action="edit", path="a.py")

        raw = redis.Redis(
            unix_socket_path=str(running_server.paths.socket_path), decode_responses=True
        )
        assert raw.sismember("agent:list", agent_id)
        assert raw.hget(f"agent:{agent_id}", "phase") == "writing"
        events = raw.lrange("event", 0, -1)
        assert any('"action":"edit"' in e for e in events)

        lock_result = session.lock_file("shared/file.py")
        assert lock_result.ok is True

        session.unlock_file("shared/file.py")
        assert raw.get(f"lock:{filehash('shared/file.py')}") is None

        agents = session.list_agents()
        assert any(a.agent_id == agent_id for a in agents)

        raw.close()
        session.detach()
        session.close()

    def test_ring_write_falls_back_when_ring_reports_full(
        self, running_server: BeaconServer
    ) -> None:
        """A ring that returns None from reserve() must not drop the op."""
        agent_id = str(uuid.uuid4())
        session = CoordSession(running_server.paths, agent_id)
        session.attach(_make_record(agent_id))

        class _FullRing:
            def reserve(self):
                return None

        session._ring._ring = _FullRing()

        session.heartbeat(phase="fallback-path")

        raw = redis.Redis(
            unix_socket_path=str(running_server.paths.socket_path), decode_responses=True
        )
        assert raw.hget(f"agent:{agent_id}", "phase") == "fallback-path"
        raw.close()
        session.detach()
        session.close()


class TestLockFileSemantics:
    def test_second_agent_cannot_acquire_a_held_lock(self, running_server: BeaconServer) -> None:
        a = CoordSession(running_server.paths, str(uuid.uuid4()))
        b = CoordSession(running_server.paths, str(uuid.uuid4()))
        a.attach(_make_record(a.agent_id))
        b.attach(_make_record(b.agent_id))

        r1 = a.lock_file("contested.py")
        r2 = b.lock_file("contested.py")

        assert r1.ok is True
        assert r2.ok is False
        assert r2.holder == a.agent_id

        a.detach()
        b.detach()
        a.close()
        b.close()


class TestEphemeralRingConnectorDirectly:
    def test_write_returns_false_when_never_attached(self, tmp_path: Path) -> None:
        from beagle.beacon.keys import resolve_paths

        paths = resolve_paths(tmp_path)
        connector = EphemeralRingConnector(paths, "unattached-agent")
        assert connector.write("heartbeat", {"agent_ttl_s": 15}) is False
