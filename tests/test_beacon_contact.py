# Copyright (c) 2026 Matt Creigh. All rights reserved.
"""Tests for beagle.beacon.contact — pairwise peer rendezvous channels.

See plans/beagle-beacon-coordination.xml WP-5B: decisions D-09/D-10/D-11,
invariants I-4/I-5, measured facts M-11/M-12.
"""

from __future__ import annotations

import multiprocessing
import threading
import time
import uuid
from datetime import UTC, datetime
from pathlib import Path

import pytest
import redis

from beagle.beacon.connector import CoordSession
from beagle.beacon.contact import Unreachable, open_channel, sweep_channels
from beagle.beacon.keys import resolve_paths
from beagle.beacon.records import AgentRecord, stable_colour
from beagle.beacon.server import BeaconServer


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
    from beagle.beacon.spawn import is_live

    assert is_live(server.paths, connect_timeout_s=2.0)
    yield server
    server.stop()
    thread.join(timeout=5)


def _opener_proc(
    workdir: str,
    agent_id: str,
    peer_id: str,
    ready: multiprocessing.Event,
    attached: multiprocessing.Event,
    result: multiprocessing.Queue,
) -> None:
    paths = resolve_paths(workdir)
    session = CoordSession(paths, agent_id)
    session.attach(_make_record(agent_id))
    ready.set()
    attached.wait(timeout=5)

    channel = session.open_channel(peer_id, "handoff")
    if isinstance(channel, Unreachable):
        result.put({"role": "opener", "error": channel.reason})
        return

    session.send_on_channel(channel.a2b_path, b"hello from A")

    gate = threading.Event()
    deadline = time.monotonic() + 3.0
    reply = []
    while time.monotonic() < deadline and not reply:
        reply = session.read_channel(channel.b2a_path)
        if not reply:
            gate.wait(0.01)

    result.put({"role": "opener", "reply": reply[0] if reply else None})
    session.detach()
    session.close()


def _callee_proc(
    workdir: str,
    agent_id: str,
    ready: multiprocessing.Event,
    attached: multiprocessing.Event,
    result: multiprocessing.Queue,
) -> None:
    paths = resolve_paths(workdir)
    session = CoordSession(paths, agent_id)
    session.attach(_make_record(agent_id))
    ready.set()
    attached.wait(timeout=5)

    gate = threading.Event()
    deadline = time.monotonic() + 3.0
    offers = []
    while time.monotonic() < deadline and not offers:
        offers = session.poll_offers()
        if not offers:
            gate.wait(0.01)

    if not offers:
        result.put({"role": "callee", "error": "no offer received"})
        return

    offer = offers[0]
    incoming = []
    deadline = time.monotonic() + 3.0
    while time.monotonic() < deadline and not incoming:
        incoming = session.read_channel(offer["a2b_path"])
        if not incoming:
            gate.wait(0.01)

    session.send_on_channel(offer["b2a_path"], b"hello from B")

    result.put({"role": "callee", "received": incoming[0] if incoming else None})
    session.detach()
    session.close()


class TestTwoProcessRendezvous:
    """The recipe's own stop condition: a REAL two-process test, not a mock."""

    def test_channel_opens_and_messages_arrive_both_directions(
        self, running_server: BeaconServer
    ) -> None:
        agent_a = str(uuid.uuid4())
        agent_b = str(uuid.uuid4())

        ready_a: multiprocessing.Event = multiprocessing.Event()
        ready_b: multiprocessing.Event = multiprocessing.Event()
        attached_a: multiprocessing.Event = multiprocessing.Event()
        attached_b: multiprocessing.Event = multiprocessing.Event()
        results: multiprocessing.Queue = multiprocessing.Queue()

        real_workdir = self._workdir

        p_a = multiprocessing.Process(
            target=_opener_proc,
            args=(real_workdir, agent_a, agent_b, ready_a, attached_a, results),
        )
        p_b = multiprocessing.Process(
            target=_callee_proc,
            args=(real_workdir, agent_b, ready_b, attached_b, results),
        )
        p_b.start()
        p_a.start()

        assert ready_a.wait(timeout=5)
        assert ready_b.wait(timeout=5)

        running_server.poller.attach(agent_a)
        running_server.poller.attach(agent_b)

        # Populating agent:<id>.inbox_ring is coord_attach's job (WP-7, not
        # yet built) — until then, an integration test completes it by hand,
        # same as it already does for poller.attach() itself.
        client = redis.Redis(
            unix_socket_path=str(running_server.paths.socket_path), decode_responses=True
        )
        out_path = running_server.paths.agent_ring_path(agent_b, direction="out")
        client.hset(f"agent:{agent_b}", "inbox_ring", str(out_path))
        client.close()

        attached_a.set()
        attached_b.set()

        p_a.join(timeout=10)
        p_b.join(timeout=10)

        outcomes = {}
        for _ in range(2):
            r = results.get(timeout=5)
            outcomes[r["role"]] = r

        assert "error" not in outcomes["opener"], outcomes["opener"]
        assert "error" not in outcomes["callee"], outcomes["callee"]
        assert outcomes["callee"]["received"] == b"hello from A"
        assert outcomes["opener"]["reply"] == b"hello from B"

    @pytest.fixture(autouse=True)
    def _capture_workdir(self, tmp_path: Path) -> None:
        self._workdir = str(tmp_path)


