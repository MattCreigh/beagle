"""Tests for beagle.beacon.backends.fakeredis_unix — the fakeredis-over-unix-
socket Beacon store backend.

See plans/beagle-beacon-coordination.xml WP-2 and
plans/beagle-coord-backend-slot.xml WP-B3 (this module moved here from the
deleted beagle.beacon.store). The cross-process test proves measured fact
M-8 and the M-9 fix: without the ``get_request`` override in
:class:`~beagle.beacon.backends.fakeredis_unix.UnixFakeServer`, two client
processes silently fail to share state instead of raising, so this test
asserts on the shared STATE (both ids visible in the roster, one loses SET
NX) rather than merely asserting no exception was raised.
"""

from __future__ import annotations

import multiprocessing
import os
from pathlib import Path

import pytest
import redis

from beagle.beacon.backends.fakeredis_unix import FakeredisUnixDriver, UnixFakeServer
from beagle.beacon.keys import BeaconPaths, dirhash, resolve_paths


def _paths_for_socket(sock_path: str, tmp_path: Path) -> BeaconPaths:
    """A minimal BeaconPaths for a socket not derived via resolve_paths().

    beacon_socket's fixture builds its own path directly for a real
    child-process server, bypassing resolve_paths() entirely — only
    socket_path is exercised by is_live(), so the other fields are unused
    placeholders.
    """
    return BeaconPaths(
        dirhash="test",
        base_dir=tmp_path,
        socket_path=Path(sock_path),
        rings_dir=tmp_path,
        archive_dir=tmp_path,
    )


def _run_server(sock_path: str, ready: multiprocessing.Event, stop: multiprocessing.Event) -> None:
    """Child process target: run a UnixFakeServer until told to stop."""
    server = UnixFakeServer(sock_path)
    ready.set()
    server.timeout = 0.1
    while not stop.is_set():
        server.handle_request()
    server.shutdown()
    server.server_close()


def _client_op(
    sock_path: str,
    agent_id: str,
    first_writer: multiprocessing.Event,
    writes_done: multiprocessing.Barrier,
    results: multiprocessing.Queue,
    *,
    is_first: bool,
) -> None:
    """Child process target: perform the shared-state operations for one agent.

    Ordering is by real synchronization, not by timing: the first writer
    signals `first_writer` right after its own SET NX so the second writer
    only starts once the lock outcome is deterministic, and both writers
    rendezvous on `writes_done` before either reads the roster — so a
    roster read can never observe only its own write.
    """
    r = redis.Redis(unix_socket_path=sock_path, decode_responses=True)
    if not is_first:
        assert first_writer.wait(timeout=5), "first writer never signalled"
    got_lock = r.set("lock:file", agent_id, nx=True, ex=5)
    if is_first:
        first_writer.set()
    r.sadd("agent:list", agent_id)
    r.hset(f"agent:{agent_id}", mapping={"phase": "test"})
    writes_done.wait(timeout=5)
    roster = sorted(r.smembers("agent:list"))
    results.put({"agent_id": agent_id, "got_lock": bool(got_lock), "roster": roster})
    r.close()


@pytest.fixture
def beacon_socket(tmp_path: Path):
    """Start a UnixFakeServer as a real child process and yield its socket path."""
    sock_path = str(tmp_path / "beacon.sock")
    ready = multiprocessing.Event()
    stop = multiprocessing.Event()
    proc = multiprocessing.Process(target=_run_server, args=(sock_path, ready, stop))
    proc.start()
    assert ready.wait(timeout=5), "server process did not start in time"
    yield sock_path
    stop.set()
    proc.join(timeout=5)
    if proc.is_alive():
        proc.terminate()
        proc.join(timeout=2)


class TestSharedStoreAcrossProcesses:
    """M-8 / M-9: two SEPARATE processes must observe one shared store."""

    def test_two_processes_share_one_store(self, beacon_socket: str) -> None:
        results: multiprocessing.Queue = multiprocessing.Queue()
        first_writer = multiprocessing.Event()
        writes_done = multiprocessing.Barrier(2)
        p_a = multiprocessing.Process(
            target=_client_op,
            args=(beacon_socket, "agent-a", first_writer, writes_done, results),
            kwargs={"is_first": True},
        )
        p_b = multiprocessing.Process(
            target=_client_op,
            args=(beacon_socket, "agent-b", first_writer, writes_done, results),
            kwargs={"is_first": False},
        )
        p_a.start()
        p_b.start()
        p_a.join(timeout=5)
        p_b.join(timeout=5)

        outcomes = {}
        for _ in range(2):
            r = results.get(timeout=5)
            outcomes[r["agent_id"]] = r

        assert outcomes["agent-a"]["got_lock"] is True
        assert outcomes["agent-b"]["got_lock"] is False, (
            "second process acquired a lock the first already held — "
            "the store is NOT shared (the M-9 defect: two separate stores)"
        )
        assert outcomes["agent-a"]["roster"] == ["agent-a", "agent-b"]
        assert outcomes["agent-b"]["roster"] == ["agent-a", "agent-b"], (
            "agent-b's roster read did not include agent-a — writes are not "
            "visible across processes"
        )

    def test_store_survives_zero_clients(self, beacon_socket: str) -> None:
        r1 = redis.Redis(unix_socket_path=beacon_socket, decode_responses=True)
        r1.set("persisted", "yes")
        r1.close()

        r2 = redis.Redis(unix_socket_path=beacon_socket, decode_responses=True)
        assert r2.get("persisted") == "yes"
        r2.close()

    def test_ping_true_for_live_server(self, beacon_socket: str, tmp_path: Path) -> None:
        driver = FakeredisUnixDriver()
        paths = _paths_for_socket(beacon_socket, tmp_path)
        assert driver.is_live(paths, connect_timeout_s=2.0) is True

    def test_ping_false_for_missing_socket(self, tmp_path: Path) -> None:
        driver = FakeredisUnixDriver()
        paths = _paths_for_socket(str(tmp_path / "nothing.sock"), tmp_path)
        assert driver.is_live(paths, connect_timeout_s=2.0) is False


class TestSocketPermissions:
    """C-01 / D-03: the socket must be private to the owning user."""

    def test_socket_mode_is_0600(self, tmp_path: Path) -> None:
        sock_path = tmp_path / "mode.sock"
        server = UnixFakeServer(str(sock_path))
        try:
            mode = os.stat(sock_path).st_mode & 0o777
            assert oct(mode) == "0o600", f"expected 0o600, got {oct(mode)}"
        finally:
            server.server_close()

    def test_socket_directory_is_0700(self, tmp_path: Path) -> None:
        paths = resolve_paths(tmp_path)
        mode = os.stat(paths.base_dir).st_mode & 0o777
        assert oct(mode) == "0o700"


class TestDirhashContainment:
    """C-05: containment by real path resolution, never string prefix."""

    def test_symlink_resolves_to_target_hash(self, tmp_path: Path) -> None:
        real_dir = tmp_path / "real"
        real_dir.mkdir()
        link = tmp_path / "link"
        link.symlink_to(real_dir)

        assert dirhash(link) == dirhash(real_dir)

    def test_different_directories_hash_differently(self, tmp_path: Path) -> None:
        a = tmp_path / "a"
        b = tmp_path / "b"
        a.mkdir()
        b.mkdir()
        assert dirhash(a) != dirhash(b)

    def test_relative_path_resolves_before_hashing(self, tmp_path: Path, monkeypatch) -> None:
        sub = tmp_path / "sub"
        sub.mkdir()
        monkeypatch.chdir(tmp_path)
        assert dirhash("sub") == dirhash(sub.resolve())
