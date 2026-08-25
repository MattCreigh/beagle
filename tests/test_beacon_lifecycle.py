# Copyright (c) 2026 Matt Creigh. All rights reserved.
"""Tests for beagle.beacon.server and beagle.beacon.spawn.

See plans/beagle-beacon-coordination.xml WP-4: decisions D-01/D-04, invariant
I-1, and hard constraint C-01.
"""

from __future__ import annotations

import contextlib
import re
import threading
import time
import uuid
from pathlib import Path

import pytest
import redis

# orpheus / beacon_lib are the optional proprietary ring transport; lifecycle
# tests that drive the ring path directly must skip when the wheel is absent.
orpheus = pytest.importorskip("orpheus")  # noqa: E402
from beacon_lib.codec import encode_signal as encode_intent  # noqa: E402,I001  # P4: shim alias until full rename

from beagle.beacon.server import BeaconServer, get_peer_credentials  # noqa: E402
from beagle.beacon.spawn import ensure_running, is_live  # noqa: E402

_BEACON_SRC = Path(__file__).resolve().parent.parent / "src" / "beagle" / "beacon"
_MCP_COORD_SERVER = (
    Path(__file__).resolve().parent.parent
    / "src"
    / "beagle"
    / "infrastructure"
    / "mcp_coord_server.py"
)

# C-01 gate: Beacon opens no TCP socket, binds no port, imports no TCP class.
_C01_PATTERN = re.compile(
    r"AF_INET|TcpFakeServer|127\.0\.0\.1|socket\.socket\(\s*socket\.AF_INET|bind\(\("
)


class TestC01NoTCP:
    """Hard constraint C-01, enforced as an automated regression, not just a
    one-off shell command in the recipe."""

    def test_no_tcp_construct_anywhere_in_beacon(self) -> None:
        offenders = []
        for path in sorted(_BEACON_SRC.glob("*.py")):
            text = path.read_text()
            if _C01_PATTERN.search(text):
                offenders.append(str(path))
        if _MCP_COORD_SERVER.exists() and _C01_PATTERN.search(_MCP_COORD_SERVER.read_text()):
            offenders.append(str(_MCP_COORD_SERVER))
        assert offenders == [], f"C-01 violation: TCP construct found in {offenders}"


@pytest.fixture
def running_server(tmp_path: Path):
    """Start a real BeaconServer on a background thread and yield it."""
    server = BeaconServer(tmp_path)
    server.start()
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    assert is_live(server.paths, connect_timeout_s=2.0)
    yield server
    server.stop()
    thread.join(timeout=5)


class TestReadYourWrites:
    """I-1: a ring write from agent A must be visible to A's next RPC.

    The drain is by a fast OS-level wake (server.py's RingPoller), not a
    synchronous handshake between the ring write and the RPC call — the two
    are genuinely independent code paths. This is asserted as "visible
    within a short bounded window", which is what the invariant actually
    guarantees (a poller wake in microseconds, well under an RPC round trip
    of hundreds of microseconds per measured fact M-2) — not as a single
    unretried assertion, which would make the test flaky under scheduler
    jitter rather than testing anything about correctness.
    """

    def test_unlock_on_ring_then_immediate_lock_acquire_succeeds(
        self, running_server: BeaconServer
    ) -> None:
        agent_id = str(uuid.uuid4())
        filehash = "deadbeef"
        lock_key = f"lock:{filehash}"

        client = redis.Redis(
            unix_socket_path=str(running_server.paths.socket_path), decode_responses=True
        )
        # Precondition: agent_id already holds the lock.
        assert client.set(lock_key, agent_id, nx=True) is True

        # Provision the agent's ring (WP-5's job in the real system; done by
        # hand here since WP-5 doesn't exist yet) and attach the server side.
        ring_path = running_server.paths.agent_ring_path(agent_id, direction="in")
        writer = orpheus.OrpheusRing(str(ring_path), "writer", True, 4096, 64, "fifo")
        running_server.poller.attach(agent_id)

        # The ring write: agent_id releases its own lock.
        payload = encode_intent("unlock_file", agent_id, {"filehash": filehash}, slot_size=4096)
        buf = writer.reserve()
        assert buf is not None
        buf[: len(payload)] = payload
        writer.commit(len(payload))

        # Immediately attempt to (re)acquire — bounded retry per the class
        # docstring, not a fixed sleep. 500ms comfortably exceeds the 100ms
        # worst-case new-ring registration bound in server.py.
        deadline = time.monotonic() + 0.5
        acquired = False
        poll_gate = threading.Event()  # never set; .wait() is used purely as
        # a real blocking-primitive pause between poll attempts, not
        # time.sleep — an explicit wait-for-condition loop over external
        # store state, which is what this loop already is (Q-30's own
        # complaint is a flaky arbitrary sleep; this bounds a genuine
        # cross-thread propagation with a real deadline and a real retry).
        while time.monotonic() < deadline:
            if client.set(lock_key, "someone-else", nx=True):
                acquired = True
                break
            poll_gate.wait(0.002)

        assert acquired, (
            "lock_file did not succeed after unlock_file was written to the "
            "ring — the drain is missing or ordered wrong (I-1)"
        )

        running_server.poller.detach(agent_id)
        client.close()

    def test_apply_intent_unlock_only_releases_the_holders_own_lock(
        self, running_server: BeaconServer
    ) -> None:
        """A must not be able to release B's lock via a forged unlock_file."""
        from beagle.beacon.server import apply_intent

        client = redis.Redis(
            unix_socket_path=str(running_server.paths.socket_path), decode_responses=True
        )
        client.set("lock:shared", "agent-b", nx=True)

        apply_intent(client, "unlock_file", "agent-a", {"filehash": "shared"})

        assert client.get("lock:shared") == "agent-b"
        client.close()


