# Copyright (c) 2026 Matt Creigh. Released under the MIT License.
# SPDX-License-Identifier: MIT
"""Tests for beagle.beacon.archive — the last-detach snapshot and election.

See plans/beagle-beacon-coordination.xml WP-6: decision D-07, concept spec
section 9 (a failed flush keeps a .partial and logs loudly).
"""

from __future__ import annotations

import json
import threading
from pathlib import Path

import pytest
import redis

from beagle.beacon.archive import elect_flush_owner, flush_archive, snapshot_keyspace
from beagle.beacon.server import BeaconServer


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


class TestFlushOwnerElection:
    """Exactly one of possibly-several 'last agent' candidates flushes."""

    def test_two_simultaneous_candidates_only_one_writes_an_archive(
        self, running_server: BeaconServer
    ) -> None:
        client_a = redis.Redis(
            unix_socket_path=str(running_server.paths.socket_path), decode_responses=True
        )
        client_b = redis.Redis(
            unix_socket_path=str(running_server.paths.socket_path), decode_responses=True
        )
        client_a.hset("issue:BGL-1", mapping={"title": "x"})

        results: list[tuple[str, bool]] = []
        barrier = threading.Barrier(2)

        def contend(name: str, client: redis.Redis) -> None:
            barrier.wait(timeout=5)
            won = elect_flush_owner(client, name)
            results.append((name, won))
            if won:
                flush_archive(client, running_server.paths)

        t_a = threading.Thread(target=contend, args=("agent-a", client_a))
        t_b = threading.Thread(target=contend, args=("agent-b", client_b))
        t_a.start()
        t_b.start()
        t_a.join(timeout=5)
        t_b.join(timeout=5)

        winners = [name for name, won in results if won]
        assert len(winners) == 1, f"expected exactly one winner, got {winners}"

        archive_files = list(running_server.paths.archive_dir.glob("beacon-*.jsonl"))
        assert len(archive_files) == 1, (
            f"expected exactly ONE archive file, found {len(archive_files)} — "
            "the NX election is wrong"
        )
        client_a.close()
        client_b.close()


class TestSecretRejectionInArchive:
    """C-03: a secret-shaped key must never reach the archive."""

    def test_snapshot_rejects_secret_shaped_key(self, running_server: BeaconServer) -> None:
        client = redis.Redis(
            unix_socket_path=str(running_server.paths.socket_path), decode_responses=True
        )
        client.set("issue:api_token_leak", "should-never-be-archived")

        with pytest.raises(ValueError, match="secret"):
            snapshot_keyspace(client)

        client.close()


class TestFailedFlushLeavesPartial:
    def test_snapshot_failure_leaves_partial_and_logs_error(
        self, running_server: BeaconServer, caplog: pytest.LogCaptureFixture
    ) -> None:
        client = redis.Redis(
            unix_socket_path=str(running_server.paths.socket_path), decode_responses=True
        )
        client.set("issue:trigger_secret_key", "x")  # forces snapshot_keyspace to raise

        with caplog.at_level("ERROR", logger="Beagle.beacon.archive"), pytest.raises(ValueError):
            flush_archive(client, running_server.paths)

        assert any("archive flush failed" in r.message for r in caplog.records)
        partials = list(running_server.paths.archive_dir.glob("*.partial"))
        assert len(partials) == 1, "a failed flush must leave a .partial marker"
        client.close()


class TestSnapshotRoundTrip:
    def test_snapshot_captures_every_redis_type(self, running_server: BeaconServer) -> None:
        client = redis.Redis(
            unix_socket_path=str(running_server.paths.socket_path), decode_responses=True
        )
        client.set("meta:dir", "/some/path")
        client.hset("issue:BGL-1", mapping={"title": "x"})
        client.sadd("issue:by_state:open", "BGL-1")
        client.lpush("comment:BGL-1", "hello")
        client.zadd("zscored", {"a": 1.0})

        dest = flush_archive(client, running_server.paths)

        assert dest.exists()
        payload = json.loads(dest.read_text())
        assert payload["meta:dir"] == {"type": "string", "value": "/some/path"}
        assert payload["issue:BGL-1"] == {"type": "hash", "value": {"title": "x"}}
        assert payload["issue:by_state:open"] == {"type": "set", "value": ["BGL-1"]}
        assert payload["comment:BGL-1"] == {"type": "list", "value": ["hello"]}
        client.close()

    def test_archive_numbering_increments(self, running_server: BeaconServer) -> None:
        client = redis.Redis(
            unix_socket_path=str(running_server.paths.socket_path), decode_responses=True
        )
        client.set("k", "v")
        d1 = flush_archive(client, running_server.paths)
        d2 = flush_archive(client, running_server.paths)
        assert d1 != d2
        assert d1.name == "beacon-0.jsonl"
        assert d2.name == "beacon-1.jsonl"
        client.close()


class TestArchiveFileMode:
    def test_archive_is_mode_0600(self, running_server: BeaconServer) -> None:
        import os

        client = redis.Redis(
            unix_socket_path=str(running_server.paths.socket_path), decode_responses=True
        )
        client.set("k", "v")
        dest = flush_archive(client, running_server.paths)
        mode = os.stat(dest).st_mode & 0o777
        assert oct(mode) == "0o600"
        client.close()