class TestUnreachable:
    """I-5: never return a stale path for a dead peer."""

    def test_open_channel_to_expired_lease_is_unreachable_not_stale(
        self, running_server: BeaconServer
    ) -> None:
        client = redis.Redis(
            unix_socket_path=str(running_server.paths.socket_path), decode_responses=True
        )
        opener = str(uuid.uuid4())
        dead_peer = str(uuid.uuid4())

        # Register dead_peer with a TTL, then let it expire.
        client.hset(f"agent:{dead_peer}", mapping={"contactable": "1", "accepts": "handoff"})
        client.expire(f"agent:{dead_peer}", 1)
        threading.Event().wait(1.2)  # real TTL expiry, not a poll loop
        assert not client.exists(f"agent:{dead_peer}")

        result = open_channel(client, running_server.paths, opener, dead_peer, "handoff")

        assert isinstance(result, Unreachable)
        assert "no live lease" in result.reason
        client.close()

    def test_open_channel_to_non_contactable_peer_is_unreachable(
        self, running_server: BeaconServer
    ) -> None:
        client = redis.Redis(
            unix_socket_path=str(running_server.paths.socket_path), decode_responses=True
        )
        opener = str(uuid.uuid4())
        peer = str(uuid.uuid4())
        client.hset(f"agent:{peer}", mapping={"contactable": "0", "accepts": "handoff"})
        client.expire(f"agent:{peer}", 30)

        result = open_channel(client, running_server.paths, opener, peer, "handoff")

        assert isinstance(result, Unreachable)
        assert "not contactable" in result.reason
        client.close()

    def test_open_channel_for_unaccepted_kind_is_unreachable(
        self, running_server: BeaconServer
    ) -> None:
        client = redis.Redis(
            unix_socket_path=str(running_server.paths.socket_path), decode_responses=True
        )
        opener = str(uuid.uuid4())
        peer = str(uuid.uuid4())
        client.hset(f"agent:{peer}", mapping={"contactable": "1", "accepts": "query"})
        client.expire(f"agent:{peer}", 30)

        result = open_channel(client, running_server.paths, opener, peer, "handoff")

        assert isinstance(result, Unreachable)
        assert "does not accept" in result.reason
        client.close()


class TestDropOnFull:
    """D-10 / M-12: a full ring drops the message; send() never blocks or raises."""

    def test_send_returns_false_on_full_ring_within_one_second(self) -> None:
        import orpheus

        from beagle.beacon.contact import send

        ring_path = "/dev/shm/beacon_test_full_channel.ring"
        Path(ring_path).unlink(missing_ok=True)
        ring = orpheus.OrpheusRing(ring_path, "writer", True, 128, 4, "fifo")

        start = time.monotonic()
        # Fill the tiny ring.
        for _ in range(4):
            assert send(ring, b"x") is True
        # One more must drop, not block, not raise.
        dropped = send(ring, b"x")
        elapsed = time.monotonic() - start

        assert dropped is False
        assert elapsed < 1.0
        Path(ring_path).unlink(missing_ok=True)


class TestChannelCap:
    """D-11: the per-agent channel cap refuses the overflow, evicts nothing."""

    def test_opening_over_the_cap_is_refused_without_evicting(
        self, running_server: BeaconServer
    ) -> None:
        client = redis.Redis(
            unix_socket_path=str(running_server.paths.socket_path), decode_responses=True
        )
        opener = str(uuid.uuid4())
        cap = 2
        peers = [str(uuid.uuid4()) for _ in range(cap + 1)]
        for peer in peers:
            client.hset(f"agent:{peer}", mapping={"contactable": "1", "accepts": "handoff"})
            client.expire(f"agent:{peer}", 60)

        opened = []
        for peer in peers[:cap]:
            result = open_channel(
                client, running_server.paths, opener, peer, "handoff", max_channels_per_agent=cap
            )
            assert not isinstance(result, Unreachable), result
            opened.append(result)

        overflow = open_channel(
            client, running_server.paths, opener, peers[cap], "handoff", max_channels_per_agent=cap
        )
        assert isinstance(overflow, Unreachable)
        assert "channel cap" in overflow.reason

        # No existing channel was evicted.
        for ch in opened:
            assert client.exists(f"chan:{ch.channel_id}")
        client.close()


class TestSweep:
    """D-11: the sweep unlinks ring FILES of a dead party's channel."""

    def test_sweep_unlinks_ring_files_of_a_dead_party(self, running_server: BeaconServer) -> None:
        client = redis.Redis(
            unix_socket_path=str(running_server.paths.socket_path), decode_responses=True
        )
        opener = str(uuid.uuid4())
        peer = str(uuid.uuid4())
        client.hset(f"agent:{peer}", mapping={"contactable": "1", "accepts": "handoff"})
        client.expire(f"agent:{peer}", 60)
        client.hset(f"agent:{opener}", mapping={"contactable": "1"})
        client.expire(f"agent:{opener}", 1)  # opener's own lease will expire

        result = open_channel(client, running_server.paths, opener, peer, "handoff")
        assert not isinstance(result, Unreachable), result
        assert Path(result.a2b_path).exists()
        assert Path(result.b2a_path).exists()

        threading.Event().wait(1.2)  # real TTL expiry, not a poll loop
        assert not client.exists(f"agent:{opener}")

        closed = sweep_channels(client, running_server.paths)

        assert result.channel_id in closed
        assert not Path(result.a2b_path).exists()
        assert not Path(result.b2a_path).exists()
        assert not client.exists(f"chan:{result.channel_id}")
        client.close()