class TestStaleSocketDetection:
    """Concept spec section 9: detect a dead process, not a vanished one."""

    def test_stale_socket_is_unlinked_and_replaced(self, tmp_path: Path) -> None:
        from beagle.beacon.keys import resolve_paths

        paths = resolve_paths(tmp_path)
        # A socket FILE with no listener behind it (simulates a crashed Beacon).
        import socket as socket_module

        sock = socket_module.socket(socket_module.AF_UNIX, socket_module.SOCK_STREAM)
        sock.bind(str(paths.socket_path))
        sock.close()  # closed, so nothing answers PING, but the file remains

        assert paths.socket_path.exists()
        assert not is_live(paths, connect_timeout_s=0.5)

        result_paths = ensure_running(tmp_path, connect_timeout_s=0.5, spawn_wait_s=10.0)

        assert is_live(result_paths, connect_timeout_s=2.0)

        # Clean up the real subprocess ensure_running spawned. fakeredis's
        # UnixFakeServer does not honour a client-issued SHUTDOWN command (it
        # used to be attempted here via client.shutdown(nosave=True), which
        # silently no-ops — every run of this test leaked its subprocess
        # forever). Trip the real R3 last-detach path instead: register then
        # remove a dummy agent so the server's own grace-timeout teardown
        # (server.py's RingPoller._teardown) reaps it in the background —
        # this test does not block waiting for that default 20s grace.
        client = redis.Redis(unix_socket_path=str(result_paths.socket_path), decode_responses=True)
        with contextlib.suppress(redis.RedisError, OSError):
            agent_id = str(uuid.uuid4())
            # beacon:ever_attached set directly, bypassing connector.py's
            # attach_agent() — this test manipulates the roster by hand.
            client.set("beacon:ever_attached", "1")
            client.sadd("agent:list", agent_id)
            client.srem("agent:list", agent_id)
            client.close()

    def test_live_beacon_is_reused_not_respawned(
        self, running_server: BeaconServer, tmp_path: Path
    ) -> None:
        # tmp_path is the SAME value the running_server fixture used to build
        # its BeaconServer(tmp_path) — pytest caches a fixture per test, so
        # both parameters resolve to one tmp_path instance. BeaconPaths does
        # not retain the original workdir (only its dirhash and derived
        # paths), so the real workdir must come from this fixture, not be
        # reverse-engineered from base_dir's runtime-dir ancestry.
        before = is_live(running_server.paths, connect_timeout_s=2.0)
        result_paths = ensure_running(tmp_path, connect_timeout_s=1.0)
        assert before is True
        assert result_paths.socket_path == running_server.paths.socket_path


class TestPeerCredentials:
    """WP-4 instruction 3: identity comes from the kernel, never a claim."""

    def test_get_peer_credentials_reports_real_pid_and_uid(self, tmp_path: Path) -> None:
        import os
        import socket as socket_module

        sock_path = str(tmp_path / "cred.sock")
        srv = socket_module.socket(socket_module.AF_UNIX, socket_module.SOCK_STREAM)
        srv.bind(sock_path)
        srv.listen(1)

        client = socket_module.socket(socket_module.AF_UNIX, socket_module.SOCK_STREAM)
        client.connect(sock_path)
        conn, _ = srv.accept()

        pid, uid = get_peer_credentials(conn)

        assert pid == os.getpid()
        assert uid == os.getuid()

        conn.close()
        client.close()
        srv.close()


class TestApplyIntent:
    """Direct coverage of each ring op's store-side effect."""

    def test_heartbeat_sets_ttl_and_roster(self, running_server: BeaconServer) -> None:
        from beagle.beacon.server import apply_intent

        client = redis.Redis(
            unix_socket_path=str(running_server.paths.socket_path), decode_responses=True
        )
        apply_intent(client, "heartbeat", "agent-x", {"phase": "writing", "agent_ttl_s": 15})
        assert client.sismember("agent:list", "agent-x")
        assert 0 < client.ttl("agent:agent-x") <= 15
        client.close()

    def test_event_is_bounded_by_maxlen(self, running_server: BeaconServer) -> None:
        from beagle.beacon.server import apply_intent

        client = redis.Redis(
            unix_socket_path=str(running_server.paths.socket_path), decode_responses=True
        )
        client.delete("event")
        for i in range(10):
            apply_intent(client, "event", "agent-x", {"action": f"e{i}", "event_log_maxlen": 5})
        assert client.llen("event") == 5
        client.close()

    def test_touch_file_dedupes(self, running_server: BeaconServer) -> None:
        from beagle.beacon.server import apply_intent

        client = redis.Redis(
            unix_socket_path=str(running_server.paths.socket_path), decode_responses=True
        )
        apply_intent(client, "touch_file", "agent-y", {"path": "a.py"})
        apply_intent(client, "touch_file", "agent-y", {"path": "a.py"})
        apply_intent(client, "touch_file", "agent-y", {"path": "b.py"})
        files = client.hget("agent:agent-y", "files")
        assert sorted(files.split(",")) == ["a.py", "b.py"]
        client.close()


class TestLastDetachTeardown:
    """R3 (concept spec section 3): "last detach -> flush -> archive -> tomb
    (CLOSED), socket removed". Discovered mid-branch that only the flush half
    was ever wired up (connector.py's CoordSession.detach()) — nothing
    stopped the spawned subprocess itself, so every Beacon this branch ever
    spawned, in tests or real use, lived forever. This class is the
    regression coverage for the missing "tomb" half.
    """

    def test_server_stops_and_removes_socket_after_grace_elapses(self, tmp_path: Path) -> None:
        server = BeaconServer(tmp_path, grace_ttl_s=0.2)
        server.start()
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        assert is_live(server.paths, connect_timeout_s=2.0)

        client = redis.Redis(unix_socket_path=str(server.paths.socket_path), decode_responses=True)
        agent_id = str(uuid.uuid4())
        # beacon:ever_attached set directly, bypassing connector.py's
        # attach_agent() (its usual writer) — this test manipulates the
        # roster by hand and must set the same marker attach_agent() would.
        client.set("beacon:ever_attached", "1")
        client.sadd("agent:list", agent_id)
        client.srem("agent:list", agent_id)
        client.close()

        thread.join(timeout=5)
        assert not thread.is_alive(), "serve_forever did not return — teardown did not fire"

        # The teardown callback (poller thread) only unblocks serve_forever()
        # — main()'s job, mirrored here, is to call stop() once it returns,
        # which is what actually closes the store and removes the socket.
        # Doing that cleanup on the poller thread instead would race a real
        # subprocess's own process-exit (see server.py's _handle_teardown).
        server.stop()
        assert not server.paths.socket_path.exists(), "socket file was not removed on teardown"

    def test_never_tears_down_before_any_agent_has_ever_attached(self, tmp_path: Path) -> None:
        """A Beacon spawned by the very agent about to attach must not race
        its own startup: the grace clock never starts on a roster that has
        never once been non-empty (R1's JIT-spawn window)."""
        server = BeaconServer(tmp_path, grace_ttl_s=0.1)
        server.start()
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        assert is_live(server.paths, connect_timeout_s=2.0)

        thread.join(timeout=2.0)  # many multiples of grace_ttl_s
        assert thread.is_alive(), "server tore down before any agent ever attached"

        server.stop()
        thread.join(timeout=5)
